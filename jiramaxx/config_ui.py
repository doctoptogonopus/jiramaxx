"""
Configuration GUI.
Three tabs: Jira credentials | App settings | Ticket Types field lists.

The Ticket Types tab uses three listboxes (Required / Optional / Not in Form)
with move and reorder buttons. Python dicts are the source of truth;
listboxes are display-only and refreshed after every action.
"""
from __future__ import annotations
import copy
from pathlib import Path
import yaml
import PySimpleGUI as sg

from .api import JiraClient
from .models import FIELD_META, TICKET_CLASSES, init_ticket_config, init_jira_config
from .utils import safe_read, show_error, bring_to_front

ALL_FIELDS = list(FIELD_META.keys())
TICKET_TYPES = list(TICKET_CLASSES.keys())

_JIRA_KEYS = [
    ('Base URL',      'jira.base_url'),
    ('API Token',     'jira.api_token'),
    ('User Email',    'jira.user_email'),
    ('Project Key',   'jira.project_key'),
    ('Board ID',      'jira.board_id'),
    ('My Account ID', 'jira.my_account_id'),
    ('Token Type',    'jira.token_type'),
    ('Cloud ID',      'jira.cloud_id'),
]

# Custom field IDs vary per Jira instance. Find them via Project Settings → Fields.
_CUSTOM_FIELD_KEYS = [
    ('Story Points field', 'jira.custom_fields.story_points'),
    ('Epic Link field',    'jira.custom_fields.epic_link'),
    ('Epic Name field',    'jira.custom_fields.epic_name'),
    ('Sprint field',       'jira.custom_fields.sprint'),
]

_APP_KEYS = [
    ('Cache Directory', 'cache.directory'),
    ('UI Theme',        'ui.theme'),
    ('Hotkey: Create',  'hotkeys.create_ticket'),
    ('Hotkey: Manage',  'hotkeys.manage_tickets'),
]

_DEFAULTS: dict[str, dict] = {
    'Story':      {'required': ['summary', 'description', 'story_points'],
                   'optional': ['assignee', 'labels', 'sprint', 'epic_link', 'priority']},
    'Bug':        {'required': ['summary', 'description', 'severity', 'steps_to_reproduce'],
                   'optional': ['assignee', 'labels', 'priority']},
    'Task':       {'required': ['summary', 'description'],
                   'optional': ['assignee', 'story_points', 'labels', 'priority']},
    'Epic':       {'required': ['summary', 'description', 'epic_name'],
                   'optional': ['labels', 'priority']},
    'Initiative': {'required': ['summary', 'description'],
                   'optional': ['labels', 'priority']},
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _nested_get(d: dict, dotkey: str) -> str:
    cur = d
    for k in dotkey.split('.'):
        if not isinstance(cur, dict):
            return ''
        cur = cur.get(k, '')
    return str(cur) if cur is not None else ''


def _nested_set(d: dict, dotkey: str, value):
    keys = dotkey.split('.')
    cur = d
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value


def _available_for(state: dict) -> list[str]:
    used = set(state['required'] + state['optional'])
    return [f for f in ALL_FIELDS if f not in used]


def _init_type_fields(config: dict) -> dict[str, dict]:
    result = {}
    for t in TICKET_TYPES:
        saved = config.get('ticket_types', {}).get(t, {})
        fb = _DEFAULTS.get(t, {'required': ['summary', 'description'], 'optional': []})
        result[t] = {
            'required': list(saved.get('required', fb['required'])),
            'optional': list(saved.get('optional', fb['optional'])),
        }
    return result


def _refresh(window: sg.Window, type_fields: dict, t: str):
    state = type_fields[t]
    window['-CFG-REQ-'].update(values=state['required'])
    window['-CFG-OPT-'].update(values=state['optional'])
    window['-CFG-AVAIL-'].update(values=_available_for(state))


def _reorder(lst: list, item, delta: int) -> int:
    idx = lst.index(item)
    new_idx = max(0, min(len(lst) - 1, idx + delta))
    if new_idx != idx:
        lst.insert(new_idx, lst.pop(idx))
    return new_idx


# ── Layout builders ───────────────────────────────────────────────────────────

def _jira_tab(config: dict) -> list:
    rows = []
    for label, key in _JIRA_KEYS:
        pw = '*' if 'token' in key else ''
        if key == 'jira.project_key':
            rows.append([sg.Text(label, size=(14, 1)),
                         sg.Input(_nested_get(config, key), key=f'-CFG-{key}-',
                                  size=(32, 1), password_char=pw, enable_events=True),
                         sg.Button('Browse', key='-BROWSE-PROJ-', size=(8, 1))])
        elif key == 'jira.token_type':
            cur = _nested_get(config, key) or 'classic'
            rows.append([sg.Text(label, size=(14, 1)),
                         sg.Combo(['classic', 'scoped'], default_value=cur,
                                  key=f'-CFG-{key}-', readonly=True, size=(10, 1),
                                  enable_events=True),
                         sg.Text('classic = direct site URL  |  scoped = api.atlassian.com',
                                 font=('Helvetica', 8))])
        elif key == 'jira.cloud_id':
            rows.append([sg.Text(label, size=(14, 1)),
                         sg.Input(_nested_get(config, key), key=f'-CFG-{key}-',
                                  size=(32, 1), enable_events=True),
                         sg.Button('Discover', key='-DISCOVER-CLOUD-', size=(9, 1))])
        elif key == 'jira.my_account_id':
            rows.append([sg.Text(label, size=(14, 1)),
                         sg.Input(_nested_get(config, key), key=f'-CFG-{key}-',
                                  size=(32, 1), enable_events=True),
                         sg.Button('Browse', key='-BROWSE-ACCOUNT-', size=(8, 1))])
        else:
            rows.append([sg.Text(label, size=(14, 1)),
                         sg.Input(_nested_get(config, key), key=f'-CFG-{key}-',
                                  size=(42, 1), password_char=pw, enable_events=True)])
    sprint_names = [s['name'] for s in (config.get('jira', {}).get('sprint_cache') or [])]
    sprint_summary = f"{len(sprint_names)} cached" if sprint_names else "none cached"
    rows += [
        [sg.HSep()],
        [sg.Text('Custom Field IDs  (Project Settings → Fields)',
                 font=('Helvetica', 9, 'italic'))],
        *[[sg.Text(label, size=(18, 1)),
           sg.Input(_nested_get(config, key), key=f'-CFG-{key}-', size=(28, 1),
                    enable_events=True)]
          for label, key in _CUSTOM_FIELD_KEYS],
        [sg.HSep()],
        [sg.Button('Refresh Sprints', key='-REFRESH-SPRINTS-'),
         sg.Text(f'Sprint cache: {sprint_summary}', key='-SPRINT-STATUS-',
                 font=('Helvetica', 9))],
    ]
    return rows


def _app_tab(config: dict) -> list:
    return [[sg.Text(label, size=(18, 1)),
             sg.Input(_nested_get(config, key), key=f'-CFG-{key}-', size=(38, 1),
                      enable_events=True)]
            for label, key in _APP_KEYS]


def _types_tab(type_fields: dict, current_type: str) -> list:
    s = type_fields[current_type]
    W, H = 18, 12
    return [
        [sg.Text('Ticket Type:'),
         sg.Combo(TICKET_TYPES, default_value=current_type, key='-CFG-TYPE-',
                  enable_events=True, readonly=True, size=(14, 1))],
        [sg.HSep()],
        [
            sg.Column([
                [sg.Text('Required', font=('Helvetica', 10, 'bold'))],
                [sg.Listbox(s['required'], key='-CFG-REQ-',
                            size=(W, H), select_mode='single', enable_events=True)],
                [sg.Button('↑', key='-REQ-UP-',   size=(3, 1)),
                 sg.Button('↓', key='-REQ-DOWN-', size=(3, 1)),
                 sg.Button('✕', key='-REQ-RM-',   size=(3, 1))],
            ]),
            sg.Column([
                [sg.Text('Optional', font=('Helvetica', 10, 'bold'))],
                [sg.Listbox(s['optional'], key='-CFG-OPT-',
                            size=(W, H), select_mode='single', enable_events=True)],
                [sg.Button('↑', key='-OPT-UP-',   size=(3, 1)),
                 sg.Button('↓', key='-OPT-DOWN-', size=(3, 1)),
                 sg.Button('✕', key='-OPT-RM-',   size=(3, 1))],
            ]),
            sg.Column([
                [sg.Text('Not in Form', font=('Helvetica', 10, 'bold'))],
                [sg.Listbox(_available_for(s), key='-CFG-AVAIL-',
                            size=(W, H), select_mode='single', enable_events=True)],
                [sg.Button('→ Req', key='-TO-REQ-', size=(8, 1)),
                 sg.Button('→ Opt', key='-TO-OPT-', size=(8, 1))],
            ]),
        ],
    ]


# ── Main entry point ──────────────────────────────────────────────────────────

def show_config_window(config: dict, config_path: Path) -> dict | None:
    """Open the config editor. Returns updated config on save, None on cancel."""
    working = copy.deepcopy(config)
    type_fields = _init_type_fields(working)
    current_type = TICKET_TYPES[0]

    def _make_client(v: dict) -> JiraClient:
        return JiraClient(
            v.get('-CFG-jira.base_url-', '').strip(),
            v.get('-CFG-jira.user_email-', '').strip(),
            v.get('-CFG-jira.api_token-', '').strip(),
            token_type=v.get('-CFG-jira.token_type-', 'classic') or 'classic',
            cloud_id=v.get('-CFG-jira.cloud_id-', '').strip(),
        )

    def _cloud_id_ok(v: dict) -> bool:
        """False (and shows a popup) if scoped mode is selected without Cloud ID."""
        if v.get('-CFG-jira.token_type-', '') == 'scoped' \
                and not v.get('-CFG-jira.cloud_id-', '').strip():
            sg.popup('Cloud ID is required when Token Type is "scoped".\n\n'
                     'Click "Discover" next to the Cloud ID field to populate it '
                     'automatically, or fill it in manually.',
                     title='Missing Cloud ID', modal=True, keep_on_top=True)
            return False
        return True

    layout = [
        [sg.TabGroup([[
            sg.Tab('Jira',         _jira_tab(working)),
            sg.Tab('App Settings', _app_tab(working)),
            sg.Tab('Ticket Types', _types_tab(type_fields, current_type)),
        ]])],
        [sg.Push(),
         sg.Button('Save', key='-SAVE-'),
         sg.Button('Test Connection', key='-TEST-CONN-'),
         sg.Button('Discard Changes', key='-CANCEL-')],
    ]
    window = sg.Window('Configuration', layout, finalize=True)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)

    # Capture initial form state for change detection
    _, _orig_vals = window.read(timeout=0)
    _CHANGED_BG = '#6B4300'
    _DEFAULT_BG = sg.theme_input_background_color()
    _cfg_keys = [f'-CFG-{k}-' for _, k in _JIRA_KEYS + _CUSTOM_FIELD_KEYS + _APP_KEYS]

    def _highlight_changes():
        _, cur = window.read(timeout=0)
        if cur is None:
            return
        for fkey in _cfg_keys:
            if fkey not in cur or fkey not in _orig_vals:
                continue
            changed = str(cur.get(fkey) or '') != str(_orig_vals.get(fkey) or '')
            try:
                window[fkey].update(background_color=_CHANGED_BG if changed else _DEFAULT_BG)
            except Exception:
                pass

    while True:
        event, values = safe_read(window)

        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            window.close()
            return None

        # ── Type selector ──────────────────────────────────────────────────
        if event == '-CFG-TYPE-':
            current_type = values['-CFG-TYPE-']
            _refresh(window, type_fields, current_type)

        # ── Move from Not-in-Form → Required ──────────────────────────────
        elif event == '-TO-REQ-':
            sel = values.get('-CFG-AVAIL-')
            if sel:
                type_fields[current_type]['required'].append(sel[0])
                _refresh(window, type_fields, current_type)

        # ── Move from Not-in-Form → Optional ──────────────────────────────
        elif event == '-TO-OPT-':
            sel = values.get('-CFG-AVAIL-')
            if sel:
                type_fields[current_type]['optional'].append(sel[0])
                _refresh(window, type_fields, current_type)

        # ── Remove from Required → Not-in-Form ────────────────────────────
        elif event == '-REQ-RM-':
            sel = values.get('-CFG-REQ-')
            if sel and sel[0] in type_fields[current_type]['required']:
                type_fields[current_type]['required'].remove(sel[0])
                _refresh(window, type_fields, current_type)

        # ── Remove from Optional → Not-in-Form ────────────────────────────
        elif event == '-OPT-RM-':
            sel = values.get('-CFG-OPT-')
            if sel and sel[0] in type_fields[current_type]['optional']:
                type_fields[current_type]['optional'].remove(sel[0])
                _refresh(window, type_fields, current_type)

        # ── Reorder Required ───────────────────────────────────────────────
        elif event in ('-REQ-UP-', '-REQ-DOWN-'):
            sel = values.get('-CFG-REQ-')
            if sel:
                lst = type_fields[current_type]['required']
                if sel[0] in lst:
                    new_i = _reorder(lst, sel[0], -1 if event == '-REQ-UP-' else 1)
                    _refresh(window, type_fields, current_type)
                    window['-CFG-REQ-'].update(set_to_index=[new_i])

        # ── Reorder Optional ───────────────────────────────────────────────
        elif event in ('-OPT-UP-', '-OPT-DOWN-'):
            sel = values.get('-CFG-OPT-')
            if sel:
                lst = type_fields[current_type]['optional']
                if sel[0] in lst:
                    new_i = _reorder(lst, sel[0], -1 if event == '-OPT-UP-' else 1)
                    _refresh(window, type_fields, current_type)
                    window['-CFG-OPT-'].update(set_to_index=[new_i])

        # ── Test Connection ────────────────────────────────────────────────
        elif event == '-TEST-CONN-':
            url   = values.get('-CFG-jira.base_url-', '').strip()
            token = values.get('-CFG-jira.api_token-', '').strip()
            proj  = values.get('-CFG-jira.project_key-', '').strip()
            if not all([url, token]):
                sg.popup('Fill in Base URL and API Token first.',
                         title='Test Connection', modal=True, keep_on_top=True)
            elif not _cloud_id_ok(values):
                pass
            else:
                try:
                    import traceback
                    tmp = _make_client(values)
                    me = tmp.get_myself()
                    lines = [f"Auth OK  →  {me.get('displayName', '?')} ({me.get('emailAddress', '?')})"]
                    if proj:
                        try:
                            pdata = tmp.get_project(proj)
                            lines.append(f"Project  →  {pdata.get('name', proj)}  [{proj}]  ✓ found")
                            can_create = tmp.check_create_permission(proj)
                            lines.append(f"CREATE_ISSUES permission  →  {'✓ YES' if can_create else '✗ NO — this is why tickets fail'}")
                        except Exception as pe:
                            lines.append(f"Project  →  {proj}  ✗ not found or no access: {pe}")
                    sg.popup('\n'.join(lines), title='Connection Test',
                             font=('Courier', 10), modal=True, keep_on_top=True)
                except Exception as exc:
                    import traceback
                    show_error(f"Connection failed:\n{exc}", tb=traceback.format_exc())

        # ── Discover Cloud ID ──────────────────────────────────────────────
        elif event == '-DISCOVER-CLOUD-':
            url = values.get('-CFG-jira.base_url-', '').strip()
            if not url:
                sg.popup('Fill in Base URL first.', title='Discover Cloud ID',
                         modal=True, keep_on_top=True)
            else:
                try:
                    cloud_id = JiraClient.discover_cloud_id(url)
                    window['-CFG-jira.cloud_id-'].update(cloud_id)
                except Exception as exc:
                    import traceback
                    show_error(f"Could not discover Cloud ID:\n{exc}",
                               tb=traceback.format_exc())

        # ── Browse Projects ────────────────────────────────────────────────
        elif event == '-BROWSE-PROJ-':
            url   = values.get('-CFG-jira.base_url-', '').strip()
            token = values.get('-CFG-jira.api_token-', '').strip()
            if not all([url, token]):
                sg.popup('Fill in Base URL and API Token first.',
                         title='Browse Projects', modal=True, keep_on_top=True)
            elif not _cloud_id_ok(values):
                pass
            else:
                try:
                    tmp = _make_client(values)
                    projects = tmp._get('/rest/api/3/project')
                    if isinstance(projects, dict):
                        projects = projects.get('values', [])
                    proj_labels = [f"{p['key']:12s}  {p['name']}" for p in projects]
                    lay = [
                        [sg.Text('Select your project:', font=('Helvetica', 11, 'bold'))],
                        [sg.Listbox(proj_labels,
                                    size=(60, min(len(proj_labels) + 1, 12)),
                                    key='-PL-', select_mode='single',
                                    enable_events=True)],
                        [sg.Button('Select', key='-PSEL-'),
                         sg.Button('Cancel', key='-PCNL-')],
                    ]
                    pw = sg.Window('Projects', lay, finalize=True, modal=True)
                    pw.bind('<Return>', '-PSEL-')
                    pw.bind('<Escape>', '-PCNL-')
                    bring_to_front(pw)
                    while True:
                        pe, pv = safe_read(pw)
                        if pe in (sg.WIN_CLOSED, '-PCNL-'):
                            break
                        if pe in ('-PSEL-', '-PL-') and pv.get('-PL-'):
                            chosen = pv['-PL-'][0].split()[0].strip()
                            window['-CFG-jira.project_key-'].update(chosen)
                            break
                    pw.close()
                except Exception as exc:
                    import traceback
                    show_error(f"Could not fetch projects:\n{exc}",
                               tb=traceback.format_exc())

        # ── Browse Account ID (Jira user search) ──────────────────────────
        elif event == '-BROWSE-ACCOUNT-':
            token = values.get('-CFG-jira.api_token-', '').strip()
            if not token:
                sg.popup('Fill in API Token first.', title='Browse Users',
                         modal=True, keep_on_top=True)
            elif not _cloud_id_ok(values):
                pass
            else:
                tmp = _make_client(values)
                default_query = values.get('-CFG-jira.user_email-', '').strip()

                lay = [
                    [sg.Text('Search users (name or email):',
                             font=('Helvetica', 11, 'bold'))],
                    [sg.Input(default_query, key='-UQ-', size=(40, 1)),
                     sg.Button('Search', key='-USEARCH-', bind_return_key=True)],
                    [sg.Listbox([], size=(60, 10), key='-UL-',
                                select_mode='single', enable_events=True)],
                    [sg.Button('Use My Account', key='-UMYSELF-'),
                     sg.Push(),
                     sg.Button('Select', key='-USEL-'),
                     sg.Button('Cancel', key='-UCNL-')],
                ]
                uw = sg.Window('Browse Users', lay, finalize=True, modal=True)
                uw.bind('<Escape>', '-UCNL-')
                bring_to_front(uw)

                users_data: list[tuple[str, str]] = []

                def _do_search(q: str):
                    q = q.strip()
                    if not q:
                        return
                    try:
                        users = tmp._get('/rest/api/3/user/search',
                                         {'query': q, 'maxResults': 30})
                    except Exception as exc:
                        import traceback
                        show_error(f"Could not search users:\n{exc}",
                                   tb=traceback.format_exc())
                        return
                    if not isinstance(users, list):
                        users = []
                    users_data.clear()
                    for u in users:
                        label = (f"{u.get('displayName', '?')}  "
                                 f"({u.get('emailAddress', 'no email')})  →  "
                                 f"{u.get('accountId', '?')}")
                        users_data.append((label, u.get('accountId', '')))
                    uw['-UL-'].update([d for d, _ in users_data])

                try:
                    if default_query:
                        _do_search(default_query)

                    while True:
                        ue, uv = safe_read(uw)
                        if ue in (sg.WIN_CLOSED, '-UCNL-'):
                            break
                        if ue == '-USEARCH-':
                            _do_search(uv.get('-UQ-', ''))
                        elif ue == '-UMYSELF-':
                            try:
                                me = tmp.get_myself()
                            except Exception as exc:
                                import traceback
                                show_error(f"Could not load your account:\n{exc}",
                                           tb=traceback.format_exc())
                                continue
                            aid = me.get('accountId', '')
                            if aid:
                                window['-CFG-jira.my_account_id-'].update(aid)
                            break
                        elif ue in ('-USEL-', '-UL-') and uv.get('-UL-'):
                            chosen = uv['-UL-'][0]
                            for d, aid in users_data:
                                if d == chosen and aid:
                                    window['-CFG-jira.my_account_id-'].update(aid)
                                    break
                            if ue == '-USEL-':
                                break
                finally:
                    uw.close()

        # ── Refresh Sprints ────────────────────────────────────────────────
        elif event == '-REFRESH-SPRINTS-':
            proj  = values.get('-CFG-jira.project_key-', '').strip()
            token = values.get('-CFG-jira.api_token-', '').strip()
            sprint_cf = values.get('-CFG-jira.custom_fields.sprint-', '').strip() or 'customfield_10020'
            if not all([token, proj]):
                sg.popup('Fill in API Token and Project Key first.',
                         title='Refresh Sprints', modal=True, keep_on_top=True)
            elif not _cloud_id_ok(values):
                pass
            else:
                try:
                    tmp = _make_client(values)
                    sprints = tmp.get_sprints(proj, sprint_cf)
                    working.setdefault('jira', {})['sprint_cache'] = sprints
                    init_jira_config(working.get('jira', {}))
                    names = [s['name'] for s in sprints]
                    window['-SPRINT-STATUS-'].update(
                        f"Sprint cache: {len(sprints)} cached — {', '.join(names) or 'none'}")
                    sg.popup_quick_message(f"Cached {len(sprints)} sprint(s).",
                                           auto_close_duration=1,
                                           background_color='#2e7d32', text_color='white')
                except Exception as exc:
                    import traceback
                    show_error(f"Could not fetch sprints:\n{exc}", tb=traceback.format_exc())

        # ── Save ───────────────────────────────────────────────────────────
        elif event == '-SAVE-':
            for _, key in _JIRA_KEYS + _CUSTOM_FIELD_KEYS + _APP_KEYS:
                _nested_set(working, key, values.get(f'-CFG-{key}-', ''))
            working.setdefault('ticket_types', {})
            for t, state in type_fields.items():
                working['ticket_types'][t] = {
                    'required': state['required'],
                    'optional': state['optional'],
                }
            with open(config_path, 'w') as f:
                yaml.dump(working, f, default_flow_style=False, sort_keys=False)
            init_ticket_config(working.get('ticket_types', {}))
            init_jira_config(working.get('jira', {}))
            sg.popup_quick_message('Configuration saved.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')
            window.close()
            return working

        _highlight_changes()

    window.close()
    return None
