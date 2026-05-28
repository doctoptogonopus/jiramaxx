from __future__ import annotations
import PySimpleGUI as sg
from .models import Ticket, TICKET_CLASSES, FIELD_META
from .cache import Cache
from .api import JiraClient
import traceback as _tb
from .utils import safe_read as _read, show_error, bring_to_front
from .recording import RECORDING_AVAILABLE, _DISABLED_BY_ENV as _RECORDING_DISABLED_BY_ENV

_LABEL_W = 22
_INPUT_W = 42
_MULTI_H = 5


def _fkey(field_name: str) -> str:
    return f'-FIELD-{field_name.upper()}-'


def _build_field_row(field_name: str, ticket: Ticket) -> list:
    meta = FIELD_META.get(field_name, {'type': 'text', 'label': field_name.replace('_', ' ').title()})
    required = field_name in ticket.required_fields
    label = f"{'*' if required else ' '} {meta['label']}:"
    val = str(getattr(ticket, field_name, '') or '')
    key = _fkey(field_name)

    if meta['type'] == 'multiline':
        return [sg.Text(label, size=(_LABEL_W, 1)),
                sg.Multiline(val, key=key, size=(_INPUT_W, _MULTI_H))]
    if meta['type'] == 'dropdown':
        opts = meta.get('options', [])
        default = val if val in opts else (opts[0] if opts else '')
        return [sg.Text(label, size=(_LABEL_W, 1)),
                sg.Combo(opts, default_value=default, key=key, size=(_INPUT_W - 2, 1), readonly=True)]
    if meta['type'] == 'spinner':
        opts = meta.get('values', list(range(1, 22)))
        try:
            default = int(val) if val else opts[0]
            default = default if default in opts else opts[0]
        except (ValueError, TypeError):
            default = opts[0]
        return [sg.Text(label, size=(_LABEL_W, 1)),
                sg.Spin(opts, initial_value=default, key=key, size=(6, 1))]
    # text / number fallback
    return [sg.Text(label, size=(_LABEL_W, 1)),
            sg.Input(val, key=key, size=(_INPUT_W, 1))]


# ─── Ticket form ────────────────────────────────────────────────────────────

def show_ticket_form(ticket: Ticket, cache: Cache, jira: JiraClient, config: dict) -> str:
    """Open a full ticket form. Returns 'submitted' | 'saved' | 'cancelled'."""
    heading = f"{'Edit' if ticket.summary else 'New'} {ticket.ticket_type}"
    layout = [
        [sg.Text(heading, font=('Helvetica', 13, 'bold'))],
        [sg.HSep()],
        *[_build_field_row(f, ticket) for f in ticket.all_form_fields()],
        [sg.HSep()],
        [sg.Text('* required', font=('Helvetica', 8))],
        [sg.Push(),
         sg.Button('Submit to Jira', key='-SUBMIT-', bind_return_key=False),
         sg.Button('Save Draft',     key='-SAVE-'),
         sg.Button('Cancel',         key='-CANCEL-')],
    ]
    window = sg.Window(f'Jira Tool – {ticket.ticket_type}', layout,
                       finalize=True, return_keyboard_events=False)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)

    def _tab_out(event):
        event.widget.tk_focusNext().focus()
        return 'break'
    def _shift_tab_out(event):
        event.widget.tk_focusPrev().focus()
        return 'break'
    window.bind('<Control-Return>', '-SUBMIT-')
    window.bind('<Control-s>', '-SAVE-')
    window.bind('<Control-S>', '-SAVE-')
    for _f in ticket.all_form_fields():
        if FIELD_META.get(_f, {}).get('type') == 'multiline':
            window[_fkey(_f)].Widget.bind('<Tab>', _tab_out)
            window[_fkey(_f)].Widget.bind('<Shift-Tab>', _shift_tab_out)

    _all_fields = ticket.all_form_fields()
    if _all_fields:
        window[_fkey(_all_fields[0])].set_focus()

    result = 'cancelled'
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break

        ticket.apply_form_values(values)

        if event == '-SAVE-':
            cache.save(ticket)
            sg.popup_quick_message('Draft saved.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')
            result = 'saved'
            break

        if event == '-SUBMIT-':
            valid, missing = ticket.is_valid()
            if not valid:
                show_error("Missing required fields:\n  " + '\n  '.join(missing),
                           title='Validation Error')
                continue
            type_ok, type_errors = ticket.validate_fields()
            if not type_ok:
                show_error("Field validation errors:\n  " + '\n  '.join(type_errors),
                           title='Validation Error')
                continue
            try:
                payload = ticket.to_jira_payload(config['jira']['project_key'])
                resp = jira.create_issue(payload)
                ticket.jira_key = resp.get('key')
                ticket.submitted = True
                cache.save(ticket)
                sg.popup_quick_message(f"Created {ticket.jira_key}!", auto_close_duration=2,
                                       background_color='#2e7d32', text_color='white')
                result = 'submitted'
                break
            except Exception as exc:
                show_error(f"Jira API error:\n{exc}", tb=_tb.format_exc(), title='Error')

    window.close()
    return result


# ─── Type selector ──────────────────────────────────────────────────────────

def show_type_selector() -> str | None:
    layout = [
        [sg.Text('Select ticket type', font=('Helvetica', 12, 'bold'))],
        *[[sg.Button(f'({t[0]}) {t}', key=t, size=(16, 2))] for t in TICKET_CLASSES],
        [sg.Button('Cancel', key='-CANCEL-', size=(16, 1))],
    ]
    window = sg.Window('New Ticket', layout, finalize=True, modal=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    for i, t in enumerate(TICKET_CLASSES, 1):
        window.bind(str(i), t)
        window.bind(t[0].lower(), t)
        window.bind(t[0].upper(), t)

    event, _ = _read(window)
    window.close()
    return event if event in TICKET_CLASSES else None


# ─── Draft list ─────────────────────────────────────────────────────────────

def show_draft_list(drafts: list[Ticket], cache: Cache) -> Ticket | None:
    """Show drafts, let user open or delete one. Returns the ticket to open, or None."""
    if not drafts:
        sg.popup('No drafts found.', title='Drafts', modal=True, keep_on_top=True)
        return None

    labels = [f"[{t.ticket_type:10s}]  {t.summary or '(no title)':40s}  {t.created_at[:10]}"
              for t in drafts]
    layout = [
        [sg.Text(f'{len(drafts)} incomplete draft(s)', font=('Helvetica', 12, 'bold'))],
        [sg.Listbox(labels, size=(72, min(len(drafts) + 1, 12)),
                    key='-LIST-', enable_events=True, select_mode=sg.LISTBOX_SELECT_MODE_SINGLE)],
        [sg.Push(),
         sg.Button('Open',   key='-OPEN-'),
         sg.Button('Delete', key='-DELETE-'),
         sg.Button('Cancel', key='-CANCEL-')],
    ]
    window = sg.Window('Drafts', layout, finalize=True,
                       return_keyboard_events=False, modal=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    window.bind('<Return>', '-OPEN-')

    result = None
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break

        sel = values.get('-LIST-')
        if not sel and event in ('-OPEN-', '-DELETE-'):
            sg.popup('Select a draft first.', modal=True, keep_on_top=True)
            continue

        if sel:
            idx = labels.index(sel[0])
            if event in ('-OPEN-', '-LIST-'):
                result = drafts[idx]
                break
            if event == '-DELETE-':
                if sg.popup_yes_no(f"Delete '{drafts[idx].summary or '(no title)'}'?") == 'Yes':
                    cache.delete(drafts[idx].ticket_id)
                    drafts.pop(idx)
                    labels.pop(idx)
                    window['-LIST-'].update(labels)
                    if not drafts:
                        break

    window.close()
    return result


# ─── Ticket interaction (comment / status change) ────────────────────────────

def show_interaction_window(jira: JiraClient, config: dict):
    try:
        issues = jira.get_active_sprint_issues(config['jira']['board_id'],
                                               config['jira']['project_key'])
    except Exception as exc:
        show_error(f"Could not fetch sprint tickets:\n{exc}", tb=_tb.format_exc())
        return

    if not issues:
        sg.popup('No active sprint tickets found.', modal=True, keep_on_top=True)
        return

    labels = [f"{i['key']:12s}  {i['fields']['summary']}" for i in issues]
    layout = [
        [sg.Text('Current Sprint Tickets', font=('Helvetica', 12, 'bold'))],
        [sg.Listbox(labels, size=(72, min(len(issues) + 1, 14)),
                    key='-LIST-', enable_events=False,
                    select_mode=sg.LISTBOX_SELECT_MODE_BROWSE)],
        [sg.Push(),
         sg.Button('(C) Add Comment',   key='-COMMENT-'),
         sg.Button('(S) Change Status', key='-STATUS-'),
         sg.Button('(X) Close',         key='-CANCEL-')],
    ]
    window = sg.Window('Manage Tickets', layout, finalize=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    for ch, ev in [('c', '-COMMENT-'), ('C', '-COMMENT-'),
                   ('s', '-STATUS-'),  ('S', '-STATUS-'),
                   ('x', '-CANCEL-'),  ('X', '-CANCEL-')]:
        window.bind(ch, ev)
    window['-LIST-'].update(set_to_index=[0])
    window['-LIST-'].Widget.activate(0)
    window['-LIST-'].Widget.focus_set()

    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break

        sel = values.get('-LIST-')
        if not sel:
            sg.popup('Select a ticket first.', modal=True, keep_on_top=True)
            continue

        idx = labels.index(sel[0])
        issue_key = issues[idx]['key']

        if event == '-COMMENT-':
            comment = sg.popup_get_text(f'Comment for {issue_key}:', title='Add Comment',
                                        size=(60, 1))
            if comment:
                try:
                    jira.add_comment(issue_key, comment)
                    sg.popup_quick_message('Comment added.', auto_close_duration=1,
                                           background_color='#2e7d32', text_color='white')
                except Exception as exc:
                    show_error(f"API error:\n{exc}", tb=_tb.format_exc())

        elif event == '-STATUS-':
            try:
                transitions = jira.get_transitions(issue_key)
                t_names = [t['name'] for t in transitions]
                layout_s = [
                    [sg.Text(f'Transitions for {issue_key}', font=('Helvetica', 11, 'bold'))],
                    [sg.Listbox(t_names, size=(40, min(len(t_names) + 1, 8)),
                                key='-T-', select_mode=sg.LISTBOX_SELECT_MODE_SINGLE)],
                    [sg.Button('Apply', key='-APPLY-'), sg.Button('Cancel', key='-TCANCEL-')],
                ]
                tw = sg.Window('Change Status', layout_s, finalize=True, modal=True)
                tw.bind('<Escape>', '-TCANCEL-')
                bring_to_front(tw)
                tw.bind('<Return>', '-APPLY-')
                tevt, tvals = _read(tw)
                tw.close()
                if tevt == '-APPLY-' and tvals.get('-T-'):
                    chosen_name = tvals['-T-'][0]
                    tid = next(t['id'] for t in transitions if t['name'] == chosen_name)
                    jira.transition_issue(issue_key, tid)
                    sg.popup_quick_message('Status updated.', auto_close_duration=1,
                                           background_color='#2e7d32', text_color='white')
            except Exception as exc:
                show_error(f"API error:\n{exc}", tb=_tb.format_exc())

    window.close()


# ─── Main window ─────────────────────────────────────────────────────────────

def run_main_window(cache: Cache, jira: JiraClient, config: dict,
                    config_path=None) -> dict:
    """Returns (possibly updated) config dict — may change after visiting Config."""
    from .config_ui import show_config_window
    from pathlib import Path
    if config_path is None:
        config_path = Path(__file__).parent / 'config.yaml'

    def _draft_msg(n: int) -> str:
        return f'{n} incomplete draft(s) — press D to view' if n else 'No pending drafts'

    drafts = cache.drafts()

    if RECORDING_AVAILABLE:
        _rec_tip = 'Start/stop meeting recording'
    elif _RECORDING_DISABLED_BY_ENV:
        _rec_tip = 'Recording disabled by environment policy (JIRAMAXX_DISABLE_RECORDING)'
    else:
        _rec_tip = 'Install jiramaxx[recording] to enable'
    layout = [
        [sg.Text('Jira Tool', font=('Helvetica', 16, 'bold'))],
        [sg.Text(_draft_msg(len(drafts)), key='-MSG-', font=('Helvetica', 10))],
        [sg.HSep()],
        [sg.Button('(N) New Ticket',     key='-NEW-',    size=(18, 2)),
         sg.Button('(D) View Drafts',    key='-DRAFTS-', size=(18, 2),
                   disabled=len(drafts) == 0)],
        [sg.Button('(M) Manage Tickets', key='-MANAGE-', size=(18, 2)),
         sg.Button('(C) Config',         key='-CONFIG-', size=(18, 2))],
        [sg.Button('(Q) Quit',           key='-QUIT-',   size=(38, 1))],
        [sg.Push(),
         sg.Button('⏺ Record', key='-RECORD-', size=(10, 1), font=('Helvetica', 8),
                   button_color=('white', '#5a1a1a'),
                   disabled=not RECORDING_AVAILABLE, tooltip=_rec_tip)],
    ]
    window = sg.Window('Jira Tool', layout, finalize=True)
    window.bind('<Escape>', '-QUIT-')
    bring_to_front(window)
    for ch, ev in [('n', '-NEW-'), ('N', '-NEW-'),
                   ('d', '-DRAFTS-'), ('D', '-DRAFTS-'),
                   ('m', '-MANAGE-'), ('M', '-MANAGE-'),
                   ('c', '-CONFIG-'), ('C', '-CONFIG-'),
                   ('q', '-QUIT-'),   ('Q', '-QUIT-')]:
        window.bind(ch, ev)

    _session = None  # active RecordingSession or None

    while True:
        event, _ = _read(window)

        if event in (sg.WIN_CLOSED, '-QUIT-'):
            if _session is not None:
                _session.stop(wait_for_transcription=False)
            break

        if event == '-RECORD-':
            if _session is None:
                from .recording import RecordingSession
                try:
                    _new_session = RecordingSession(config)
                    _new_session.start()
                    _session = _new_session
                    window['-RECORD-'].update('⏹ Stop',
                                              button_color=('white', '#c62828'))
                except Exception as exc:
                    show_error(f"Could not start recording:\n{exc}",
                               tb=_tb.format_exc(), title='Recording Error')
            else:
                import threading as _th
                window['-RECORD-'].update('Saving…', disabled=True)
                _stop_thread = _th.Thread(
                    target=_session.stop,
                    kwargs={'wait_for_transcription': True}, daemon=True)
                _stop_thread.start()

                _prog_layout = [
                    [sg.Text('Finishing transcription…',
                             font=('Helvetica', 11))],
                    [sg.Text('This may take up to a minute.',
                             font=('Helvetica', 9, 'italic'))],
                ]
                _prog_win = sg.Window('Saving Recording', _prog_layout,
                                      modal=True, finalize=True,
                                      disable_close=True, keep_on_top=True)
                while _stop_thread.is_alive():
                    _prog_win.read(timeout=200)
                _prog_win.close()

                sg.popup_quick_message(
                    f"Recording saved.\n"
                    f"Transcript: {_session.transcript_path}\n"
                    f"Suggestions: {_session.suggestions_dir}",
                    auto_close_duration=4,
                    background_color='#2e7d32', text_color='white',
                )
                _session = None
                window['-RECORD-'].update('⏺ Record', disabled=False,
                                          button_color=('white', '#5a1a1a'))

        elif event == '-NEW-':
            window.hide()
            ticket_type = show_type_selector()
            if ticket_type:
                ticket = TICKET_CLASSES[ticket_type]()
                show_ticket_form(ticket, cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-DRAFTS-':
            window.hide()
            drafts = cache.drafts()
            chosen = show_draft_list(drafts, cache)
            if chosen:
                show_ticket_form(chosen, cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-MANAGE-':
            window.hide()
            show_interaction_window(jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-CONFIG-':
            window.hide()
            updated = show_config_window(config, config_path)
            if updated:
                config = updated
                sg.theme(config.get('ui', {}).get('theme', 'DarkBlue3'))
                jcfg = config.get('jira', {})
                jira = JiraClient(
                    jcfg.get('base_url', ''),
                    jcfg.get('user_email', ''),
                    jcfg.get('api_token', ''),
                    token_type=jcfg.get('token_type', 'classic'),
                    cloud_id=jcfg.get('cloud_id', ''),
                )
                cache = Cache(config.get('cache', {}).get('directory', '~/.jira_tool/cache'))
            window.un_hide()
            bring_to_front(window)

        drafts = cache.drafts()
        window['-MSG-'].update(_draft_msg(len(drafts)))
        window['-DRAFTS-'].update(disabled=len(drafts) == 0)

    window.close()
    return config
