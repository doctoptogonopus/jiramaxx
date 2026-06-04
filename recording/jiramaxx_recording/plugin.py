"""
Recording plugin: the bridge between core jiramaxx and the recording engine.

Core discovers this class via the ``jiramaxx.plugins`` entry point (declared in
this package's pyproject.toml) and calls its hooks to render a Record button in
the main window and a Recording tab in the config window. Core has no
compile-time knowledge of any of this.
"""
from __future__ import annotations
import os
import threading

import PySimpleGUI as sg

from jiramaxx.plugins import Plugin
from jiramaxx.utils import safe_read, show_error, bring_to_front

# Corporate kill switch. Set JIRAMAXX_DISABLE_RECORDING=1 (or true/yes/on) in
# the environment (e.g. via group policy) to force-disable recording even when
# this package is installed.
_DISABLED_BY_ENV = os.environ.get('JIRAMAXX_DISABLE_RECORDING', '').strip().lower() in (
    '1', 'true', 'yes', 'on'
)

_REC_KEY = '-RECORD-'


class RecordingPlugin(Plugin):
    name = 'recording'

    def __init__(self):
        self._session = None  # active RecordingSession or None

    # ── Main window ──────────────────────────────────────────────────────────

    def main_buttons(self) -> list:
        tip = ('Recording disabled by environment policy '
               '(JIRAMAXX_DISABLE_RECORDING)' if _DISABLED_BY_ENV
               else 'Start/stop meeting recording')
        return [sg.Button('⏺ Record', key=_REC_KEY, size=(10, 1),
                          font=('Helvetica', 8), button_color=('white', '#5a1a1a'),
                          disabled=_DISABLED_BY_ENV, tooltip=tip)]

    def handle_main_event(self, event, values, window, ctx: dict) -> bool:
        if event != _REC_KEY:
            return False
        config = ctx.get('config', {})

        if self._session is None:
            from .engine import RecordingSession
            try:
                new_session = RecordingSession(config)
                new_session.start()
                self._session = new_session
                window[_REC_KEY].update('⏹ Stop', button_color=('white', '#c62828'))
            except Exception as exc:
                import traceback
                show_error(f"Could not start recording:\n{exc}",
                           tb=traceback.format_exc(), title='Recording Error')
        else:
            window[_REC_KEY].update('Saving…', disabled=True)
            stop_thread = threading.Thread(
                target=self._session.stop,
                kwargs={'wait_for_transcription': True}, daemon=True)
            stop_thread.start()

            prog_layout = [
                [sg.Text('Finishing transcription…', font=('Helvetica', 11))],
                [sg.Text('This may take up to a minute.',
                         font=('Helvetica', 9, 'italic'))],
            ]
            prog_win = sg.Window('Saving Recording', prog_layout, modal=True,
                                 finalize=True, disable_close=True, keep_on_top=True)
            while stop_thread.is_alive():
                prog_win.read(timeout=200)
            prog_win.close()

            sg.popup_quick_message(
                f"Recording saved.\n"
                f"Transcript: {self._session.transcript_path}\n"
                f"Suggestions: {self._session.suggestions_dir}",
                auto_close_duration=4,
                background_color='#2e7d32', text_color='white',
            )
            self._session = None
            window[_REC_KEY].update('⏺ Record', disabled=False,
                                    button_color=('white', '#5a1a1a'))
        return True

    def on_main_window_close(self) -> None:
        if self._session is not None:
            self._session.stop(wait_for_transcription=False)
            self._session = None

    # ── Config window ────────────────────────────────────────────────────────

    def config_tab(self, config: dict):
        return sg.Tab('Recording', self._tab_layout(config))

    def _tab_layout(self, config: dict) -> list:
        if _DISABLED_BY_ENV:
            return [
                [sg.Text('Recording disabled by environment policy.',
                         font=('Helvetica', 11, 'bold'))],
                [sg.Text('JIRAMAXX_DISABLE_RECORDING is set in your environment.',
                         font=('Helvetica', 9))],
                [sg.Text('Contact your administrator to enable this feature.',
                         font=('Helvetica', 9, 'italic'))],
            ]

        rec = config.get('recording', {})
        keywords = list(rec.get('keywords', ['', '', '']))
        while len(keywords) < 3:
            keywords.append('')

        W_LBL, W_IN = 20, 26
        lang_tip = ('Multi-language support is not yet implemented — '
                    'let me know if you want this enabled.')
        return [
            [sg.Text('Audio Devices', font=('Helvetica', 10, 'bold'))],
            [sg.Text('Input (microphone)', size=(W_LBL, 1)),
             sg.Input(rec.get('input_device', ''), key='-REC-input_device-', size=(W_IN, 1)),
             sg.Button('Browse', key='-BROWSE-INPUT-', size=(7, 1))],
            [sg.Text('Output (loopback)', size=(W_LBL, 1)),
             sg.Input(rec.get('loopback_device', ''), key='-REC-loopback_device-', size=(W_IN, 1)),
             sg.Button('Browse', key='-BROWSE-LOOPBACK-', size=(7, 1))],
            [sg.HSep()],
            [sg.Text('Transcription', font=('Helvetica', 10, 'bold'))],
            [sg.Text('Language', size=(W_LBL, 1)),
             sg.Combo(['English', 'Spanish', 'French', 'German', 'Italian',
                       'Portuguese', 'Japanese', 'Mandarin'],
                      default_value='English', key='-REC-language-',
                      size=(W_IN - 2, 1), readonly=True, disabled=True,
                      tooltip=lang_tip)],
            [sg.HSep()],
            [sg.Text('Paths', font=('Helvetica', 10, 'bold'))],
            [sg.Text('Transcript directory', size=(W_LBL, 1)),
             sg.Input(rec.get('transcript_dir', '~/.jiramaxx/transcripts'),
                      key='-REC-transcript_dir-', size=(W_IN, 1)),
             sg.FolderBrowse('Browse', target='-REC-transcript_dir-', size=(7, 1))],
            [sg.Text('Suggestions directory', size=(W_LBL, 1)),
             sg.Input(rec.get('suggestions_dir', '~/.jiramaxx/suggestions'),
                      key='-REC-suggestions_dir-', size=(W_IN, 1)),
             sg.FolderBrowse('Browse', target='-REC-suggestions_dir-', size=(7, 1))],
            [sg.HSep()],
            [sg.Text('Keyword triggers  (up to 3 — saves surrounding 30s chunks as suggestions)',
                     font=('Helvetica', 10, 'bold'))],
            *[[sg.Text(f'Keyword {i + 1}', size=(W_LBL, 1)),
               sg.Input(keywords[i], key=f'-REC-keyword{i}-', size=(W_IN, 1))]
              for i in range(3)],
        ]

    def handle_config_event(self, event, values, window, working: dict) -> bool:
        if event not in ('-BROWSE-INPUT-', '-BROWSE-LOOPBACK-'):
            return False
        from .engine import list_devices
        loopbacks, inputs = list_devices()
        is_lb = event == '-BROWSE-LOOPBACK-'
        names = loopbacks if is_lb else inputs
        title = ('Loopback Devices (Teams audio output)' if is_lb
                 else 'Input Devices (microphone)')
        target_key = '-REC-loopback_device-' if is_lb else '-REC-input_device-'
        if not names:
            sg.popup(f"No {'loopback' if is_lb else 'input'} devices found.",
                     title=title, modal=True, keep_on_top=True)
            return True
        lay = [
            [sg.Text(title, font=('Helvetica', 10, 'bold'))],
            [sg.Listbox(names, size=(60, min(len(names) + 1, 8)),
                        key='-DEV-', select_mode='single', enable_events=True)],
            [sg.Button('Select', key='-SEL-'), sg.Button('Cancel', key='-CAN-')],
        ]
        dw = sg.Window(title, lay, finalize=True, modal=True)
        dw.bind('<Return>', '-SEL-')
        dw.bind('<Escape>', '-CAN-')
        bring_to_front(dw)
        de, dv = safe_read(dw)
        dw.close()
        if de in ('-SEL-', '-DEV-') and dv.get('-DEV-'):
            window[target_key].update(dv['-DEV-'][0])
        return True

    def collect_config(self, values, working: dict) -> None:
        if _DISABLED_BY_ENV:
            return
        existing = working.get('recording', {})
        section = {
            'input_device':    values.get('-REC-input_device-', ''),
            'loopback_device': values.get('-REC-loopback_device-', ''),
            'transcript_dir':  values.get('-REC-transcript_dir-', '~/.jiramaxx/transcripts'),
            'suggestions_dir': values.get('-REC-suggestions_dir-', '~/.jiramaxx/suggestions'),
            'keywords':        [values.get(f'-REC-keyword{i}-', '') for i in range(3)],
        }
        # Preserve a manually-set local model path override if present.
        if existing.get('model_path'):
            section['model_path'] = existing['model_path']
        working['recording'] = section
