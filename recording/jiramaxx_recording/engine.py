"""
Audio capture + local transcription engine.

Captures system audio (loopback) plus an optional microphone, mixes them, and
transcribes in 30-second chunks with faster-whisper. Transcription runs entirely
on the local CPU — no audio or text ever leaves the machine. The only network
activity is a one-time, read-only download of the public Whisper weights
(``base.en``) from Hugging Face on first use; thereafter the cached model loads
offline. An explicitly configured ``model_path`` or a model pre-placed under
``models/base.en`` is used directly (no download). See ``model_needs_download``.
"""
from __future__ import annotations
import os
import time
import warnings
import threading
import queue
from pathlib import Path
from datetime import datetime

import numpy as np
import soundcard as sc

SAMPLE_RATE = 16000
CHUNK_SECONDS = 30
_SUB_SECONDS = 1
_AUDIO_Q_MAX = 20  # caps in-flight audio at ~38MB if transcription falls behind

# Whisper sizes the user can pick (faster-whisper resolves these to the
# Systran/faster-whisper-<name> repos). English-only variants are listed since
# transcription is locked to English; large-v3 has no .en variant.
DEFAULT_MODEL = 'base.en'
AVAILABLE_MODELS = ['tiny.en', 'base.en', 'small.en', 'medium.en', 'large-v3']
# Rough on-disk/download sizes (MB) for confirmation prompts — not exact.
_MODEL_APPROX_MB = {
    'tiny.en': 75, 'base.en': 145, 'small.en': 480,
    'medium.en': 1500, 'large-v3': 3090,
}

_model_lock = threading.Lock()
_whisper_model = None
_whisper_lang_loaded: str | None = None
_whisper_key_loaded: tuple | None = None  # (model_name, resolved_source)

# WASAPI loopback capture emits "data discontinuity" warnings while the stream
# primes (first moment of a recording). They're benign startup noise, so we
# swallow *only that message* for a short warmup window after each start — any
# later discontinuity (which can signal real dropped audio) still surfaces.
_DISCONTINUITY_WARMUP_SEC = 1.0
_warmup_until = 0.0
_filter_installed = False


def _install_discontinuity_filter() -> None:
    """Install a one-time warnings hook that drops the soundcard 'data
    discontinuity' message during the post-start warmup window only."""
    global _filter_installed
    if _filter_installed:
        return
    orig = warnings.showwarning

    def _showwarning(message, category, filename, lineno, file=None, line=None):
        if time.monotonic() < _warmup_until and 'discontinuity' in str(message):
            return  # expected stream-priming glitch at recording start
        orig(message, category, filename, lineno, file, line)

    warnings.showwarning = _showwarning
    _filter_installed = True


def _resolve_model_source(model_path: str = '',
                          model_name: str = DEFAULT_MODEL) -> tuple[str, bool]:
    """Return (source, local_only). Prefers an explicit path, then a bundled
    model of the requested size, then the plain size name (which permits a
    one-time download for non-air-gapped installs)."""
    if model_path:
        return os.path.expanduser(model_path), True
    bundled = Path(__file__).parent / 'models' / (model_name or DEFAULT_MODEL)
    if bundled.exists():
        return str(bundled), True
    return (model_name or DEFAULT_MODEL), False


def model_needs_download(model_path: str = '',
                         model_name: str = DEFAULT_MODEL) -> bool:
    """True if loading the model would hit the network (i.e. it's neither at an
    explicit path, nor bundled, nor already in the Hugging Face cache). Used to
    decide whether to prompt for / show a download before recording starts."""
    _source, local_only = _resolve_model_source(model_path, model_name)
    if local_only:
        return False
    try:
        from huggingface_hub import try_to_load_from_cache
        repo = f'Systran/faster-whisper-{model_name or DEFAULT_MODEL}'
        hit = try_to_load_from_cache(repo, 'model.bin')
    except Exception:
        return True  # can't tell — assume a download so the user is warned
    return not isinstance(hit, str)


def _load_model(language: str = 'en', model_path: str = '',
                model_name: str = DEFAULT_MODEL):
    """Load (and cache) the faster-whisper model. Reloads if the requested
    model/source changes; downloads on first use when not local/bundled."""
    global _whisper_model, _whisper_lang_loaded, _whisper_key_loaded
    with _model_lock:
        source, local_only = _resolve_model_source(model_path, model_name)
        key = (model_name or DEFAULT_MODEL, source)
        if (_whisper_model is None or _whisper_lang_loaded != language
                or _whisper_key_loaded != key):
            from faster_whisper import WhisperModel
            if local_only:
                # Guarantee zero network calls at runtime (not even an update ping).
                os.environ.setdefault('HF_HUB_OFFLINE', '1')
            _whisper_model = WhisperModel(
                source, device='cpu', compute_type='int8',
                local_files_only=local_only)
            _whisper_lang_loaded = language
            _whisper_key_loaded = key
        return _whisper_model


def prepare_model(model_path: str = '', model_name: str = DEFAULT_MODEL) -> None:
    """Download (if needed) and load the selected model so a later recording
    reuses it with no download. Only ever called from an explicit user action
    (the Download/Prepare button or a confirmed download prompt). Raises on
    failure (e.g. no network)."""
    _load_model('en', model_path, model_name)


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
    """Return (loopback_device_names, input_device_names)."""
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
        self._model_path = self._cfg.get('model_path', '')
        self._model_name = self._cfg.get('model', DEFAULT_MODEL) or DEFAULT_MODEL
        self._stop_event = threading.Event()
        self._audio_q: queue.Queue = queue.Queue(maxsize=_AUDIO_Q_MAX)
        self._start_time = datetime.now()
        self._recorders: list[_DeviceRecorder] = []
        self._mix_thread: threading.Thread | None = None
        self._tx_thread: threading.Thread | None = None
        self._had_audio = False  # set once any chunk carries real signal

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

        # Arm the warmup filter before touching the devices, so the stream-
        # priming "data discontinuity" warnings (pre-flight + the first moment of
        # capture) are swallowed for ~1s while everything spins up.
        global _warmup_until
        _install_discontinuity_filter()
        _warmup_until = time.monotonic() + _DISCONTINUITY_WARMUP_SEC

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
        # (By design recording only starts once the model is ready, so this is a
        # fast in-memory load, not a download.)
        threading.Thread(target=_load_model,
                         args=(self._language(), self._model_path, self._model_name),
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

    @property
    def had_audio(self) -> bool:
        """True if any captured chunk carried real signal (not pure silence)."""
        return self._had_audio

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
                if not self._had_audio and np.abs(mixed).max() > 0.005:
                    self._had_audio = True  # real signal, not silence
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
        model = _load_model(self._language(), self._model_path, self._model_name)
        pending_chunks: list[str] = []
        chunks_after = 0  # countdown of follow-up chunks needed since last keyword

        while True:
            chunk = self._audio_q.get()
            if chunk is None:
                if pending_chunks:
                    self._save_suggestion(''.join(pending_chunks))
                break

            segments, _ = model.transcribe(chunk, beam_size=5,
                                           language=self._language(),
                                           vad_filter=True)
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
