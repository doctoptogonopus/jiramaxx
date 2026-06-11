from __future__ import annotations
from datetime import datetime
import os
import PySimpleGUI as sg
from .models import Ticket, Task, TICKET_CLASSES, FIELD_META
from .cache import Cache
from .api import JiraClient
import traceback as _tb
from .utils import safe_read as _read, show_error, bring_to_front, run_with_busy
from .plugins import discover_plugins

_LABEL_W = 22
_INPUT_W = 42
_MULTI_H = 5

_PRIORITY_RANK = {'Highest': 0, 'High': 1, 'Medium': 2, 'Low': 3, 'Lowest': 4}


def _fkey(field_name: str) -> str:
    return f'-FIELD-{field_name.upper()}-'


def _sc(config: dict, name: str, default: str) -> str:
    """Read a configurable in-window shortcut key (single character)."""
    return ((config.get('shortcuts') or {}).get(name) or default).strip() or default


def _epic_link_cf(config: dict) -> str:
    return ((config.get('jira', {}).get('custom_fields') or {}).get('epic_link')
            or 'customfield_10014')


# ── Issue field accessors (operate on raw Jira search results) ────────────────

def _i_priority(i: dict) -> str:
    return ((i.get('fields', {}).get('priority') or {}).get('name')) or ''


def _i_status(i: dict) -> str:
    return ((i.get('fields', {}).get('status') or {}).get('name')) or ''


def _i_assignee(i: dict) -> str:
    a = i.get('fields', {}).get('assignee')
    return (a.get('displayName') or '(unassigned)') if a else '(unassigned)'


def _i_assignee_email(i: dict) -> str:
    """Assignee email when Jira exposes it (profile visibility permitting); falls
    back to the display name, then '(unassigned)'."""
    a = i.get('fields', {}).get('assignee') or {}
    return a.get('emailAddress') or a.get('displayName') or '(unassigned)'


def _i_due(i: dict) -> str:
    return i.get('fields', {}).get('duedate') or ''


def _i_epic(i: dict, epic_cf: str) -> tuple[str | None, str | None]:
    """Resolve an issue's epic to (key, label). Prefers the parent (which carries
    the epic key + summary in the search response), else the epic-link custom
    field (key only). Returns (None, None) when there is no epic."""
    f = i.get('fields', {})
    parent = f.get('parent')
    if parent:
        key = parent.get('key')
        summ = (parent.get('fields', {}) or {}).get('summary', '')
        return key, f"{key}  {summ}".strip()
    el = f.get(epic_cf)
    if el:
        return str(el), str(el)
    return None, None


def _sort_issues(issues: list[dict], sort: str | None, descending: bool) -> list[dict]:
    if not sort:
        return issues  # API default order (updated DESC)
    keyfns = {
        'Priority': lambda i: _PRIORITY_RANK.get(_i_priority(i), 99),
        'Due date': lambda i: _i_due(i) or '9999-99-99',
        'Status':   lambda i: _i_status(i).lower(),
        'Assignee': lambda i: _i_assignee(i).lower(),
        'Key':      lambda i: i.get('key', ''),
    }
    fn = keyfns.get(sort)
    return sorted(issues, key=fn, reverse=descending) if fn else issues


def _busy_fetch(fn, what: str) -> tuple:
    """Run a Jira call behind the busy modal (see utils.run_with_busy) with the
    standard error popup. Returns (result, ok) — ok is False on error/cancel."""
    status, val, tb = run_with_busy(fn, message=f'{what}…')
    if status == 'ok':
        return val, True
    if status == 'error':
        show_error(f"{what} failed:\n{val}", tb=tb)
    return None, False


def _to_clipboard(window, text: str) -> None:
    try:
        window.TKroot.clipboard_clear()
        window.TKroot.clipboard_append(text)
        window.TKroot.update()
    except Exception:
        pass


def _soft_select(window, idx: int = 0, key: str = '-LIST-') -> None:
    """Pre-highlight a listbox row and give the list keyboard focus so arrow
    keys navigate immediately. ``idx`` is clamped to the current row count."""
    lst = window[key]
    n = len(lst.get_list_values())
    if n == 0:
        return
    idx = max(0, min(idx, n - 1))
    lst.update(set_to_index=[idx])
    lst.Widget.activate(idx)
    lst.Widget.focus_set()


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

def _title_cancel_prompt(title_text: str) -> str:
    """'Return' / 'save' / 'discard' when type selection is cancelled mid-flow.
    Returns 'return' | 'save' | 'discard'."""
    layout = [
        [sg.Text('Type selection cancelled.', font=('Helvetica', 10))],
        [sg.Text(f'Title: "{title_text[:50]}"', font=('Helvetica', 9))],
        [sg.HSep()],
        [sg.Button('Save Draft', key='-SAVE-'),
         sg.Button('Discard', key='-DISC-'),
         sg.Button('Return to form', key='-RET-')],
    ]
    w = sg.Window('Unsaved title', layout, finalize=True, modal=True,
                  keep_on_top=True, return_keyboard_events=False)
    w.bind('<Escape>', '-RET-')
    _enter_clicks_focused(w)
    bring_to_front(w)
    w['-RET-'].set_focus()
    ev, _ = _read(w)
    w.close()
    return {'-SAVE-': 'save', '-DISC-': 'discard'}.get(ev, 'return')


def _type_picker_dialog(current_type: str) -> str | None:
    """Arrow-key navigable ticket type picker. Returns the chosen type name or None."""
    types = list(TICKET_CLASSES.keys())
    cur_idx = types.index(current_type) if current_type in types else 0
    layout = [
        [sg.Text('Select ticket type', font=('Helvetica', 11, 'bold'))],
        [sg.Listbox(types, default_values=[types[cur_idx]], size=(30, len(types)),
                    key='-T-', select_mode=sg.LISTBOX_SELECT_MODE_BROWSE,
                    font=('Helvetica', 11))],
        [sg.Push(),
         sg.Button('Select', key='-OK-', bind_return_key=True),
         sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Change type', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    bring_to_front(w)
    w['-T-'].set_focus()
    # Make the tk listbox's *active* row the current type so the first arrow
    # press moves from it (selection alone doesn't set the active row).
    w['-T-'].Widget.activate(cur_idx)
    ev, vals = _read(w)
    w.close()
    if ev == '-OK-' and vals.get('-T-'):
        return vals['-T-'][0]
    return None


def _unsaved_changes_prompt() -> str:
    """Three-way modal for unsaved changes on form close. 'Save' means save a
    local draft — exiting should never force a Jira submit (which could bounce
    on validation). Returns 'save' | 'discard' | 'return'."""
    layout = [
        [sg.Text('You have unsaved changes.', font=('Helvetica', 10))],
        [sg.HSep()],
        [sg.Button('Save Draft', key='-SAVE-'),
         sg.Button('Discard', key='-DISC-'),
         sg.Button('Return to form', key='-RET-')],
    ]
    w = sg.Window('Unsaved changes', layout, finalize=True, modal=True,
                  keep_on_top=True, return_keyboard_events=False)
    w.bind('<Escape>', '-RET-')
    _enter_clicks_focused(w)
    bring_to_front(w)
    w['-RET-'].set_focus()
    ev, _ = _read(w)
    w.close()
    return {'-SAVE-': 'save', '-DISC-': 'discard'}.get(ev, 'return')


def _is_dirty(ticket: Ticket, orig: dict) -> bool:
    curr = ticket.to_dict()
    return any(str(curr.get(f, '') or '') != str(orig.get(f, '') or '')
               for f in curr)


def show_ticket_form(ticket: Ticket, cache: Cache, jira: JiraClient, config: dict) -> str:
    """Open a full ticket form. Returns 'submitted' | 'saved' | 'cancelled'."""
    restart_with = None

    heading = f"{'Edit' if ticket.summary else 'New'} {ticket.ticket_type}"
    layout = [
        [sg.Text(heading, font=('Helvetica', 13, 'bold'))],
        [sg.HSep()],
        *[_build_field_row(f, ticket) for f in ticket.all_form_fields()],
        [sg.HSep()],
        [sg.Text('* required', font=('Helvetica', 8))],
        [sg.Push(),
         sg.Button('Change Type', key='-CHTYPE-'),
         sg.Button('Submit to Jira',  key='-SUBMIT-', bind_return_key=False),
         sg.Button('Save Draft',      key='-SAVE-'),
         sg.Button('Cancel',          key='-CANCEL-')],
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
    # No single-letter hotkey for Change Type: the form is all text inputs, and
    # window-level letter binds fire even while an Input/Text has focus.
    for _f in ticket.all_form_fields():
        if FIELD_META.get(_f, {}).get('type') == 'multiline':
            window[_fkey(_f)].Widget.bind('<Tab>', _tab_out)
            window[_fkey(_f)].Widget.bind('<Shift-Tab>', _shift_tab_out)

    _all_fields = ticket.all_form_fields()
    if _all_fields:
        window[_fkey(_all_fields[0])].set_focus()

    # Snapshot for unsaved-changes detection (Change 4).
    _orig = ticket.to_dict()

    result = 'cancelled'
    while True:
        event, values = _read(window)

        # Apply form values at every iteration so ticket stays current (Change 4).
        if event not in (sg.WIN_CLOSED,):
            ticket.apply_form_values(values)

        if event == sg.WIN_CLOSED:
            # The window is already destroyed (X button), but `ticket` was kept
            # current by the apply-every-event loop — the work is still savable.
            if _is_dirty(ticket, _orig) and _yn_dialog(
                    'Save your changes as a draft?', title='Unsaved changes'):
                cache.save(ticket)
                result = 'saved'
            break

        if event == '-CANCEL-':
            if _is_dirty(ticket, _orig):
                action = _unsaved_changes_prompt()
                if action == 'return':
                    continue
                if action == 'save':
                    cache.save(ticket)
                    result = 'saved'
                break
            break

        if event == '-CHTYPE-':
            new_type = _type_picker_dialog(ticket.ticket_type)
            if new_type and new_type != ticket.ticket_type:
                new_ticket = TICKET_CLASSES[new_type]()
                for f in ('summary', 'description', 'priority', 'labels'):
                    setattr(new_ticket, f, getattr(ticket, f, '') or '')
                restart_with = new_ticket
                break
            continue

        if event == '-SAVE-':
            cache.save(ticket)
            sg.popup_quick_message('Draft saved.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')
            result = 'saved'
            break

        if event == '-SUBMIT-':
            # "me" assignee needs a configured account ID — keep the form open
            # so the user can fix the field or save a draft (nothing is lost).
            if ((getattr(ticket, 'assignee', '') or '').strip().lower() == 'me'
                    and not ((config.get('jira') or {}).get('my_account_id') or '').strip()):
                show_error(
                    'Account ID is not configured.\n\n'
                    'Go to Settings → Jira Settings and enter your Jira Account ID\n'
                    'before assigning tickets to yourself with "me".',
                    title='Missing Account ID')
                continue
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

    # Change 5: type-change restart (tail-call).
    if restart_with is not None:
        return show_ticket_form(restart_with, cache, jira, config)

    return result


# ─── Type selector ──────────────────────────────────────────────────────────

def show_type_selector() -> str | None:
    layout = [
        [sg.Text('Select ticket type', font=('Helvetica', 12, 'bold'))],
        *[[sg.Button(f'({t[0]}) {t}', key=t, size=(16, 2))] for t in TICKET_CLASSES],
        [sg.Button('Cancel', key='-CANCEL-', size=(16, 1))],
    ]
    window = sg.Window('New Ticket', layout, finalize=True, modal=True, keep_on_top=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    for i, t in enumerate(TICKET_CLASSES, 1):
        window.bind(str(i), t)
        window.bind(t[0].lower(), t)
        window.bind(t[0].upper(), t)

    event, _ = _read(window)
    window.close()
    return event if event in TICKET_CLASSES else None


# ─── Title-first new ticket flow (also powers "Create Subtask") ───────────────

def show_new_ticket_flow(cache: Cache, jira: JiraClient, config: dict,
                         parent_key: str | None = None,
                         initial_title: str = '') -> None:
    """Title-first creation: one title field, then Save (quick-save a Task draft),
    Edit (pick a type → full form), or Cancel. ``parent_key`` makes the result a
    subtask of that issue (carried via Ticket.parent into the Jira payload).
    ``initial_title`` pre-fills the field (set when 'Return to form' re-enters
    after a cancelled type selection, so the typed title isn't lost)."""
    heading = f'New Subtask of {parent_key}' if parent_key else 'New Ticket'
    layout = [
        [sg.Text(heading, font=('Helvetica', 13, 'bold'))],
        [sg.Text('Title:', size=(6, 1)), sg.Input(initial_title, key='-TITLE-', size=(50, 1))],
        [sg.HSep()],
        [sg.Push(),
         sg.Button('Save (Ctrl+S)', key='-SAVE-'),
         sg.Button('Edit (Enter)',  key='-EDIT-'),
         sg.Button('Cancel',        key='-CANCEL-')],
    ]
    window = sg.Window('New Ticket', layout, finalize=True, modal=True,
                       keep_on_top=True, return_keyboard_events=False)
    window.bind('<Escape>', '-CANCEL-')
    window.bind('<Return>', '-EDIT-')
    window.bind('<Control-s>', '-SAVE-')
    window.bind('<Control-S>', '-SAVE-')
    bring_to_front(window)
    window['-TITLE-'].set_focus()

    action, title = None, ''
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break
        title = (values.get('-TITLE-') or '').strip()
        if event in ('-SAVE-', '-EDIT-'):
            if not title:
                sg.popup('Enter a title first.', modal=True, keep_on_top=True)
                continue
            action = 'save' if event == '-SAVE-' else 'edit'
            break
    window.close()

    if action == 'save':
        ticket = Task()
        ticket.summary = title
        if parent_key:
            ticket.parent = parent_key
        cache.save(ticket)
        sg.popup_quick_message('Draft saved.', auto_close_duration=1,
                               background_color='#2e7d32', text_color='white')
    elif action == 'edit':
        ticket_type = show_type_selector()
        if ticket_type:
            ticket = TICKET_CLASSES[ticket_type]()
            ticket.summary = title
            if parent_key:
                ticket.parent = parent_key
            show_ticket_form(ticket, cache, jira, config)
        else:
            # Type selection was cancelled — ask user what to do with the title.
            decision = _title_cancel_prompt(title)
            if decision == 'save':
                ticket = Task()
                ticket.summary = title
                if parent_key:
                    ticket.parent = parent_key
                cache.save(ticket)
                sg.popup_quick_message('Draft saved.', auto_close_duration=1,
                                       background_color='#2e7d32', text_color='white')
            elif decision == 'discard':
                return
            else:
                # 'return' — re-enter the title form with the title preserved.
                show_new_ticket_flow(cache, jira, config, parent_key=parent_key,
                                     initial_title=title)


# ─── Draft list ─────────────────────────────────────────────────────────────

_DRAFT_ORDERS = ['Created (newest)', 'Created (oldest)', 'Type', 'Summary']


def _draft_label(t: Ticket) -> str:
    # Truncate the summary to a fixed width so the trailing date column always
    # lines up (rendered in a monospace font — see the Listbox below).
    summ = t.summary or '(no title)'
    summ = (summ[:39] + '…') if len(summ) > 40 else summ
    return f"[{t.ticket_type:10s}]  {summ:40s}  {t.created_at[:10]}"


def _sort_drafts(drafts: list[Ticket], order: str) -> list[Ticket]:
    if order == 'Created (oldest)':
        return sorted(drafts, key=lambda t: t.created_at)
    if order == 'Type':
        return sorted(drafts, key=lambda t: (t.ticket_type, t.created_at))
    if order == 'Summary':
        return sorted(drafts, key=lambda t: (t.summary or '').lower())
    return sorted(drafts, key=lambda t: t.created_at, reverse=True)  # newest first


def _enter_clicks_focused(w: sg.Window) -> None:
    """Make <Return> activate the *focused* button (tk buttons only honor
    <Space> natively), so Tab→Enter keyboard flows work in confirmation
    dialogs. Only for button-only dialogs — don't use where Inputs need Enter."""
    def _h(e):
        if hasattr(e.widget, 'invoke'):
            e.widget.invoke()
            return 'break'
    w.TKroot.bind('<Return>', _h)


def _yn_dialog(message: str, title: str = '') -> bool:
    """Yes/No confirmation with keyboard support.
    Enter fires the focused button (Yes by default); Tab moves focus to No."""
    layout = [
        [sg.Text(message, font=('Helvetica', 10))],
        [sg.Push(),
         sg.Button('Yes', key='-YES-'),
         sg.Button('No', key='-NO-')],
    ]
    w = sg.Window(title or 'Confirm', layout, finalize=True, modal=True,
                  keep_on_top=True, return_keyboard_events=False)
    w.bind('<Escape>', '-NO-')
    _enter_clicks_focused(w)
    bring_to_front(w)
    w['-YES-'].set_focus()
    ev, _ = _read(w)
    w.close()
    return ev == '-YES-'


def _order_popup(options: list[str], current: str) -> str | None:
    """Tiny modal to pick a sort order. Returns the chosen option or None."""
    layout = [
        [sg.Text('Order by', font=('Helvetica', 12, 'bold'))],
        [sg.Combo(options, default_value=current, key='-O-', readonly=True, size=(20, 1))],
        [sg.Push(), sg.Button('Apply', key='-A-'), sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Order', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    w.bind('<Return>', '-A-')
    bring_to_front(w)
    ev, vals = _read(w)
    w.close()
    return vals.get('-O-') if ev == '-A-' else None


def _comment_popup(issue_key: str) -> str | None:
    """Single-line comment prompt. Returns the text, or None if cancelled."""
    layout = [
        [sg.Text(f'Comment for {issue_key}:', font=('Helvetica', 11, 'bold'))],
        [sg.Input('', key='-CMT-', size=(60, 1))],
        [sg.Push(), sg.Button('Add', key='-OK-'), sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Add Comment', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    w.bind('<Return>', '-OK-')
    bring_to_front(w)
    w['-CMT-'].set_focus()
    ev, vals = _read(w)
    w.close()
    if ev == '-OK-':
        return (vals.get('-CMT-') or '').strip() or None
    return None


def show_draft_list(drafts: list[Ticket], cache: Cache,
                    config: dict | None = None) -> Ticket | None:
    """Show drafts, let user open/delete/reorder one. Returns the ticket to open."""
    if not drafts:
        sg.popup('No drafts found.', title='Drafts', modal=True, keep_on_top=True)
        return None

    order = _DRAFT_ORDERS[0]  # newest first by default
    drafts = _sort_drafts(list(drafts), order)
    labels = [_draft_label(t) for t in drafts]
    layout = [
        [sg.Text(f'{len(drafts)} incomplete draft(s)', font=('Helvetica', 12, 'bold')),
         sg.Push(),
         sg.Button('☰', key='-ORDER-', size=(3, 1), tooltip='Sort order')],
        [sg.Listbox(labels, size=(72, min(len(drafts) + 1, 12)),
                    key='-LIST-', enable_events=False, font=('Consolas', 10),
                    tooltip='Ctrl/Shift-click to select several (for Delete)',
                    select_mode=sg.LISTBOX_SELECT_MODE_EXTENDED)],
        [sg.Push(),
         sg.Button('Open',   key='-OPEN-'),
         sg.Button('Delete', key='-DELETE-'),
         sg.Button('Cancel', key='-CANCEL-')],
    ]
    window = sg.Window('Drafts', layout, finalize=True,
                       return_keyboard_events=False, modal=True, keep_on_top=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    window.bind('<Return>', '-OPEN-')
    _soft_select(window, 0)

    result = None
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break

        if event == '-ORDER-':
            chosen = _order_popup(_DRAFT_ORDERS, order)
            if chosen and chosen != order:
                order = chosen
                drafts = _sort_drafts(drafts, order)
                labels = [_draft_label(t) for t in drafts]
                window['-LIST-'].update(labels)
                _soft_select(window, 0)
            continue

        if event in ('-OPEN-', '-DELETE-'):
            # get_indexes() maps selection straight to positions (robust even when
            # two drafts share an identical label).
            idxs = sorted(window['-LIST-'].get_indexes())
            if not idxs:
                sg.popup('Select a draft first.', modal=True, keep_on_top=True)
                continue

            if event == '-OPEN-':
                result = drafts[idxs[0]]  # open the first selected
                break

            if event == '-DELETE-':
                n = len(idxs)
                prompt = (f'Delete these {n} drafts?' if n > 1
                          else f"Delete '{drafts[idxs[0]].summary or '(no title)'}'?")
                # keep_on_top so the confirmation sits above the (keep-on-top) list.
                if _yn_dialog(prompt, title='Delete drafts'):
                    for i in reversed(idxs):  # delete high→low to keep indices valid
                        cache.delete(drafts[i].ticket_id)
                        drafts.pop(i)
                        labels.pop(i)
                    window['-LIST-'].update(labels)
                    if not drafts:
                        break
                    _soft_select(window, min(idxs[0], len(drafts) - 1))

    window.close()
    return result


# ─── Ticket interaction (comment / status / subtask / release) ───────────────

def _change_status(jira: JiraClient, issue_key: str) -> bool:
    """Modal transition picker for one issue. Returns True if a transition applied."""
    try:
        transitions = jira.get_transitions(issue_key)
    except Exception as exc:
        show_error(f"API error:\n{exc}", tb=_tb.format_exc())
        return False
    t_names = [t['name'] for t in transitions]
    if not t_names:
        sg.popup('No transitions available.', modal=True, keep_on_top=True)
        return False
    layout_s = [
        [sg.Text(f'Transitions for {issue_key}', font=('Helvetica', 11, 'bold'))],
        [sg.Listbox(t_names, size=(40, min(len(t_names) + 1, 8)),
                    key='-T-', select_mode=sg.LISTBOX_SELECT_MODE_SINGLE)],
        [sg.Button('Apply', key='-APPLY-'), sg.Button('Cancel', key='-TCANCEL-')],
    ]
    tw = sg.Window('Change Status', layout_s, finalize=True, modal=True, keep_on_top=True)
    tw.bind('<Escape>', '-TCANCEL-')
    bring_to_front(tw)
    tw.bind('<Return>', '-APPLY-')
    tevt, tvals = _read(tw)
    tw.close()
    if tevt == '-APPLY-' and tvals.get('-T-'):
        try:
            chosen_name = tvals['-T-'][0]
            tid = next(t['id'] for t in transitions if t['name'] == chosen_name)
            jira.transition_issue(issue_key, tid)
            sg.popup_quick_message('Status updated.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')
            return True
        except Exception as exc:
            show_error(f"API error:\n{exc}", tb=_tb.format_exc())
    return False


def _bulk_transition(jira: JiraClient, issues: list[dict],
                     target_status: str) -> tuple[int, list[str]]:
    """Transition every issue to ``target_status``. Returns (ok_count, failures)."""
    ok, failures, tl = 0, [], target_status.lower()
    for i in issues:
        key = i['key']
        try:
            trans = jira.get_transitions(key)
            # Exact (case-insensitive) name match only — a substring fallback can
            # silently pick the wrong transition (e.g. "Done" inside "Not Done").
            match = next((t for t in trans if t['name'].lower() == tl), None)
            if match is None:
                avail = ', '.join(t['name'] for t in trans) or 'none'
                failures.append(f"{key}: no '{target_status}' transition "
                                f"(available: {avail})")
                continue
            jira.transition_issue(key, match['id'])
            ok += 1
        except Exception as exc:
            failures.append(f"{key}: {exc}")
    return ok, failures


def _bulk_done_popup(issues: list[dict], done_status: str) -> list[dict] | None:
    """Checklist to confirm/trim which tickets move to ``done_status``. All start
    checked; the user unchecks anything not actually complete. Returns the chosen
    issues, or None if cancelled."""
    rows = [[sg.Checkbox(f"{i['key']}  {i['fields']['summary']}", default=True,
                         key=f"-CB-{i['key']}-")] for i in issues]
    body = sg.Column(rows, scrollable=len(issues) > 12, vertical_scroll_only=True,
                     size=(560, min(len(issues) * 24 + 12, 340)))
    layout = [
        [sg.Text(f'Move selected tickets to "{done_status}"',
                 font=('Helvetica', 12, 'bold'))],
        [sg.Text('All are checked by default — uncheck any that are not complete.',
                 font=('Helvetica', 9))],
        [body],
        [sg.Push(),
         sg.Button('Move selected', key='-GO-'),
         sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Bulk → Done', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    bring_to_front(w)
    ev, vals = _read(w)
    w.close()
    if ev != '-GO-':
        return None
    return [i for i in issues if vals.get(f"-CB-{i['key']}-")]


def _manage_options_popup(state: dict) -> bool:
    """Sort + epic-grouping options for the Manage window. Returns True if applied."""
    sort_opts = ['(Default: recent)', 'Priority', 'Due date', 'Status', 'Assignee', 'Key']
    cur = state['sort'] or '(Default: recent)'
    layout = [
        [sg.Text('View options', font=('Helvetica', 12, 'bold'))],
        [sg.Text('Sort by:', size=(9, 1)),
         sg.Combo(sort_opts, default_value=cur, key='-SORT-', readonly=True, size=(18, 1))],
        [sg.Checkbox('Descending', default=state['desc'], key='-DESC-')],
        [sg.Checkbox('Group by epic', default=(state['view'] == 'tree'), key='-GROUP-')],
        [sg.Push(), sg.Button('Apply', key='-APPLY-'), sg.Button('Cancel', key='-OCANCEL-')],
    ]
    w = sg.Window('Options', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-OCANCEL-')
    w.bind('<Return>', '-APPLY-')
    bring_to_front(w)
    ev, vals = _read(w)
    w.close()
    if ev != '-APPLY-':
        return False
    s = vals['-SORT-']
    state['sort'] = None if s.startswith('(Default') else s
    state['desc'] = bool(vals['-DESC-'])
    state['view'] = 'tree' if vals['-GROUP-'] else 'flat'
    return True


def _selected_key(values: dict, state: dict, row_keys: list[str]) -> str | None:
    """Resolve the selected issue key in either flat (table) or tree view. Epic
    header rows (keys prefixed 'EPIC::') are treated as no selection."""
    if state['view'] == 'tree':
        for k in (values.get('-TREE-') or []):
            if not str(k).startswith('EPIC::'):
                return k
        return None
    rows = values.get('-TABLE-') or []
    if rows and 0 <= rows[0] < len(row_keys):
        return row_keys[rows[0]]
    return None


def _show_release_view(cache: Cache, jira: JiraClient, config: dict) -> None:
    """Release-coordinator mode: all users' tickets in the configured release status,
    with copy-keys / copy-users / bulk-move-to-done."""
    proj = config['jira']['project_key']
    epic_cf = _epic_link_cf(config)
    rel = config.get('release') or {}
    filter_status = (rel.get('filter_status') or '').strip()
    done_status = (rel.get('done_status') or '').strip()
    if not filter_status or not done_status:
        show_error('Release statuses are not configured.\n\nSet a Pre-Release Status and a '
                   'Completed status in Config → App Settings → Release settings, then click '
                   '"Test statuses".', title='Release not configured')
        return

    def _fetch() -> list[dict]:
        items = jira.get_sprint_issues(proj, mine=False, status=filter_status,
                                       epic_link_cf=epic_cf)
        # Guard against a status name the JQL matched loosely.
        return [i for i in items if _i_status(i).lower() == filter_status.lower()]

    issues, ok = _busy_fetch(_fetch, 'Fetching release tickets')
    if not ok:
        return

    while True:  # rebuild after update / bulk move
        emails = sorted({_i_assignee_email(i) for i in issues})
        labels = [f"{i['key']:12s} {_i_assignee(i):22s} {i['fields']['summary']}"
                  for i in issues]
        layout = [
            [sg.Text(f'Release mode — status "{filter_status}"',
                     font=('Helvetica', 12, 'bold'))],
            [sg.Text(f'{len(issues)} ticket(s) · {len(emails)} user(s)')],
            [sg.Listbox(labels, size=(80, min(len(issues) + 1, 16)), key='-RLIST-',
                        select_mode=sg.LISTBOX_SELECT_MODE_BROWSE)],
            [sg.Push(),
             sg.Button('Copy Keys',  key='-CKEYS-'),
             sg.Button('Copy Users', key='-CUSERS-'),
             sg.Button(f'Bulk → {done_status}', key='-BULK-'),
             sg.Button('Update', key='-RUPDATE-'),
             sg.Button('Back',   key='-BACK-')],
        ]
        window = sg.Window('Release Mode', layout, finalize=True, modal=True, keep_on_top=True)
        window.bind('<Escape>', '-BACK-')
        bring_to_front(window)
        if issues:
            _soft_select(window, 0, key='-RLIST-')

        rebuild = False
        while not rebuild:
            event, values = _read(window)
            if event in (sg.WIN_CLOSED, '-BACK-'):
                window.close()
                return
            if event == '-CKEYS-':
                _to_clipboard(window, ', '.join(i['key'] for i in issues))
                sg.popup_quick_message('Keys copied.', auto_close_duration=1,
                                       background_color='#2e7d32', text_color='white')
            elif event == '-CUSERS-':
                _to_clipboard(window, ', '.join(emails))
                sg.popup_quick_message('Users copied.', auto_close_duration=1,
                                       background_color='#2e7d32', text_color='white')
            elif event == '-RUPDATE-':
                fresh, ok = _busy_fetch(_fetch, 'Updating')
                if ok:
                    issues = fresh
                rebuild = True
            elif event == '-BULK-':
                if not issues:
                    sg.popup('Nothing to move.', modal=True, keep_on_top=True)
                    continue
                selected = _bulk_done_popup(issues, done_status)
                if not selected:
                    continue
                result, ok = _busy_fetch(
                    lambda: _bulk_transition(jira, selected, done_status),
                    'Transitioning tickets')
                if not ok:
                    continue
                moved, failures = result
                msg = f"Moved {moved} ticket(s) to {done_status}."
                if failures:
                    msg += f"\n\n{len(failures)} failed:\n  " + '\n  '.join(failures)
                sg.popup(msg, title='Bulk transition', modal=True, keep_on_top=True)
                # Best-effort refresh; the bulk popup already reported the outcome.
                status, fresh, _ = run_with_busy(_fetch, message='Refreshing…')
                if status == 'ok':
                    issues = fresh
                rebuild = True
        window.close()


def show_interaction_window(cache: Cache, jira: JiraClient, config: dict):
    """Manage the active sprint's tickets assigned to me. Opens from the on-disk
    snapshot (Update refetches); supports sort + epic grouping (Options), subtask
    creation, and release-coordinator mode."""
    proj = config['jira']['project_key']
    epic_cf = _epic_link_cf(config)

    def _fetch() -> list[dict]:
        return jira.get_sprint_issues(proj, mine=True, epic_link_cf=epic_cf)

    snap = cache.load_sprint_issues()
    if snap is None:
        issues, ok = _busy_fetch(_fetch, 'Fetching sprint tickets')
        if not ok:
            return
        cache.save_sprint_issues(issues)
        fetched = 'just now'
    else:
        issues = snap['issues']
        fetched = (snap.get('fetched_at') or '')[:19].replace('T', ' ')

    state = {'issues': issues, 'view': 'flat', 'sort': None, 'desc': False,
             'fetched': fetched}

    k_comment = _sc(config, 'comment', 'c')
    k_status  = _sc(config, 'status', 's')
    k_subtask = _sc(config, 'subtask', 't')
    k_update  = _sc(config, 'update', 'u')

    while True:  # (re)build the window after Update / Options
        ordered = _sort_issues(state['issues'], state['sort'], state['desc'])
        sub_line = f"updated {state['fetched']}" if state['fetched'] else ''
        row_keys = [i['key'] for i in ordered]

        if state['view'] == 'tree':
            td = sg.TreeData()
            groups: dict = {}
            group_order: list = []
            for i in ordered:
                ek, elabel = _i_epic(i, epic_cf)
                gk = ek or '__none__'
                if gk not in groups:
                    groups[gk] = [elabel or '(No Epic)', []]
                    group_order.append(gk)
                groups[gk][1].append(i)
            for gk in group_order:
                glabel, its = groups[gk]
                td.insert('', f'EPIC::{gk}', glabel, values=['', ''])
                for i in its:
                    td.insert(f'EPIC::{gk}', i['key'], f"{i['key']}  {i['fields']['summary']}",
                              values=[_i_status(i), _i_due(i)])
            n_rows = min(len(ordered) + len(group_order) + 1, 18)
            list_elem = sg.Tree(data=td, headings=['Status', 'Due'],
                                col0_heading='Epic / Ticket', col0_width=46,
                                col_widths=[12, 10], auto_size_columns=False,
                                key='-TREE-', num_rows=max(n_rows, 4),
                                select_mode=sg.TABLE_SELECT_MODE_BROWSE,
                                show_expanded=True, enable_events=False, justification='left')
        else:
            table_rows = []
            for i in ordered:
                ek, _ = _i_epic(i, epic_cf)
                table_rows.append([i['key'], i['fields']['summary'],
                                   ek or '', _i_due(i)])
            list_elem = sg.Table(values=table_rows,
                                 headings=['Key', 'Summary', 'Epic', 'Due'],
                                 col_widths=[11, 40, 12, 11], auto_size_columns=False,
                                 justification='left', key='-TABLE-',
                                 num_rows=min(len(ordered) + 1, 16),
                                 select_mode=sg.TABLE_SELECT_MODE_BROWSE,
                                 enable_events=False, expand_x=True,
                                 hide_vertical_scroll=len(ordered) <= 16)

        _release_ok = bool((config.get('release') or {}).get('validated'))
        layout = [
            [sg.Text(f'Current Sprint — mine ({len(ordered)})',
                     font=('Helvetica', 12, 'bold')),
             sg.Push(), sg.Text(sub_line, font=('Helvetica', 8)),
             sg.Button('Release', key='-RELEASE-', size=(10, 1),
                       disabled=not _release_ok,
                       tooltip=None if _release_ok else
                       'Validate the release statuses in Config → Release settings first'),
             sg.Button('☰', key='-OPTIONS-', size=(3, 1), tooltip='Sort / group options')],
            [list_elem],
            [sg.Push(),
             sg.Button(f'({k_comment.upper()}) Comment', key='-COMMENT-'),
             sg.Button(f'({k_status.upper()}) Status',   key='-STATUS-'),
             sg.Button(f'({k_subtask.upper()}) Subtask', key='-SUBTASK-'),
             sg.Button(f'({k_update.upper()}) Update',   key='-UPDATE-'),
             sg.Button('(X) Close', key='-CANCEL-')],
        ]
        window = sg.Window('Manage Tickets', layout, finalize=True)
        window.bind('<Escape>', '-CANCEL-')
        bring_to_front(window)
        for ch, ev in [(k_comment, '-COMMENT-'), (k_status, '-STATUS-'),
                       (k_subtask, '-SUBTASK-'), (k_update, '-UPDATE-'),
                       ('x', '-CANCEL-')]:
            window.bind(ch.lower(), ev)
            window.bind(ch.upper(), ev)
        if ordered:
            elem = window['-TABLE-' if state['view'] == 'flat' else '-TREE-']
            if state['view'] == 'flat':
                elem.update(select_rows=[0])
            try:
                elem.Widget.focus_set()
                # Arrow keys need the Treeview focus *item*, not just widget focus.
                if state['view'] == 'flat':
                    kids = elem.Widget.get_children()
                    if kids:
                        elem.Widget.focus(kids[0])
                else:
                    tops = elem.Widget.get_children()
                    if tops:
                        leaves = elem.Widget.get_children(tops[0])
                        first = leaves[0] if leaves else tops[0]
                        elem.Widget.focus(first)
                        elem.Widget.selection_set(first)
            except Exception:
                pass

        rebuild = False
        while not rebuild:
            event, values = _read(window)
            if event in (sg.WIN_CLOSED, '-CANCEL-'):
                window.close()
                return

            if event == '-UPDATE-':
                fresh, ok = _busy_fetch(_fetch, 'Updating sprint tickets')
                if ok:
                    state['issues'] = fresh
                    cache.save_sprint_issues(fresh)
                    state['fetched'] = datetime.now().isoformat()[:19].replace('T', ' ')
                rebuild = True
                continue

            if event == '-OPTIONS-':
                if _manage_options_popup(state):
                    rebuild = True
                continue

            if event == '-RELEASE-':
                window.hide()
                _show_release_view(cache, jira, config)
                window.un_hide()
                bring_to_front(window)
                continue

            issue_key = _selected_key(values, state, row_keys)
            if not issue_key:
                sg.popup('Select a ticket first.', modal=True, keep_on_top=True)
                continue

            if event == '-COMMENT-':
                comment = _comment_popup(issue_key)
                if comment:
                    try:
                        jira.add_comment(issue_key, comment)
                        sg.popup_quick_message('Comment added.', auto_close_duration=1,
                                               background_color='#2e7d32', text_color='white')
                    except Exception as exc:
                        show_error(f"API error:\n{exc}", tb=_tb.format_exc())
            elif event == '-STATUS-':
                _change_status(jira, issue_key)
            elif event == '-SUBTASK-':
                window.hide()
                show_new_ticket_flow(cache, jira, config, parent_key=issue_key)
                window.un_hide()
                bring_to_front(window)

        window.close()


# ─── Main window ─────────────────────────────────────────────────────────────

def run_main_window(cache: Cache, jira: JiraClient, config: dict,
                    config_path=None) -> dict:
    """Returns (possibly updated) config dict — may change after visiting Config."""
    from .config_ui import show_config_window
    if config_path is None:
        from .main import CONFIG_PATH
        config_path = CONFIG_PATH

    def _draft_msg(n: int) -> str:
        return f'{n} incomplete draft(s) — press D to view' if n else 'No pending drafts'

    drafts = cache.drafts()

    # Per-deployment lever: hide the initiative planner when this env var is set
    # (mirrors the recording plugin's JIRAMAXX_DISABLE_RECORDING).
    planner_enabled = not os.environ.get('JIRAMAXX_DISABLE_PLANNER')

    plugins = discover_plugins()
    # Plugins (e.g. jiramaxx-recording) contribute buttons here; if none are
    # installed this row is empty and is omitted from the layout entirely.
    plugin_buttons = [b for p in plugins for b in p.main_buttons()]

    layout = [
        [sg.Text('Jira Tool', font=('Helvetica', 16, 'bold'))],
        [sg.Text(_draft_msg(len(drafts)), key='-MSG-', font=('Helvetica', 10))],
        [sg.HSep()],
        [sg.Button('(N) New Ticket',     key='-NEW-',    size=(18, 2)),
         sg.Button('(D) View Drafts',    key='-DRAFTS-', size=(18, 2),
                   disabled=len(drafts) == 0)],
        [sg.Button('(M) Manage Tickets', key='-MANAGE-', size=(18, 2)),
         sg.Button('(C) Config',         key='-CONFIG-', size=(18, 2))],
        *([[sg.Button('(G) Plan Initiative', key='-PLAN-',  size=(38, 1))]]
          if planner_enabled else []),
        [sg.Button('(Q) Quit',           key='-QUIT-',   size=(38, 1))],
    ]
    if plugin_buttons:
        layout.append([sg.Push(), *plugin_buttons])
    window = sg.Window('Jira Tool', layout, finalize=True)
    window.bind('<Escape>', '-QUIT-')
    bring_to_front(window)
    bindings = [('n', '-NEW-'), ('N', '-NEW-'),
                ('d', '-DRAFTS-'), ('D', '-DRAFTS-'),
                ('m', '-MANAGE-'), ('M', '-MANAGE-'),
                ('c', '-CONFIG-'), ('C', '-CONFIG-'),
                ('q', '-QUIT-'),   ('Q', '-QUIT-')]
    if planner_enabled:
        bindings += [('g', '-PLAN-'), ('G', '-PLAN-')]
    for ch, ev in bindings:
        window.bind(ch, ev)

    while True:
        event, values = _read(window)

        if event in (sg.WIN_CLOSED, '-QUIT-'):
            for p in plugins:
                try:
                    p.on_main_window_close()
                except Exception:
                    pass
            break

        # Give plugins (e.g. recording) first crack at the event.
        handled = False
        for p in plugins:
            try:
                if p.handle_main_event(event, values, window, {'config': config}):
                    handled = True
                    break
            except Exception as exc:
                show_error(f"Plugin error:\n{exc}", tb=_tb.format_exc(),
                           title='Plugin Error')
                handled = True
                break

        if handled:
            pass
        elif event == '-NEW-':
            window.hide()
            show_new_ticket_flow(cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-DRAFTS-':
            window.hide()
            # Loop so that opening a draft and leaving its form returns to the
            # drafts list (not the main window); break out only when the list is
            # cancelled or empty.
            while True:
                drafts = cache.drafts()
                chosen = show_draft_list(drafts, cache, config)
                if not chosen:
                    break
                show_ticket_form(chosen, cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-MANAGE-':
            window.hide()
            show_interaction_window(cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-PLAN-':
            # Lazy import to avoid an import cycle: planner.py imports shared form
            # helpers from this module (mirrors the config_ui import below).
            from .planner import show_plan_picker
            window.hide()
            show_plan_picker(cache, jira, config)
            window.un_hide()
            bring_to_front(window)

        elif event == '-CONFIG-':
            window.hide()
            updated = show_config_window(config, config_path)
            if updated:
                from .main import data_dir
                config = updated
                sg.theme(config.get('ui', {}).get('theme', 'DarkBlue3'))
                jira = JiraClient.from_config(config)
                cache = Cache(str(data_dir(config)))
            window.un_hide()
            bring_to_front(window)

        # Only re-scan the drafts folder after an action that can change it —
        # not for every keypress/plugin event (it reads every YAML on disk).
        if handled or event in ('-NEW-', '-DRAFTS-', '-MANAGE-', '-PLAN-'):
            drafts = cache.drafts()
            window['-MSG-'].update(_draft_msg(len(drafts)))
            window['-DRAFTS-'].update(disabled=len(drafts) == 0)

    window.close()
    return config
