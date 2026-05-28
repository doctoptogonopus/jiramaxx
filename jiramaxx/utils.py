import PySimpleGUI as sg


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
