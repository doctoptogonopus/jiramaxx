import os
import PySimpleGUI as sg


def _subdirs(path: str) -> list:
    try:
        subs = sorted((d for d in os.listdir(path)
                       if os.path.isdir(os.path.join(path, d))), key=str.lower)
    except OSError:
        subs = []
    return ['..'] + subs


def pick_folder(window, target_key: str, current: str) -> None:
    """In-app folder picker (a PySimpleGUI window, NOT the native OS dialog).
    tkinter's askdirectory deadlocks in this app — its modal loop conflicts with
    the global keyboard hotkey hook — so we hand-roll the folder browser and avoid
    the native dialog entirely. On selection, writes the chosen path into
    ``window[target_key]``."""
    start = os.path.expanduser(current.strip()) if current.strip() else os.path.expanduser('~')
    cur = os.path.abspath(start if os.path.isdir(start) else os.path.expanduser('~'))

    layout = [
        [sg.Text('Current folder:', font=('Helvetica', 9, 'bold'))],
        [sg.Text(cur, key='-CURP-', size=(62, 1))],
        [sg.Listbox(_subdirs(cur), size=(64, 14), key='-DIRS-',
                    enable_events=True, select_mode='single')],
        [sg.Text('Or type a path:'),
         sg.Input(cur, key='-MANUAL-', size=(48, 1)),
         sg.Button('Go', key='-GO-')],
        [sg.Push(),
         sg.Button('Select This Folder', key='-PICK-'),
         sg.Button('Cancel', key='-CANCEL-')],
    ]
    win = sg.Window('Select folder', layout, modal=True, finalize=True, keep_on_top=True)
    win.bind('<Escape>', '-CANCEL-')
    bring_to_front(win)

    chosen = None
    while True:
        ev, vals = safe_read(win)
        if ev in (sg.WIN_CLOSED, '-CANCEL-'):
            break
        if ev == '-DIRS-' and vals.get('-DIRS-'):
            sel = vals['-DIRS-'][0]
            cur = os.path.abspath(os.path.dirname(cur) if sel == '..'
                                  else os.path.join(cur, sel))
            win['-CURP-'].update(cur)
            win['-MANUAL-'].update(cur)
            win['-DIRS-'].update(_subdirs(cur))
        elif ev == '-GO-':
            p = os.path.expanduser(vals.get('-MANUAL-', '').strip())
            if p and os.path.isdir(p):
                cur = os.path.abspath(p)
                win['-CURP-'].update(cur)
                win['-DIRS-'].update(_subdirs(cur))
            else:
                sg.popup('Not a folder.', keep_on_top=True)
        elif ev == '-PICK-':
            chosen = cur
            break
    win.close()
    if chosen:
        window[target_key].update(os.path.normpath(chosen))


def safe_read(window: sg.Window) -> tuple:
    """window.read() with KeyboardInterrupt treated as window close."""
    try:
        return window.read()
    except KeyboardInterrupt:
        return sg.WIN_CLOSED, {}


def bring_to_front(window: sg.Window):
    """Force a window to the top of the Z-order and grab focus (Windows-safe)."""
    try:
        window.TKroot.attributes('-topmost', True)
        window.TKroot.attributes('-topmost', False)
        window.bring_to_front()
        window.force_focus()
    except Exception:
        pass


def show_error(message: str, tb: str = None, title: str = 'Error'):
    """Error popup. If tb (traceback string) is provided, shows a 'Show Stack Trace' button."""
    _no_tb = ('NoneType: None', 'NoneType: None\n', '')
    has_tb = bool(tb and tb.strip() not in _no_tb)

    layout = [
        [sg.Text(message, text_color='#ff6b6b')],
        [sg.HSep()],
        [
            *([ sg.Button('Show Stack Trace', key='-TB-') ] if has_tb else []),
            sg.Button('OK', key='-OK-'),
        ],
    ]
    window = sg.Window(title, layout, finalize=True, modal=True, keep_on_top=True)
    bring_to_front(window)
    try:
        while True:
            event, _ = safe_read(window)
            if event in (sg.WIN_CLOSED, '-OK-'):
                break
            if event == '-TB-':
                sg.popup_scrolled(tb, title='Stack Trace', size=(90, 24),
                                  font=('Courier', 9), modal=True, keep_on_top=True)
    finally:
        window.close()
