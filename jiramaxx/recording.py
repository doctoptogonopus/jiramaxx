from __future__ import annotations
import os
import threading
import queue
from pathlib import Path
from datetime import datetime

# Corporate kill switch. Set JIRAMAXX_DISABLE_RECORDING=1 in the system or
# user environment (e.g. via group policy) to force-disable the recording
# capability even if soundcard / faster-whisper happen to be importable.
_DISABLED_BY_ENV = os.environ.get('JIRAMAXX_DISABLE_RECORDING', '').strip().lower() in (
    '1', 'true', 'yes', 'on'
)

if _DISABLED_BY_ENV:
    RECORDING_AVAILABLE = False
else:
    try:
        import numpy as np
        import soundcard as sc
        from faster_whisper import WhisperModel  # noqa: F401 — import check only
        RECORDING_AVAILABLE = True
    except ImportError:
        RECORDING_AVAILABLE = False

SAMPLE_RATE = 16000
CHUNK_SECONDS = 30
_SUB_SECONDS = 1
_AUDIO_Q_MAX = 20  # caps in-flight audio at ~38MB if transcription falls behind

_model_lock = threading.Lock()
_whisper_model = None
_whisper_lang_loaded: str | None = None


def _load_model(language: str = 'en'):
    """Load (and cache) the faster-whisper model. Uses .en variant for English."""
    global _whisper_model, _whisper_lang_loaded
    with _model_lock:
        if _whisper_model is None or _whisper_lang_loaded != language:
            from faster_whisper import WhisperModel
            model_name = 'base.en' if language == 'en' else 'base'
            _whisper_model = WhisperModel(model_name, device='cpu', compute_type='int8')
            _whisper_lang_loaded = language
        return _whisper_model


def _round_to_30min(dt: datetime) -> datetime:
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


def _session_folder(start_time: datetime, base_dir: Path) -> Path:
    rounded = _round_to_30min(start_time)
    h = rounded.strftime('%I').lstrip('0') or '12'
    base_name = (
        f"{rounded.strftime('%Y-%m-%d')} "
        f"{h}-{rounded.strftime('%M')} {rounded.strftime('%p')}"
    )
    folder = base_dir / base_name
    if not folder.exists():
        return folder
    n = 2
    while (base_dir / f"{base_name} ({n})").exists():
        n += 1
    return base_dir / f"{base_name} ({n})"


def list_devices() -> tuple[list[str], list[str]]:
    """Return (loopback_device_names, input_device_names). Requires recording extra."""
    if not RECORDING_AVAILABLE:
        return [], []
    all_mics = sc.all_microphones(include_loopback=True)
    loopbacks, inputs = [], []
    for m in all_mics:
        is_lb = getattr(m, 'isloopback', 'loopback' in m.name.lower())
        (loopbacks if is_lb else inputs).append(m.name)
    return loopbacks, inputs


class _DeviceRecorder:
    """Captures one audio device into a queue of 1-second numpy chunks."""

    def __init__(self, device, sample_rate: int, sub_frames: int):
        self._device = device
        self._sample_rate = sample_rate
        self._sub_frames = sub_frames
        self._q: queue.Queue = queue.Queue(maxsize=10)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self): self._thread.start()
    def stop(self): self._stop.set()

    def get(self, timeout: float = 2.0):
        try:
            return self._q.get(timeout=timeout)
        except queue.Empty:
            return None

    def _run(self):
        try:
            with self._device.recorder(samplerate=self._sample_rate, channels=1) as rec:
                while not self._stop.is_set():
                    data = rec.record(numframes=self._sub_frames)
                    if data.ndim > 1:
                        data = data.mean(axis=1)
                    try:
                        self._q.put(data.astype(np.float32), timeout=2.0)
                    except queue.Full:
                        pass
        except Exception:
            pass


class RecordingSession:
    """
    Captures audio from configured devices, transcribes in 30-second chunks via
    faster-whisper, and saves transcripts + keyword-triggered suggestions to disk.
    Keyword window extends each time a new keyword fires in a follow-up chunk.
    """

    def __init__(self, config: dict):
        self._cfg = config.get('recording', {})
        self._stop_event = threading.Event()
        self._audio_q: queue.Queue = queue.Queue(maxsize=_AUDIO_Q_MAX)
        self._start_time = datetime.now()
        self._recorders: list[_DeviceRecorder] = []
        self._mix_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None

        transcript_dir = Path(
            self._cfg.get('transcript_dir', '~/.jiramaxx/transcripts')
        ).expanduser()
        suggestions_dir = Path(
            self._cfg.get('suggestions_dir', '~/.jiramaxx/suggestions')
        ).expanduser()
        transcript_dir.mkdir(parents=True, exist_ok=True)

        h = self._start_time.strftime('%I').lstrip('0') or '12'
        date_str = self._start_time.strftime('%Y-%m-%d')
        time_str = f"{h}-{self._start_time.strftime('%M')}{self._start_time.strftime('%p')}"
        self._transcript_file = transcript_dir / f"{date_str}_{time_str}_transcript.txt"
        self._session_dir = _session_folder(self._start_time, suggestions_dir)

        self._keywords = [
            kw.strip().lower()
            for kw in (self._cfg.get('keywords') or [])
            if kw and kw.strip()
        ]

    # ── Public API ──────────────────────────────────────────────────────────

    def start(self):
        """Open devices, prewarm the model, and start background threads.
        Raises RuntimeError if a device cannot be opened."""
        lb_dev = self._resolve_device(self._cfg.get('loopback_device', ''),
                                      prefer_loopback=True)
        if lb_dev is None:
            raise RuntimeError(
                "No system-audio (loopback) device available.\n"
                "Open Config → Recording and select an output device."
            )

        in_dev = None
        in_name = self._cfg.get('input_device', '')
        if in_name:
            in_dev = self._resolve_device(in_name, prefer_loopback=False)
            if in_dev is None:
                raise RuntimeError(f"Configured input device not found: {in_name}")

        # Pre-flight: open each device briefly so failures surface immediately
        # instead of being silently swallowed inside a background thread.
        for label, dev in [('loopback', lb_dev), ('input', in_dev)]:
            if dev is None:
                continue
            try:
                with dev.recorder(samplerate=SAMPLE_RATE, channels=1) as r:
                    r.record(numframes=SAMPLE_RATE // 10)
            except Exception as e:
                raise RuntimeError(
                    f"Cannot open {label} device '{dev.name}':\n{e}"
                ) from e

        sub_frames = SAMPLE_RATE * _SUB_SECONDS
        self._recorders.append(_DeviceRecorder(lb_dev, SAMPLE_RATE, sub_frames))
        if in_dev:
            self._recorders.append(_DeviceRecorder(in_dev, SAMPLE_RATE, sub_frames))
        for r in self._recorders:
            r.start()

        # Prewarm whisper so the first chunk doesn't pay the model-load cost.
        threading.Thread(target=_load_model, args=(self._language(),),
                         daemon=True).start()

        self._mix_thread = threading.Thread(target=self._mix_loop, daemon=True)
        self._tx_thread = threading.Thread(target=self._transcribe_loop, daemon=True)
        self._mix_thread.start()
        self._tx_thread.start()

    def stop(self, wait_for_transcription: bool = True):
        self._stop_event.set()
        for r in self._recorders:
            r.stop()
        if self._mix_thread:
            self._mix_thread.join(timeout=5)
        if wait_for_transcription and self._tx_thread:
            self._tx_thread.join(timeout=300)

    @property
    def transcript_path(self) -> Path:
        return self._transcript_file

    @property
    def suggestions_dir(self) -> Path:
        return self._session_dir

    @property
    def is_transcribing(self) -> bool:
        return self._tx_thread is not None and self._tx_thread.is_alive()

    # ── Internals ───────────────────────────────────────────────────────────

    def _language(self) -> str:
        # Locked to English; the config field is disabled with a tooltip.
        return 'en'

    def _resolve_device(self, name: str, prefer_loopback: bool):
        all_mics = sc.all_microphones(include_loopback=True)
        if name:
            for d in all_mics:
                if name.lower() in d.name.lower():
                    return d
        if prefer_loopback:
            for d in all_mics:
                if getattr(d, 'isloopback', 'loopback' in d.name.lower()):
                    return d
        return None

    def _mix_loop(self):
        chunk_frames = SAMPLE_RATE * CHUNK_SECONDS
        buffer: list = []
        frames_collected = 0
        try:
            while not self._stop_event.is_set():
                chunks = [r.get(timeout=_SUB_SECONDS + 1) for r in self._recorders]
                chunks = [c for c in chunks if c is not None]
                if not chunks:
                    continue
                min_len = min(len(c) for c in chunks)
                mixed = np.mean([c[:min_len] for c in chunks], axis=0)
                buffer.append(mixed)
                frames_collected += len(mixed)
                if frames_collected >= chunk_frames:
                    try:
                        self._audio_q.put(np.concatenate(buffer), timeout=5)
                    except queue.Full:
                        pass
                    buffer, frames_collected = [], 0
        finally:
            if buffer:
                try:
                    self._audio_q.put(np.concatenate(buffer), timeout=5)
                except queue.Full:
                    pass
            self._audio_q.put(None)  # sentinel for transcribe loop

    def _transcribe_loop(self):
        model = _load_model(self._language())
        pending_chunks: list[str] = []
        chunks_after = 0  # countdown of follow-up chunks needed since last keyword

        while True:
            chunk = self._audio_q.get()
            if chunk is None:
                if pending_chunks:
                    self._save_suggestion(''.join(pending_chunks))
                break

            segments, _ = model.transcribe(chunk, beam_size=5,
                                           language=self._language())
            text = ' '.join(s.text.strip() for s in segments).strip()

            if not text:
                if pending_chunks:
                    chunks_after -= 1
                    if chunks_after <= 0:
                        self._save_suggestion(''.join(pending_chunks))
                        pending_chunks, chunks_after = [], 0
                continue

            ts = datetime.now().strftime('%H:%M:%S')
            line = f"[{ts}] {text}\n"

            with open(self._transcript_file, 'a', encoding='utf-8') as f:
                f.write(line)

            has_keyword = bool(self._keywords) and any(
                kw in text.lower() for kw in self._keywords
            )

            if pending_chunks:
                pending_chunks.append(line)
                if has_keyword:
                    chunks_after = 1  # extend window
                else:
                    chunks_after -= 1
                    if chunks_after <= 0:
                        self._save_suggestion(''.join(pending_chunks))
                        pending_chunks, chunks_after = [], 0
            elif has_keyword:
                pending_chunks = [line]
                chunks_after = 1

    def _save_suggestion(self, text: str):
        if not text.strip():
            return
        self._session_dir.mkdir(parents=True, exist_ok=True)
        n = 1
        while (self._session_dir / f"suggestion_{n:03d}.txt").exists():
            n += 1
        with open(self._session_dir / f"suggestion_{n:03d}.txt", 'w',
                  encoding='utf-8') as f:
            f.write(text)
