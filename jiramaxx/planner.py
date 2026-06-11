"""Visual initiative planner — a draggable relationship graph for laying out an
epic + stories/tasks as connected nodes and pushing the whole tree to Jira.

Lives in core but is self-contained here so it can later be lifted into an
optional ``jiramaxx-planning`` plugin with minimal churn. The only core internals
it borrows are the shared form helpers in :mod:`jiramaxx.ui`; everything else goes
through the public ``Cache`` / ``JiraClient`` / model surface.
"""
from __future__ import annotations
import math
import uuid
from tkinter import colorchooser
import PySimpleGUI as sg
from .models import Ticket, TICKET_CLASSES, FIELD_META, ticket_from_dict
from .cache import Cache, new_plan
from .api import JiraClient
from .utils import safe_read as _read, show_error, bring_to_front, run_with_busy
from .ui import _build_field_row, _fkey, _soft_select, show_type_selector, _epic_link_cf, _yn_dialog

_NODE_W, _NODE_H = 150, 50          # default size; nodes carry their own w/h once resized
_MIN_W, _MIN_H = 90, 36
_HANDLE = 16                        # px hit zone of the corner grips
_CANVAS_W, _CANVAS_H = 920, 560
_NODE_COLORS = {'Story': '#1565c0', 'Bug': '#c62828', 'Task': '#2e7d32',
                'Epic': '#6a1b9a', 'Initiative': '#00838f'}


def _node_colors(config: dict) -> dict:
    """Per-type node colors: user overrides from config (planner.node_colors)
    merged over the defaults. Pure — unit-tested."""
    return {**_NODE_COLORS, **((config.get('planner') or {}).get('node_colors') or {})}


def _node_size(n: dict) -> tuple[int, int]:
    return int(n.get('w') or _NODE_W), int(n.get('h') or _NODE_H)


def _view_node(n: dict, z: float, pan: tuple = (0, 0)) -> dict:
    """A screen-space copy of a node under the view transform
    ``screen = world·z + pan``. Model/world coords are never mutated, so plans
    on disk are unaffected by zooming."""
    w, h = _node_size(n)
    return {**n, 'x': n['x'] * z + pan[0], 'y': n['y'] * z + pan[1],
            'w': max(1, w * z), 'h': max(1, h * z)}


def _new_node(ticket: Ticket, x: int, y: int) -> dict:
    """A fresh draft node wrapping a ticket at a canvas position."""
    return {'node_id': 'n' + uuid.uuid4().hex[:6], 'x': int(x), 'y': int(y),
            'kind': 'draft', 'jira_key': None, 'ticket': ticket.to_dict()}


def _node_ticket(node: dict) -> Ticket:
    return ticket_from_dict(dict(node.get('ticket') or {}))


def _edge_label(e: dict) -> str:
    if e.get('category') == 'hierarchy':
        return 'epic-child' if e.get('rel') == 'epic-child' else 'subtask'
    return e.get('rel', 'link')


def _hierarchy_rel(parent_node: dict) -> dict:
    """The hierarchy kind is inherent in the parent's type: an Epic's child is
    epic-linked; any other parent's child is a subtask (Jira ``parent`` field)."""
    ptype = _node_ticket(parent_node).ticket_type
    return {'category': 'hierarchy', 'rel': 'epic-child' if ptype == 'Epic' else 'subtask'}


def _set_parent_edge(edges: list, parent_node: dict, child_node: dict) -> list:
    """Make ``child_node`` a hierarchy-child of ``parent_node``. A node has exactly
    one hierarchy parent, so any existing hierarchy edge into the child is dropped
    first, then the inferred edge (parent → child) is added. Returns the dropped
    edges so callers can queue Jira-side cleanup for pushed ones (see
    :func:`_apply_relationship`)."""
    cid = child_node['node_id']
    dropped = [e for e in edges
               if e.get('category') == 'hierarchy' and e.get('to') == cid]
    edges[:] = [e for e in edges
                if not (e.get('category') == 'hierarchy' and e.get('to') == cid)]
    edges.append({'from': parent_node['node_id'], 'to': cid, **_hierarchy_rel(parent_node)})
    return dropped


def _edge_segment(a: dict, b: dict) -> tuple | None:
    """The visible segment of edge a → b: from a's center to the point where the
    line meets b's border (so the arrowhead stays visible). None if coincident."""
    aw, ah = _node_size(a)
    bw, bh = _node_size(b)
    ca = (a['x'] + aw / 2, a['y'] + ah / 2)
    cb = (b['x'] + bw / 2, b['y'] + bh / 2)
    dx, dy = ca[0] - cb[0], ca[1] - cb[1]
    if dx == 0 and dy == 0:
        return None
    sx = (bw / 2) / abs(dx) if dx else float('inf')
    sy = (bh / 2) / abs(dy) if dy else float('inf')
    s = min(sx, sy)
    return ca, (cb[0] + dx * s, cb[1] + dy * s)


def _point_segment_dist(pt, p1, p2) -> float:
    """Distance from ``pt`` to the segment p1–p2 (for clicking thin edge lines —
    tk's figure lookup uses bounding boxes, far too coarse for diagonals)."""
    px, py = pt
    x1, y1 = p1
    x2, y2 = p2
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return math.hypot(px - x1, py - y1)
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (x1 + t * dx), py - (y1 + t * dy))


def _edge_at_point(pt, by_id: dict, edges: list, threshold: float = 6.0,
                   skip: set | None = None) -> int | None:
    """Index of the edge whose drawn segment is nearest to ``pt`` (within the
    threshold), or None. Lets arrows be selected like nodes. ``skip`` holds
    indices of edges not drawn on the canvas (see :func:`_implied_epic_edges`)
    — what isn't visible isn't clickable."""
    best, best_d = None, threshold
    for i, e in enumerate(edges):
        if skip and i in skip:
            continue
        a, b = by_id.get(e.get('from')), by_id.get(e.get('to'))
        if not a or not b:
            continue
        seg = _edge_segment(a, b)
        if not seg:
            continue
        d = _point_segment_dist(pt, *seg)
        if d <= best_d:
            best, best_d = i, d
    return best


def _draw_directed_edge(graph, a: dict, b: dict, label: str,
                        color: str = '#9e9e9e', label_color: str = '#1565c0',
                        label_offset: tuple = (0.0, 0.0)) -> None:
    """Draw an edge a → b as a line into b's border with an arrowhead, plus a label.
    Uses only ``draw_line`` so it works across PySimpleGUI versions."""
    seg = _edge_segment(a, b)
    if not seg:
        return
    ca, tip = seg
    dx, dy = ca[0] - tip[0], ca[1] - tip[1]
    graph.draw_line(ca, tip, color=color, width=2)
    # Two wings ~14px at ±25° off the back-vector (tip → a).
    ang = math.atan2(dy, dx)
    L, w = 14, math.radians(25)
    for sign in (+1, -1):
        graph.draw_line(tip, (tip[0] + L * math.cos(ang + sign * w),
                              tip[1] + L * math.sin(ang + sign * w)),
                        color=color, width=2)
    lx = (ca[0] + tip[0]) / 2 + label_offset[0]
    ly = (ca[1] + tip[1]) / 2 + label_offset[1]
    graph.draw_text(label, (lx, ly), color=label_color, font=('Helvetica', 7))


def _draw_node(graph, n: dict, line_color: str = 'black', line_width: int = 1,
               head_prefix: str = '', zoom: float = 1.0,
               colors: dict | None = None) -> tuple:
    """Draw one node rectangle + caption + corner grips; returns
    (rect_fig, text_fig). The caller decides the border styling (selection /
    incomplete / modified / diff colors) and passes view-space nodes (see
    ``_view_node``) with the zoom for font scaling. The top-right ≡ grip is the
    *move* handle, the bottom-right ◢ hatch the *resize* handle (the body
    itself only selects, so clicks never accidentally drag)."""
    x, y = n['x'], n['y']
    w, h = _node_size(n)
    t = _node_ticket(n)
    fill = (colors or _NODE_COLORS).get(t.ticket_type, '#455a64')
    rect = graph.draw_rectangle((x, y), (x + w, y + h), fill_color=fill,
                                line_color=line_color, line_width=line_width)
    head = head_prefix + (n.get('jira_key') or t.ticket_type)
    chars = max(8, int(w / 7.5))
    txt = graph.draw_text(f"{head}\n{(t.summary or '(no title)')[:chars]}",
                          (x + w / 2, y + h / 2),
                          color='white',
                          font=('Helvetica', max(6, round(8 * zoom))))
    # Move grip (≡) in the top-right corner.
    for i in range(3):
        gy = y + 4 + i * 3
        graph.draw_line((x + w - 13, gy), (x + w - 3, gy), color='#eceff1', width=1)
    # Resize grip (◢ hatch) in the bottom-right corner.
    for i in range(3):
        d = 4 + i * 4
        graph.draw_line((x + w - d, y + h - 2), (x + w - 2, y + h - d),
                        color='#eceff1', width=1)
    return rect, txt


def _ticket_from_issue(issue: dict) -> 'Ticket':
    """Build a Ticket carrying a raw Jira issue's real field values."""
    f = issue.get('fields', {}) or {}
    itype = ((f.get('issuetype') or {}).get('name')) or 'Task'
    cls = TICKET_CLASSES.get(itype, TICKET_CLASSES['Task'])
    t = cls()
    t.summary = f.get('summary', '') or ''
    if f.get('description') is not None:
        t.description = _adf_to_text(f.get('description'))
    if (f.get('priority') or {}).get('name'):
        t.priority = f['priority']['name']
    if f.get('labels'):
        t.labels = ', '.join(f['labels'])
    return t


def _planner_node_edit(ticket: Ticket, jira=None, node=None,
                       epic_key: str | None = None,
                       epic_is_draft: bool = False) -> bool:
    """Edit a planner node's ticket fields (Save/Cancel — no Jira submit, that's
    deferred to Push). Mutates ``ticket`` in place; returns True if saved.
    Existing (already-in-Jira) nodes are editable too: their changes are queued
    and applied to the real ticket on Push (two-way editing).

    When ``node`` and ``jira`` are given and the node has a jira_key, a
    'Restore from Jira' button lets the user pull the current field values from
    the live issue, refreshing the diff baseline to Jira's current state.

    ``epic_key``/``epic_is_draft`` describe the plan's root Epic: when the form
    has an Epic Link field, a 'This epic' button fills it with that key (or sits
    disabled while the epic is still a local draft with no key to point at)."""
    fields = ticket.all_form_fields()
    has_restore = (node is not None and node.get('jira_key') and jira is not None)

    btn_row = []
    if has_restore:
        btn_row.append(sg.Button('Restore from Jira', key='-RESTORE-'))
    btn_row += [sg.Push(),
                sg.Button('Save', key='-SAVE-', bind_return_key=False),
                sg.Button('Cancel', key='-CANCEL-')]

    field_rows = []
    for f in fields:
        row = _build_field_row(f, ticket)
        if f == 'epic_link' and (epic_key or epic_is_draft):
            row.append(sg.Button(
                'This epic', key='-THISEPIC-', disabled=epic_key is None,
                tooltip=("Fill in this plan's epic"
                         if epic_key else
                         "This plan's epic isn't in Jira yet — connect with a "
                         "Child arrow instead; it nests on push.")))
        field_rows.append(row)

    layout = [
        [sg.Text(f'Edit {ticket.ticket_type}', font=('Helvetica', 12, 'bold'))],
        [sg.HSep()],
        *field_rows,
        [sg.HSep()],
        btn_row,
    ]
    window = sg.Window('Plan node', layout, finalize=True, modal=True,
                       keep_on_top=True, return_keyboard_events=False)
    window.bind('<Escape>', '-CANCEL-')
    import tkinter as tk
    for f in fields:
        w_widget = window[_fkey(f)].Widget
        if isinstance(w_widget, tk.Text):
            w_widget.bind('<Tab>',
                          lambda e, _w=w_widget: (_w.tk_focusNext().focus_set(), 'break')[1])
            w_widget.bind('<Shift-Tab>',
                          lambda e, _w=w_widget: (_w.tk_focusPrev().focus_set(), 'break')[1])
    bring_to_front(window)
    saved = False
    fetched = None  # set when a Restore was performed; refreshes the diff baseline
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break
        if event == '-THISEPIC-':
            window[_fkey('epic_link')].update(value=epic_key)
            continue
        if event == '-RESTORE-':
            status, issue, _ = run_with_busy(
                lambda: jira.get_issue(node['jira_key']),
                message=f"Loading {node['jira_key']}…")
            if status != 'ok':
                if status == 'error':
                    show_error(f"Could not load {node['jira_key']}:\n{issue}")
                continue
            fresh = _ticket_from_issue(issue)
            fetched = fresh.to_dict()
            # Update every form widget in place so the user sees the live values.
            for f in fields:
                window[_fkey(f)].update(value=str(getattr(fresh, f, '') or ''))
            continue
        if event == '-SAVE-':
            ticket.apply_form_values(values)
            if fetched is not None:
                # Restore was used — refresh the diff baseline to Jira's current
                # state so the caller's no-diff check queues nothing for a plain
                # restore-and-save.
                node['orig_ticket'] = fetched
            saved = True
            break
    window.close()
    return saved


def _build_link_options(link_types: list[dict]) -> list[tuple[str, dict]]:
    """(label, payload) pairs for every link type in both directions. The label
    reads along the canvas arrow (source → other node); ``reverse`` swaps which
    side is the inward issue on push. Pure function — unit-tested."""
    opts: list[tuple[str, dict]] = []
    for lt in link_types or []:
        name = lt.get('name', '')
        outw = lt.get('outward', name) or name
        inw = lt.get('inward', name) or name
        opts.append((f"{outw}  ({name})", {'category': 'link', 'rel': name, 'reverse': False}))
        opts.append((f"{inw}  ({name})", {'category': 'link', 'rel': name, 'reverse': True}))
    return opts


def _link_options(jira: JiraClient, cache: Cache, state: dict) -> list[tuple[str, dict]]:
    """Link options, resolved canvas state → disk snapshot → network (saved to
    both). Link types essentially never change, so the network is hit at most
    once per installation; delete ``link_types.yaml`` to force a refresh."""
    if state.get('link_types') is None:
        cached = cache.load_link_types()
        if cached is not None:
            state['link_types'] = cached
        else:
            status, types, _ = run_with_busy(jira.get_issue_link_types,
                                             message='Fetching link types…')
            state['link_types'] = types if status == 'ok' else []
            if status == 'ok' and types:
                cache.save_link_types(types)
    return _build_link_options(state['link_types'])


def _can_be_parent(node: dict | None) -> bool:
    """Whether a node's ticket type can be a hierarchy parent in Jira. Only
    Epics (epic link / parent) and Initiatives (parent, Jira Premium) can nest
    children; nesting under a Story/Task/Bug needs a true Subtask issue type,
    which this tool doesn't model — Jira rejects the create outright."""
    if node is None:
        return True   # unknown (e.g. the spawn dialog's not-yet-typed node)
    return _node_ticket(node).ticket_type in ('Epic', 'Initiative')


def _relationship_options(jira: JiraClient, cache: Cache,
                          state: dict,
                          source_node=None,
                          target_node=None) -> list[tuple[str, dict]]:
    """Everything a new (or re-typed) edge can be. The first entry is the combo
    default, so ordering encodes the sensible default per source type:

    - Parent-capable source (Epic/Initiative, or unknown): Child, Parent, then
      every link phrase.
    - Other sources: the 'relates to' phrase first (Jira can't nest under
      them, so a dependency link is the natural default), then Parent, then
      the remaining link phrases. No Child option.

    When ``target_node`` is known (link mode / edge re-type) and can't be a
    parent, the Parent option is dropped too — nesting under it is the same
    invalid hierarchy in the other direction."""
    links = _link_options(jira, cache, state)
    parent_opt = ('Parent  (this ticket nests under it)',
                  {'category': 'hierarchy', 'dir': 'parent'})
    if _can_be_parent(source_node):
        opts = [('Child  (nests under this ticket)',
                 {'category': 'hierarchy', 'dir': 'child'})]
        if _can_be_parent(target_node):
            opts.append(parent_opt)
        return opts + links
    relates = [o for o in links
               if (o[1].get('rel') or '').lower().startswith('relat')
               and not o[1].get('reverse')]
    rest = [o for o in links if o not in relates]
    opts = relates + ([parent_opt] if _can_be_parent(target_node) else []) + rest
    return opts or [parent_opt]


def _apply_relationship(edges: list, source: dict, other: dict, payload: dict,
                        plan: dict | None = None, by_id: dict | None = None) -> None:
    """Wire ``other`` to ``source`` per a relationship payload from
    :func:`_relationship_options`. Hierarchy goes through ``_set_parent_edge`` (one
    parent per child); links append a directed source → other edge.

    When ``plan``/``by_id`` are given, a *pushed* hierarchy edge displaced by the
    re-nest is queued for removal in Jira — but only when its rel type differs
    from the replacement (epic-child ⇄ subtask). A same-rel re-parent is a plain
    field overwrite on push; clearing it would race the new value."""
    if payload.get('category') == 'hierarchy':
        if payload.get('dir') == 'parent':
            dropped = _set_parent_edge(edges, other, source)
        else:
            dropped = _set_parent_edge(edges, source, other)
        new_rel = edges[-1].get('rel')
        if plan is not None and by_id is not None:
            for e in dropped:
                if e.get('pushed') and e.get('rel') != new_rel:
                    _queue_unlink(plan, e, by_id)
    else:
        if payload.get('reverse'):
            # Inverse phrase selected (e.g. "Blocked By"): the real blocker is `other`,
            # so the canonical edge goes other → source (other blocks source).
            edges.append({'from': other['node_id'], 'to': source['node_id'],
                          'category': 'link', 'rel': payload.get('rel'),
                          'reverse': False})
        else:
            edges.append({'from': source['node_id'], 'to': other['node_id'],
                          'category': 'link', 'rel': payload.get('rel'),
                          'reverse': False})


def _spawn_dialog(jira: JiraClient, cache: Cache, state: dict,
                  with_relationship: bool,
                  source_node=None) -> tuple[str, str, dict | None] | None:
    """Title-first dialog for adding a node. With ``with_relationship`` (arrow
    click) it adds a Relationship dropdown (Child default for Epics, Parent-first
    for other types). Returns ``(action, title, rel_payload)`` where action is
    'create' or 'import', or None if cancelled. Import lets the title double as
    the search query."""
    rel_opts = (_relationship_options(jira, cache, state, source_node=source_node)
                if with_relationship else [])
    labels = [o[0] for o in rel_opts]
    rows = [[sg.Text('New connected ticket' if with_relationship else 'New ticket',
                     font=('Helvetica', 13, 'bold'))],
            [sg.Text('Title:', size=(11, 1)), sg.Input('', key='-T-', size=(42, 1))]]
    if with_relationship:
        rows.append([sg.Text('Relationship:', size=(11, 1)),
                     sg.Combo(labels, default_value=labels[0], key='-R-',
                              readonly=True, size=(40, 1))])
    rows += [[sg.HSep()],
             [sg.Push(),
              sg.Button('Create', key='-CREATE-'),
              sg.Button('Import', key='-IMPORT-',
                        tooltip='Use an existing Jira ticket (title pre-fills the search)'),
              sg.Button('Cancel', key='-C-')]]
    w = sg.Window('New ticket', rows, finalize=True, modal=True, keep_on_top=True,
                  return_keyboard_events=False)
    w.bind('<Escape>', '-C-')
    w.bind('<Return>', '-CREATE-')
    bring_to_front(w)
    w['-T-'].set_focus()

    result = None
    while True:
        ev, vals = _read(w)
        if ev in (sg.WIN_CLOSED, '-C-'):
            break
        if ev in ('-CREATE-', '-IMPORT-'):
            title = (vals.get('-T-') or '').strip()
            if ev == '-CREATE-' and not title:
                sg.popup('Enter a title first.', modal=True, keep_on_top=True)
                continue
            rel = (dict(rel_opts[labels.index(vals['-R-'])][1])
                   if with_relationship and vals.get('-R-') in labels else None)
            result = ('create' if ev == '-CREATE-' else 'import', title, rel)
            break
    w.close()
    return result


def _pick_existing_issue(jira: JiraClient, config: dict,
                         initial_query: str = '',
                         existing_keys: set | None = None) -> list[dict]:
    """Search Jira for existing issues and return a list of chosen raw issue dicts
    (empty list if cancelled). Type a key (e.g. PAY-12) or words; Search runs a
    live JQL query. ``initial_query`` pre-fills the search box (e.g. the title
    typed in the spawn dialog before choosing Import). ``existing_keys`` filters
    out issues already present in the canvas."""
    proj = config.get('jira', {}).get('project_key', '')
    issues: list[dict] = []
    layout = [
        [sg.Text('Add an existing ticket', font=('Helvetica', 12, 'bold'))],
        [sg.Input(initial_query, key='-Q-', size=(36, 1)), sg.Button('Search', key='-S-')],
        [sg.Listbox([], size=(52, 12), key='-RES-', font=('Consolas', 10),
                    select_mode=sg.LISTBOX_SELECT_MODE_EXTENDED)],
        [sg.Text('', key='-INFO-', font=('Helvetica', 8))],
        [sg.Push(), sg.Button('Add', key='-ADD-'), sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Add existing ticket', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    w.bind('<Return>', '-ENTER-')
    w.bind('<Control-a>', '-SELALL-')
    w.bind('<Control-A>', '-SELALL-')
    bring_to_front(w)
    w['-Q-'].set_focus()

    def _labels():
        return [f"{i.get('key', '')}  {((i.get('fields') or {}).get('summary') or '')[:50]}"
                for i in issues]

    def _do_search():
        # Every press gives visible feedback — a silently ignored click reads
        # as a broken button.
        q = (w['-Q-'].get() or '').strip()
        if not q:
            w['-INFO-'].update('Type a search term first.')
            return
        status, found, _ = run_with_busy(lambda: jira.search_issues(proj, q),
                                         message='Searching…')
        if status != 'ok':
            w['-INFO-'].update(f"Search failed: {found}" if status == 'error'
                               else 'Search cancelled.')
            return
        nonlocal issues
        if existing_keys:
            issues = [i for i in found if i.get('key') not in existing_keys]
        else:
            issues = found
        suffix = (f' — {len(found) - len(issues)} already in graph'
                  if existing_keys and len(found) != len(issues) else '')
        w['-RES-'].update(_labels())
        w['-INFO-'].update(f"{len(issues)} match(es){suffix}" if issues else 'No matches.')
        if issues:
            _soft_select(w, 0, key='-RES-')

    def _selection() -> list[dict]:
        return [issues[_labels().index(lbl)]
                for lbl in (w['-RES-'].get() or []) if lbl in _labels()]

    chosen = []
    while True:
        ev, vals = _read(w)
        if ev in (sg.WIN_CLOSED, '-C-'):
            break
        if ev == '-S-':
            _do_search()
        elif ev == '-SELALL-':
            w['-RES-'].Widget.select_set(0, 'end')
        elif ev == '-ENTER-':
            # Focus decides what Enter means: in the query box it always
            # searches (the previous search leaves a row pre-selected, which
            # must NOT be imported by a re-search keystroke); in the results
            # list it imports the selection.
            try:
                in_query = w.TKroot.focus_get() is w['-Q-'].Widget
            except Exception:
                in_query = True
            if in_query:
                _do_search()
            else:
                chosen = _selection()
                if chosen:
                    break
        elif ev == '-ADD-':
            chosen = _selection()
            if chosen:
                break
            w['-INFO-'].update('Select a result first.')
    w.close()
    return chosen or []


def _adf_to_text(adf) -> str:
    """Flatten an Atlassian-Document-Format description to plain text (lossy but
    good enough to view/edit; paragraphs become newlines)."""
    if isinstance(adf, str):
        return adf
    out: list[str] = []

    def walk(children):
        for c in children or []:
            if not isinstance(c, dict):
                continue
            if c.get('type') == 'text':
                out.append(c.get('text', ''))
            else:
                walk(c.get('content'))
                if c.get('type') == 'paragraph':
                    out.append('\n')

    if isinstance(adf, dict):
        walk(adf.get('content'))
    return ''.join(out).strip()


def _existing_node(issue: dict, x: int, y: int, epic_cf: str | None = None) -> dict:
    """Build an 'existing' planner node from a raw Jira issue dict, carrying the
    real field values (where fetched) so edits diff against Jira's state.
    ``parent_key`` records the ticket's current Jira parent/epic (when the
    fetch included those fields) — the epic-inheritance checklist uses it to
    tell tickets of *another* epic apart from unparented ones."""
    t = _ticket_from_issue(issue)
    n = _new_node(t, x, y)
    n['kind'] = 'existing'
    n['jira_key'] = issue.get('key')
    n['ticket'] = t.to_dict()
    f = issue.get('fields', {}) or {}
    parent_key = ((f.get('parent') or {}).get('key')) or (f.get(epic_cf) if epic_cf else None)
    if parent_key:
        n['parent_key'] = str(parent_key)
    return n


def _node_field_diff(old: dict, new: dict) -> dict:
    """Form-field changes between two ticket dicts: {field: (old, new)}."""
    diff = {}
    for fname in FIELD_META:
        ov = str(old.get(fname, '') or '')
        nv = str(new.get(fname, '') or '')
        if ov != nv:
            diff[fname] = (ov, nv)
    return diff


def _changed_jira_fields(node: dict, proj: str) -> dict:
    """Jira payload fields that an edited existing node would change, computed by
    diffing the original vs current ticket's payloads (so custom-field mapping is
    reused, not re-implemented). Clearing a field to empty is not expressed —
    the payload simply omits empties."""
    old = ticket_from_dict(dict(node.get('orig_ticket') or {})).to_jira_payload(proj)['fields']
    new = ticket_from_dict(dict(node.get('ticket') or {})).to_jira_payload(proj)['fields']
    return {k: v for k, v in new.items()
            if k not in ('project', 'issuetype') and old.get(k) != v}


def _queue_unlink(plan: dict, edge: dict, by_id: dict) -> None:
    """Queue a pushed relationship for removal in Jira on the next Push."""
    a, b = by_id.get(edge.get('from')), by_id.get(edge.get('to'))
    plan.setdefault('pending_unlinks', []).append({
        'hier': edge.get('category') == 'hierarchy',
        'rel': edge.get('rel'),
        'from_key': (a or {}).get('jira_key'),
        'to_key': (b or {}).get('jira_key'),
        'link_id': edge.get('link_id'),
    })


def _find_link_id(jira: JiraClient, unlink: dict) -> str | None:
    """Resolve a link's id by listing one endpoint's issuelinks (needed when the
    link was created by a local push — Jira's create response carries no id)."""
    src, other, rel = unlink.get('from_key'), unlink.get('to_key'), unlink.get('rel')
    if not src or not other:
        return None
    data = jira.get_issue(src, fields='issuelinks')
    for ln in ((data.get('fields') or {}).get('issuelinks') or []):
        if ((ln.get('type') or {}).get('name') == rel
                and (((ln.get('outwardIssue') or {}).get('key') == other)
                     or ((ln.get('inwardIssue') or {}).get('key') == other))):
            return ln.get('id')
    return None


def _apply_unlink(jira: JiraClient, config: dict, unlink: dict) -> None:
    """Remove one pushed relationship in Jira: clear the child's parent/epic for
    hierarchy, or delete the issue link (resolving its id if unknown)."""
    if unlink.get('hier'):
        child = unlink.get('to_key')
        if not child:
            raise ValueError('child has no Jira key')
        fields = ({'parent': None} if unlink.get('rel') == 'subtask'
                  else {_epic_link_cf(config): None})
        jira.update_issue(child, fields)
        return
    link_id = unlink.get('link_id') or _find_link_id(jira, unlink)
    if not link_id:
        raise ValueError(f"link not found in Jira "
                         f"({unlink.get('from_key')} → {unlink.get('to_key')})")
    jira.delete_issue_link(link_id)


def _root_epic_node(nodes: list, edges: list) -> dict | None:
    """The plan's root Epic: the first Epic-type node with no hierarchy parent
    (same rule everywhere — picker, canvas, push)."""
    for n in nodes:
        if (_node_ticket(n).ticket_type == 'Epic'
                and not any(e.get('category') == 'hierarchy'
                            and e['to'] == n['node_id'] for e in edges)):
            return n
    return None


def _implied_epic_edges(nodes: list, edges: list) -> set[int]:
    """Indices of root-epic hierarchy edges to *hide* on the canvas: when the
    child also participates in dependency links, its epic membership is implied
    through the connected node, so drawing the epic arrow too is line noise.
    Only children whose sole relationship is the parent/child edge keep their
    arrow. Display-only — the edges stay in the model and still push, drive
    epic inheritance, and shape the tree layout. Pure — unit-tested."""
    root = _root_epic_node(nodes, edges)
    if root is None:
        return set()
    rid = root['node_id']
    linked = {nid for e in edges if e.get('category') == 'link'
              for nid in (e.get('from'), e.get('to'))}
    return {i for i, e in enumerate(edges)
            if e.get('category') == 'hierarchy' and e.get('from') == rid
            and e.get('to') in linked}


def _push_supplied_fields(plan: dict,
                          inherit_ids: list | None = None) -> dict[str, set]:
    """Fields the push itself fills in per draft node, so required-field
    validation doesn't demand them up front: a hierarchy child's epic_link /
    parent comes from its edge at create time, and epic-inheritance-checked
    drafts get epic_link from the root epic. Pure — unit-tested."""
    supplied: dict[str, set] = {}
    for e in plan.get('edges', []):
        if e.get('category') == 'hierarchy':
            field = 'epic_link' if e.get('rel') == 'epic-child' else 'parent'
            supplied.setdefault(e['to'], set()).add(field)
    for nid in inherit_ids or []:
        supplied.setdefault(nid, set()).add('epic_link')
    return supplied


def _incomplete_nodes(plan: dict, inherit_ids: list | None = None) -> list[str]:
    """Per-node 'missing required fields' messages for not-yet-pushed drafts.
    Used both by the push itself and by the -PUSH- handler, so the user is
    blocked *before* reviewing a diff that could never apply. Fields the push
    supplies (hierarchy edges, epic inheritance) don't count as missing."""
    supplied = _push_supplied_fields(plan, inherit_ids)
    out = []
    for n in plan.get('nodes', []):
        if n.get('kind') == 'existing' or n.get('jira_key'):
            continue
        t = _node_ticket(n)
        ok, missing = t.is_valid()
        missing = [f for f in missing if f not in supplied.get(n['node_id'], ())]
        if missing:
            out.append(f"{t.ticket_type} \"{(t.summary or '(no title)')[:30]}\": "
                       f"{', '.join(missing)}")
    return out


def _push_plan_to_jira(jira: JiraClient, config: dict, plan: dict,
                       inherit_ids: list | None = None) -> tuple[str, bool]:
    """Create every not-yet-pushed draft node as a real issue (parents before
    children), then apply the not-yet-pushed hierarchy updates and issue links.
    Returns ``(summary, clean)`` — clean means everything succeeded, so the
    caller can retire the local plan (Jira now holds the whole tree). Jira keys
    are written back into the nodes and successful edges get ``pushed: True``,
    so a retry after a partial failure only attempts what failed.

    ``inherit_ids`` are the epic-inheritance choices: drafts among them are
    created *with* the root epic's key in epic_link (ordered after the epic
    exists — required-field configs and Jira create screens both want the
    field at create time); already-existing ones get an update afterwards."""
    proj = config['jira']['project_key']
    nodes = {n['node_id']: n for n in plan['nodes']}
    inherit = set(inherit_ids or [])
    root_epic = _root_epic_node(plan['nodes'], plan['edges']) if inherit else None
    root_id = root_epic['node_id'] if root_epic else None

    # Block the whole push if any draft node is missing required fields — surface
    # exactly what's missing rather than creating a partial tree that fails midway.
    incomplete = _incomplete_nodes(plan, inherit_ids)
    if incomplete:
        return ("Push blocked — fix required fields first:\n  "
                + "\n  ".join(incomplete)), False

    parent_of, parent_rel, parent_edge = {}, {}, {}
    for e in plan['edges']:
        if e.get('category') == 'hierarchy':
            parent_of[e['to']] = e['from']
            parent_rel[e['to']] = e.get('rel')
            parent_edge[e['to']] = e

    created, created_keys, failures = 0, [], []
    pending = [nid for nid, n in nodes.items()
               if n.get('kind') != 'existing' and not n.get('jira_key')]
    guard = 0
    while pending and guard <= len(nodes) + 2:
        progressed = False
        for nid in list(pending):
            n = nodes[nid]
            pid = parent_of.get(nid)
            parent_key = None
            if pid:
                pnode = nodes.get(pid)
                parent_key = pnode.get('jira_key') if pnode else None
                if pnode is not None and pnode.get('kind') != 'existing' and not parent_key:
                    continue  # parent not created yet — try a later pass
            # Inheriting drafts wait for the epic the same way children wait
            # for their parent, so the create can carry the epic's real key.
            inherit_key = None
            if nid in inherit and root_epic is not None and nid != root_id:
                inherit_key = root_epic.get('jira_key')
                if not inherit_key and root_id in pending:
                    continue  # epic not created yet — try a later pass
            ticket = _node_ticket(n)
            rel = parent_rel.get(nid)
            if parent_key and rel == 'subtask':
                ticket.parent = parent_key
            elif parent_key and rel == 'epic-child':
                ticket.epic_link = parent_key
            elif inherit_key:
                ticket.epic_link = inherit_key
            try:
                resp = jira.create_issue(ticket.to_jira_payload(proj))
                n['jira_key'] = resp.get('key')
                n['ticket'] = ticket.to_dict()
                created += 1
                created_keys.append(n['jira_key'] or nid)
                if parent_key:
                    # The hierarchy edge was satisfied at create time.
                    parent_edge[nid]['pushed'] = True
                if inherit_key:
                    n['parent_key'] = inherit_key
            except Exception as exc:
                failures.append(f"{nid}: {exc}")
            pending.remove(nid)
            progressed = True
        if not progressed:
            failures.extend(f"{nid}: parent never created (cycle?)" for nid in pending)
            break
        guard += 1

    # Existing children: their parent/epic isn't set at create time (they already
    # exist), so update them in Jira now that any new parent has a key.
    updated = 0
    for e in plan['edges']:
        if e.get('category') != 'hierarchy' or e.get('pushed'):
            continue
        child, parent = nodes.get(e['to']), nodes.get(e['from'])
        if not child or child.get('kind') != 'existing' or not child.get('jira_key'):
            continue  # new children were parented at create time
        parent_key = parent.get('jira_key') if parent else None
        if not parent_key:
            failures.append(f"update {child['jira_key']}: parent wasn't created")
            continue
        fields = ({'parent': {'key': parent_key}} if e.get('rel') == 'subtask'
                  else {_epic_link_cf(config): parent_key})
        try:
            jira.update_issue(child['jira_key'], fields)
            e['pushed'] = True
            updated += 1
        except Exception as exc:
            failures.append(f"update {child['jira_key']}: {exc}")

    linked = 0
    for e in plan['edges']:
        if e.get('category') != 'link' or e.get('pushed'):
            continue
        a, b = nodes.get(e['from']), nodes.get(e['to'])
        ak = a.get('jira_key') if a else None
        bk = b.get('jira_key') if b else None
        if not ak or not bk:
            failures.append(f"link {e.get('rel')}: an issue wasn't created")
            continue
        # The on-canvas arrow goes source(ak) → target(bk). Map it so the outward
        # phrase reads along the arrow — i.e. for the outward-phrase option (reverse
        # False) the source is the *inward* issue and the target the *outward* issue
        # (this matches what Jira actually renders). `reverse` (inward phrase) swaps.
        inward, outward = (bk, ak) if e.get('reverse') else (ak, bk)
        try:
            jira.create_issue_link(inward, outward, e.get('rel'))
            e['pushed'] = True
            linked += 1
        except Exception as exc:
            failures.append(f"link {e.get('rel')}: {exc}")

    # Two-way editing: field changes queued on existing nodes (orig_ticket
    # snapshot) become updates on the real tickets.
    for n in plan['nodes']:
        if not n.get('orig_ticket') or not n.get('jira_key'):
            continue
        fields = _changed_jira_fields(n, proj)
        if not fields:
            n.pop('orig_ticket', None)
            continue
        try:
            jira.update_issue(n['jira_key'], fields)
            n.pop('orig_ticket', None)
            updated += 1
        except Exception as exc:
            failures.append(f"update {n['jira_key']}: {exc}")

    # Queued relationship removals (deleted/re-typed pushed arrows).
    removed = 0
    unlinks = plan.get('pending_unlinks') or []
    for u in list(unlinks):
        try:
            _apply_unlink(jira, config, u)
            unlinks.remove(u)
            removed += 1
        except Exception as exc:
            failures.append(f"unlink {u.get('rel')} "
                            f"({u.get('from_key')} → {u.get('to_key')}): {exc}")
    if not unlinks:
        plan.pop('pending_unlinks', None)

    # Epic inheritance for nodes already in Jira (new drafts were created with
    # the epic in place above; ``parent_key`` marks what Jira holds, so a
    # retry or repush skips tickets that already point at the epic).
    inherited = 0
    if inherit and root_epic is not None:
        epic_key = root_epic.get('jira_key')
        for nid in inherit:
            n = nodes.get(nid)
            if (not epic_key or not n or not n.get('jira_key')
                    or n['jira_key'] == epic_key
                    or n.get('parent_key') == epic_key):
                continue
            try:
                jira.update_issue(n['jira_key'], {_epic_link_cf(config): epic_key})
                n['parent_key'] = epic_key
                inherited += 1
            except Exception as exc:
                failures.append(f"epic link {n['jira_key']}: {exc}")

    msg = (f"Created {created} issue(s), updated {updated} existing, "
           f"{linked} link(s), removed {removed} relationship(s).")
    if inherited:
        msg += f"\nEpic set on {inherited} existing ticket(s)."
    if created_keys:
        msg += '\nCreated: ' + ', '.join(created_keys)
    if failures:
        msg += f"\n\n{len(failures)} problem(s):\n  " + '\n  '.join(failures[:20])
    return msg, not failures


def _worth_saving(plan: dict) -> bool:
    """A plan earns a YAML file only while it holds local work: a draft node, a
    not-yet-pushed edge, or queued edits to pushed content. A purely
    hydrated/fully-pushed graph can always be rebuilt on demand (Open from
    Jira), so persisting it is just litter."""
    return (any(n.get('kind') != 'existing' or n.get('orig_ticket')
                for n in plan.get('nodes', []))
            or any(not e.get('pushed') for e in plan.get('edges', []))
            or bool(plan.get('pending_unlinks')))


def _migrate_plan(plan: dict) -> None:
    """Silently correct legacy edges that stored ``reverse=True`` by swapping
    their from/to and clearing the flag. New edges are always canonical (no
    reverse flag), so this only fires on old plan files. Pure — mutates in place."""
    for e in plan.get('edges', []):
        if e.get('reverse') and e.get('category') == 'link':
            e['from'], e['to'] = e['to'], e['from']
            e['reverse'] = False


def _auto_layout(nodes: list, edges: list) -> None:
    """Tidy-tree layout over the hierarchy edges: each leaf takes the next
    column slot, each parent is centered over its children, depth = row. The
    root (the epic) therefore sits alone on the top row, centered over its
    subtree — nothing beside or above it. Nodes with no hierarchy edges at
    all (dependency-only / free-floating) go in grid rows *below* the tree."""
    children: dict[str, list[str]] = {n['node_id']: [] for n in nodes}
    has_parent: set[str] = set()
    hier_members: set[str] = set()
    for e in edges:
        if (e.get('category') == 'hierarchy'
                and e['from'] in children and e['to'] in children):
            children[e['from']].append(e['to'])
            has_parent.add(e['to'])
            hier_members.update((e['from'], e['to']))
    roots = [n['node_id'] for n in nodes
             if n['node_id'] in hier_members and n['node_id'] not in has_parent]

    pos: dict[str, tuple[float, int]] = {}   # node_id → (col, depth)
    next_col = [0.0]

    def place(nid: str, depth: int) -> float:
        pos[nid] = (0.0, depth)              # reserve before recursing (cycle guard)
        kids = [k for k in children.get(nid, []) if k not in pos]
        if not kids:
            col = next_col[0]
            next_col[0] += 1
        else:
            xs = [place(k, depth + 1) for k in kids]
            col = (min(xs) + max(xs)) / 2    # parent centered over its children
        pos[nid] = (col, depth)
        return col

    for r in roots:
        place(r, 0)

    # Free-floating nodes: grid rows below the deepest tree row.
    max_depth = max((d for _, d in pos.values()), default=-1)
    orphans = [n['node_id'] for n in nodes if n['node_id'] not in pos]
    for i, nid in enumerate(orphans):
        pos[nid] = (float(i % 5), max_depth + 1 + i // 5)

    for n in nodes:
        col, depth = pos[n['node_id']]
        n['x'] = int(60 + col * 180)
        n['y'] = int(40 + depth * 130)


def _epic_inherit_candidates(nodes: list, edges: list,
                             root_epic: dict) -> list[tuple[str, bool]]:
    """Which nodes should the epic-inheritance checklist offer, and checked or
    not by default. Pure — unit-tested.

    Candidates are *every* node in the plan except the root epic itself and
    its direct hierarchy children — those already get the epic from their
    visible arrow on push, so listing them would offer a checkbox that can't
    actually opt out. Everything deeper or attached by dependency links is
    exactly the "implied" membership the checklist governs: Jira only records
    those tickets under the epic if the field is set explicitly.

    Default checked: draft nodes, nodes with no recorded Jira parent (the
    attach-the-unassigned rule), or whose ``parent_key`` points at a ticket
    *inside* this plan. Default unchecked: ``parent_key`` points elsewhere —
    the ticket belongs to another epic (an external blocker, say), and pulling
    it over should be a deliberate choice."""
    root_id = root_epic['node_id']
    direct = {e['to'] for e in edges
              if e.get('category') == 'hierarchy' and e['from'] == root_id}
    plan_keys = {n.get('jira_key') for n in nodes} - {None}
    out: list[tuple[str, bool]] = []
    for n in nodes:
        nid = n['node_id']
        if nid == root_id or nid in direct:
            continue
        pk = n.get('parent_key')
        checked = (n.get('kind') != 'existing') or not pk or pk in plan_keys
        out.append((nid, checked))
    return out


def _epic_inherit_dialog(nodes: list, edges: list,
                         root_epic: dict) -> list[str] | None:
    """Checklist of deep descendants that should inherit the root Epic as their
    epic link on push (see :func:`_epic_inherit_candidates` for who appears and
    the check defaults). Returns the chosen node_ids, [] when there's nothing
    to ask, or None if the user cancels the push."""
    candidates = _epic_inherit_candidates(nodes, edges, root_epic)
    if not candidates:
        return []
    by_id = {n['node_id']: n for n in nodes}
    epic_name = root_epic.get('jira_key') or (_node_ticket(root_epic).summary
                                              or 'this epic')[:30]
    rows = []
    for nid, checked in candidates:
        n = by_id[nid]
        t = _node_ticket(n)
        label = f"{(n.get('jira_key') or t.ticket_type)}: {(t.summary or '(no title)')[:40]}"
        if n.get('parent_key') and not checked:
            label += f"   (currently under {n['parent_key']})"
        rows.append([sg.Checkbox(label, default=checked, key=f'-CHK-{nid}-')])
    layout = [
        [sg.Text(f"Set 'Child of {epic_name}' for:", font=('Helvetica', 11, 'bold'))],
        [sg.Text("These tickets aren't directly attached to the epic — Jira "
                 'only records their epic if it is set explicitly. Tickets '
                 'already under a different epic start unchecked.',
                 font=('Helvetica', 8))],
        [sg.Column(rows, scrollable=len(rows) > 10, vertical_scroll_only=True,
                   size=(480, min(300, 30 * len(rows))) if len(rows) > 10 else (None, None))],
        [sg.HSep()],
        [sg.Push(),
         sg.Button('Apply', key='-OK-', bind_return_key=True),
         sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Epic inheritance', layout, finalize=True, modal=True,
                  keep_on_top=True)
    w.bind('<Escape>', '-C-')
    bring_to_front(w)
    ev, vals = _read(w)
    w.close()
    if ev != '-OK-':
        return None
    return [nid for nid, _ in candidates if vals.get(f'-CHK-{nid}-')]


def _link_neighbors(issue: dict) -> list[tuple]:
    """Each issuelinks entry as ``(other_key, rel, src_key, dst_key, link_id)``.
    Direction mirrors create_issue_link's verified mapping: reverse=False ⇒ the
    edge runs inwardIssue → outwardIssue, and an issue's own entry names only
    the *other* endpoint."""
    ikey = issue.get('key')
    out = []
    for ln in ((issue.get('fields') or {}).get('issuelinks') or []):
        rel = (ln.get('type') or {}).get('name') or 'Relates'
        out_key = (ln.get('outwardIssue') or {}).get('key')
        in_key = (ln.get('inwardIssue') or {}).get('key')
        if out_key:                      # this issue → outward issue
            out.append((out_key, rel, ikey, out_key, ln.get('id')))
        elif in_key:                     # inward issue → this issue
            out.append((in_key, rel, in_key, ikey, ln.get('id')))
    return out


def _plan_from_jira(jira: JiraClient, config: dict, root_key: str,
                    max_depth: int = 6, max_nodes: int = 120) -> dict:
    """Hydrate an in-memory plan from Jira: the issue, its descendants
    (recursively — one JQL call per depth level, not per node), the tickets
    linked to any of them (one batch call, a single hop), and every
    relationship among them — all as existing nodes with edges already marked
    ``pushed``. Never saved to disk by itself (see :func:`_worth_saving`).

    Guards: ``max_depth`` levels / ``max_nodes`` issues cap a runaway tree.
    Linked tickets' own links are NOT expanded further — one hop, otherwise
    this would crawl the project."""
    epic_cf = _epic_link_cf(config)
    fields = ('summary,issuetype,status,issuelinks,description,priority,labels,'
              f'parent,{epic_cf}')
    root = jira.get_issue(root_key, fields=fields)
    rkey = root.get('key', root_key)

    # BFS over the hierarchy, one get_children call per level.
    tree: dict[str, dict] = {rkey: root}
    depth_of: dict[str, int] = {rkey: 0}
    level = [rkey]
    depth = 0
    while level and depth < max_depth and len(tree) < max_nodes:
        next_level = []
        for ch in jira.get_children(level, epic_cf, fields=fields):
            k = ch.get('key')
            if not k or k in tree:
                continue
            tree[k] = ch
            depth_of[k] = depth + 1
            next_level.append(k)
            if len(tree) >= max_nodes:
                break
        level = next_level
        depth += 1

    # Linked tickets: one hop off any tree member, fetched in one batch.
    linked_keys = sorted({other for issue in tree.values()
                          for other, *_ in _link_neighbors(issue)} - set(tree))[:40]
    linked = {i['key']: i for i in
              (jira.get_issues_by_keys(linked_keys, fields=fields)
               if linked_keys else [])}

    plan = new_plan(rkey, epic_key=rkey)
    summ = ((root.get('fields') or {}).get('summary') or '')[:24]
    plan['name'] = f"{rkey} {summ}".strip()

    # Nodes are created at placeholder positions; once the hierarchy edges
    # exist, _auto_layout arranges the proper tidy tree (epic alone on top,
    # children centered under parents, linked-only tickets below).
    by_key: dict[str, dict] = {}
    for k, issue in tree.items():
        node = _existing_node(issue, 0, 0, epic_cf=epic_cf)
        plan['nodes'].append(node)
        by_key[k] = node
    for k in sorted(linked):
        node = _existing_node(linked[k], 0, 0, epic_cf=epic_cf)
        plan['nodes'].append(node)
        by_key[k] = node

    # Hierarchy edges from each child's own parent / epic-link field (children
    # of different parents arrive in one level batch, so the field is the only
    # reliable attachment).
    for k, issue in tree.items():
        if k == rkey:
            continue
        f = issue.get('fields') or {}
        pkey = ((f.get('parent') or {}).get('key')) or f.get(epic_cf)
        pnode = by_key.get(str(pkey)) if pkey else None
        if pnode is None:
            continue
        plan['edges'].append({'from': pnode['node_id'], 'to': by_key[k]['node_id'],
                              **_hierarchy_rel(pnode), 'pushed': True})

    # Dependency links among everything fetched; each link appears on both of
    # its endpoints, so dedupe by link id.
    seen: set = set()
    for issue in list(tree.values()) + list(linked.values()):
        for other, rel, src, dst, raw_id in _link_neighbors(issue):
            lid = raw_id or (rel, src, dst)
            if lid in seen or src not in by_key or dst not in by_key:
                continue
            seen.add(lid)
            plan['edges'].append({'from': by_key[src]['node_id'],
                                  'to': by_key[dst]['node_id'],
                                  'category': 'link', 'rel': rel,
                                  'reverse': False, 'pushed': True,
                                  'link_id': raw_id})
    _auto_layout(plan['nodes'], plan['edges'])
    return plan


def _push_change_summary(plan: dict, by_id: dict) -> str:
    """Grouped, per-ticket list of what Push will do: one title line per ticket
    (key or summary), its changes indented beneath. Field names use their form
    labels (summary → Summary). Relationships involving a new node group under
    that node, alongside its Create line."""
    groups: dict[str, list[str]] = {}
    order: list[str] = []

    def _label(field: str) -> str:
        return FIELD_META.get(field, {}).get('label', field.replace('_', ' ').title())

    def _name(n: dict) -> str:
        return n.get('jira_key') or (_node_ticket(n).summary or '(no title)')[:40]

    def _is_new(n: dict) -> bool:
        return n.get('kind') != 'existing' and not n.get('jira_key')

    def add(name: str, line: str) -> None:
        if name not in groups:
            groups[name] = []
            order.append(name)
        groups[name].append(line)

    for n in plan.get('nodes', []):
        if _is_new(n):
            add(_name(n), f"Create ({_node_ticket(n).ticket_type})")
        elif n.get('orig_ticket'):
            for f, (ov, nv) in _node_field_diff(n['orig_ticket'],
                                                n.get('ticket') or {}).items():
                add(_name(n), f"{_label(f)}: '{ov[:30]}' → '{nv[:30]}'")
    for e in plan.get('edges', []):
        if e.get('pushed'):
            continue
        a, b = by_id.get(e.get('from')), by_id.get(e.get('to'))
        if not a or not b:
            continue
        line = f"Relationship: {_name(a)} — {_edge_label(e)} → {_name(b)}"
        owner = b if (_is_new(b) and not _is_new(a)) else a
        add(_name(owner), line)
    for u in plan.get('pending_unlinks') or []:
        rel = ('child' if u.get('hier') else u.get('rel')) or 'link'
        add(u.get('from_key') or '?',
            f"Remove relationship: {rel} → {u.get('to_key')}")

    lines: list[str] = []
    for name in order:
        lines.append(name)
        lines.extend(f"\t{c}" for c in groups[name])
    return '\n'.join(lines)


def _touches_existing(plan: dict, by_id: dict) -> bool:
    """True when a push would mutate content already in Jira: queued field edits,
    queued relationship removals, or new edges attached to keyed tickets. Gates
    the visual diff confirmation — pure-new plans keep the simple confirm."""
    if plan.get('pending_unlinks'):
        return True
    if any(n.get('orig_ticket') and n.get('jira_key') for n in plan.get('nodes', [])):
        return True
    for e in plan.get('edges', []):
        if e.get('pushed'):
            continue
        a, b = by_id.get(e.get('from')), by_id.get(e.get('to'))
        if (a and a.get('jira_key')) or (b and b.get('jira_key')):
            return True
    return False


def _confirm_push_with_preview(plan: dict, by_id: dict,
                               colors: dict | None = None) -> bool:
    """Visual diff of what Push will do, drawn with the same graph renderer:
    green = created, orange ✎ = fields updated, green arrow = new relationship,
    red ✕ = relationship removed. Returns True if the user confirms."""
    graph = sg.Graph((_CANVAS_W, _CANVAS_H), (0, _CANVAS_H), (_CANVAS_W, 0),
                     key='-PV-', background_color='#fafafa')
    layout = [
        [sg.Text('Review changes before pushing', font=('Helvetica', 13, 'bold'))],
        [sg.Text('green = will be created · orange ✎ = fields will update · '
                 'green arrow = new relationship · red ✕ = relationship removed',
                 font=('Helvetica', 8))],
        [graph],
        [sg.Multiline('', key='-DIFFS-', size=(112, 6), disabled=True,
                      font=('Consolas', 9))],
        [sg.Push(),
         sg.Button('Yes — push all changes', key='-YES-'),
         sg.Button('No', key='-NO-'),
         sg.Push()],
    ]
    w = sg.Window('Push all changes?', layout, finalize=True, modal=True,
                  keep_on_top=True)
    w.bind('<Escape>', '-NO-')
    bring_to_front(w)

    # Fit-to-canvas zoom so big hydrated graphs are fully visible in the review.
    all_nodes = plan.get('nodes', [])
    extent_x = max((n['x'] + _node_size(n)[0] for n in all_nodes), default=_CANVAS_W)
    extent_y = max((n['y'] + _node_size(n)[1] for n in all_nodes), default=_CANVAS_H)
    z = min(1.0, (_CANVAS_W - 24) / max(extent_x, 1), (_CANVAS_H - 24) / max(extent_y, 1))
    view = {n['node_id']: _view_node(n, z) for n in all_nodes}

    for e in plan.get('edges', []):
        a, b = view.get(e['from']), view.get(e['to'])
        if not a or not b:
            continue
        new = not e.get('pushed')
        _draw_directed_edge(graph, a, b, _edge_label(e),
                            color='#2e7d32' if new else '#9e9e9e',
                            label_color='#2e7d32' if new else '#1565c0',
                            label_offset=(0, 0))
    key_to_view = {n.get('jira_key'): view[n['node_id']]
                   for n in all_nodes if n.get('jira_key')}
    for u in plan.get('pending_unlinks') or []:
        a, b = key_to_view.get(u.get('from_key')), key_to_view.get(u.get('to_key'))
        rel = ('child' if u.get('hier') else u.get('rel')) or 'link'
        if a and b:
            _draw_directed_edge(graph, a, b, f'✕ {rel}',
                                color='#e53935', label_color='#e53935',
                                label_offset=(0, 0))
    for n in all_nodes:
        vn = view[n['node_id']]
        new = n.get('kind') != 'existing' and not n.get('jira_key')
        if new:
            _draw_node(graph, vn, line_color='#2e7d32', line_width=3,
                       head_prefix='+ ', zoom=z, colors=colors)
        elif n.get('orig_ticket'):
            _draw_node(graph, vn, line_color='#fb8c00', line_width=3,
                       head_prefix='✎ ', zoom=z, colors=colors)
        else:
            _draw_node(graph, vn, zoom=z, colors=colors)
    w['-DIFFS-'].update(_push_change_summary(plan, by_id) or '(no changes)')

    ev, _ = _read(w)
    w.close()
    return ev == '-YES-'


def show_plan_canvas(cache: Cache, jira: JiraClient, config: dict, plan: dict) -> None:
    """Draggable relationship graph for one initiative plan."""
    _migrate_plan(plan)
    nodes = plan.setdefault('nodes', [])
    edges = plan.setdefault('edges', [])
    by_id = {n['node_id']: n for n in nodes}
    epic_cf = _epic_link_cf(config)
    # Imports fetch the epic cf too, so nodes record their current Jira parent
    # (drives the epic-inheritance checklist defaults).
    hydrate_fields = ('summary,issuetype,status,issuelinks,parent,'
                      f'description,priority,labels,{epic_cf}')
    state = {'selected': None, 'selected_edge': None,
             'drag_node': None, 'drag_off': (0, 0), 'moved': False,
             'resize_node': None, 'body_press': False, 'pan_press': None,
             'zoom': 1.0, 'pan': (0, 0),
             'link_types': None, 'arrows': {}, 'hover': None,
             'arrow_armed': None, 'ui_armed': None,
             'colors': _node_colors(config), 'ui_rects': {},
             'link_from': None, 'ghost_arrow': None, 'hidden_edges': set()}

    graph = sg.Graph((_CANVAS_W, _CANVAS_H), (0, _CANVAS_H), (_CANVAS_W, 0),
                     key='-CANVAS-', enable_events=True, drag_submits=True,
                     background_color='#fafafa')
    layout = [
        [sg.Text(plan.get('name', 'Plan'), font=('Helvetica', 13, 'bold')),
         sg.Push(), sg.Text('', key='-PSTATUS-', font=('Helvetica', 8))],
        [sg.Button('(A) Add Ticket', key='-ADD-'),
         sg.Button('(⏎) Edit', key='-EDIT-', disabled=True),
         sg.Button('(Del) Delete', key='-DEL-', disabled=True),
         sg.Button('(L) Layout', key='-LAYOUT-'),
         sg.Push(),
         sg.Button('(P) Push to Jira', key='-PUSH-'),
         sg.Button('(Ctrl+S) Save', key='-PSAVE-'),
         sg.Button('Close', key='-PCLOSE-')],
        [graph],
        [sg.Text('Hover a node and click a side arrow to add a connected ticket · '
                 'yellow top arrow links to another node on the canvas · '
                 'drag the ≡ grip (top-right) to move, the ◢ grip (bottom-right) to '
                 'resize · click a node or a line to select it · '
                 'double-click an arrow to edit its relationship type · '
                 'drag empty space to pan, Ctrl+wheel to zoom.',
                 font=('Helvetica', 8))],
    ]
    window = sg.Window(f"Plan — {plan.get('name', '')}", layout, finalize=True,
                       return_keyboard_events=False)
    bring_to_front(window)
    # Canvas hotkeys — n/N/a/A spawn nodes; p/P pushes; Return edits; Delete
    # deletes; Ctrl+S saves; Escape cancels link mode or clears selection.
    for k, ev in [('n', '-NSPAWN-'), ('N', '-NSPAWN-'), ('a', '-ADD-'), ('A', '-ADD-'),
                  ('p', '-PUSH-'), ('P', '-PUSH-')]:
        window.bind(k, ev)
    window.bind('<Return>', '-EDIT-')
    window.bind('<Delete>', '-DEL-')
    window.bind('<Control-s>', '-PSAVE-')
    window.bind('<Control-S>', '-PSAVE-')
    window.bind('<Escape>', '-PESC-')
    window.bind('<Control-i>', '-IMPORT-')
    window.bind('<Control-I>', '-IMPORT-')
    window.bind('l', '-LAYOUT-')
    window.bind('L', '-LAYOUT-')

    figmap: dict = {}

    def _status(msg: str = '') -> None:
        z = state['zoom']
        ztxt = f" · {round(z * 100)}%" if abs(z - 1) > 1e-9 else ''
        window['-PSTATUS-'].update(
            (msg or f"{len(nodes)} node(s) · {len(edges)} edge(s)") + ztxt)

    # ── View transform (zoom + pan): screen = world·z + pan ─────────────────
    # Model coords stay world-space; only rendering/hit-testing transform.

    def _to_world(pt) -> tuple:
        z = state['zoom']
        px, py = state['pan']
        return ((pt[0] - px) / z, (pt[1] - py) / z)

    def _set_zoom(z: float, anchor=None) -> None:
        z = max(0.4, min(2.0, z))
        if anchor is None:
            anchor = (_CANVAS_W / 2, _CANVAS_H / 2)
        wx, wy = _to_world(anchor)  # keep this world point under the anchor
        state['zoom'] = z
        state['pan'] = (anchor[0] - wx * z, anchor[1] - wy * z)
        redraw()
        _status()

    # ── Hover arrows (the spawn affordance) ──────────────────────────────────
    # Drawn straight on the tk canvas from the <Motion> callback; graph coords
    # equal widget pixels for this Graph (origin top-left, y down).

    _GAP, _ARROW_L, _ARROW_HALF = 8, 20, 9

    def _clear_arrows() -> None:
        for fid in list(state['arrows']):
            try:
                graph.delete_figure(fid)
            except Exception:
                pass
        state['arrows'].clear()
        state['hover'] = None

    def _clear_ghost() -> None:
        """Remove the link-mode ghost arrow (cursor-following dashed line)."""
        if state.get('ghost_arrow') is not None:
            try:
                graph.Widget.delete(state['ghost_arrow'])
            except Exception:
                pass
            state['ghost_arrow'] = None

    def _draw_arrows(n: dict) -> None:
        _clear_arrows()
        state['hover'] = n['node_id']
        # Arrows render in screen space (constant pixel size at any zoom).
        vn = _view_node(n, state['zoom'], state['pan'])
        x, y, w, h = vn['x'], vn['y'], vn['w'], vn['h']
        cx, cy = x + w / 2, y + h / 2
        bases = {'E': ((x + w + _GAP, cy), (1, 0)),
                 'W': ((x - _GAP, cy), (-1, 0)),
                 'S': ((cx, y + h + _GAP), (0, 1)),
                 'N': ((cx, y - _GAP), (0, -1))}
        for side, ((bx, by), (dx, dy)) in bases.items():
            px, py = -dy, dx  # perpendicular
            pts = [(bx + px * _ARROW_HALF, by + py * _ARROW_HALF),
                   (bx - px * _ARROW_HALF, by - py * _ARROW_HALF),
                   (bx + dx * _ARROW_L, by + dy * _ARROW_L)]
            # N arrow is soft yellow (link mode); E/W/S are the usual slate.
            arrow_fill = '#ffe082' if side == 'N' else '#90a4ae'
            try:
                # stipple ≈ translucency (tk canvas has no real alpha)
                fid = graph.Widget.create_polygon(
                    *[c for p in pts for c in p],
                    fill=arrow_fill, stipple='gray50', outline='')
            except Exception:
                return
            state['arrows'][fid] = (n['node_id'], side)

    def _node_at(px, py) -> dict | None:
        # reversed: later nodes draw on top, so they win overlapping hit-tests.
        for n in reversed(nodes):
            w, h = _node_size(n)
            if n['x'] <= px <= n['x'] + w and n['y'] <= py <= n['y'] + h:
                return n
        return None

    def _handle_at(px, py) -> tuple | None:
        """('move'|'resize', node) when the (world) point is inside a corner
        grip: top-right ≡ moves the node, bottom-right ◢ resizes it. The grip
        zone is constant in *screen* pixels, so it scales inversely in world."""
        zone = _HANDLE / max(state['zoom'], 0.1)
        for n in reversed(nodes):
            w, h = _node_size(n)
            x, y = n['x'], n['y']
            if x + w - zone <= px <= x + w:
                if y <= py <= y + zone:
                    return 'move', n
                if y + h - zone <= py <= y + h:
                    return 'resize', n
        return None

    def _on_motion(ev) -> None:
        if (state['drag_node'] is not None or state['resize_node'] is not None
                or state['pan_press'] is not None):
            return
        # Ghost arrow while in link mode
        if state.get('link_from'):
            src = by_id.get(state['link_from'])
            if src is not None:
                vn = _view_node(src, state['zoom'], state['pan'])
                cx = vn['x'] + vn['w'] / 2
                cy = vn['y'] + vn['h'] / 2
                _clear_ghost()
                state['ghost_arrow'] = graph.Widget.create_line(
                    cx, cy, ev.x, ev.y,
                    fill='#ffe082', dash=(6, 4), arrow='last', width=2)
        wx, wy = _to_world((ev.x, ev.y))
        n = _node_at(wx, wy)
        # Cursor hints over the grips.
        handle = _handle_at(wx, wy)
        try:
            graph.Widget.config(cursor='fleur' if handle and handle[0] == 'move'
                                else 'size_nw_se' if handle else '')
        except Exception:
            pass
        if n is not None:
            if state['hover'] != n['node_id']:
                _draw_arrows(n)
            return
        if state['hover'] is not None:
            hov = by_id.get(state['hover'])
            if hov:
                w, h = _node_size(hov)
                # Keep the arrows alive while crossing the gap toward them
                # (halo covers the screen-space arrow extent, in world units).
                halo = (_GAP + _ARROW_L + 6) / max(state['zoom'], 0.1)
                if (hov['x'] - halo <= wx <= hov['x'] + w + halo
                        and hov['y'] - halo <= wy <= hov['y'] + h + halo):
                    return
            _clear_arrows()

    def _on_wheel(ev) -> None:
        # Windows wheel delta is ±120 per notch; anchor at the pointer.
        _set_zoom(state['zoom'] * (1.2 if ev.delta > 0 else 1 / 1.2),
                  anchor=(ev.x, ev.y))

    # add='+' is load-bearing: PySimpleGUI delivers drag_submits events through
    # its own <Motion> binding on this canvas — a plain bind() would replace it
    # and silently kill node dragging.
    graph.Widget.bind('<Motion>', _on_motion, add='+')
    graph.Widget.bind('<Leave>', lambda e: _clear_arrows(), add='+')
    graph.Widget.bind('<Control-MouseWheel>', _on_wheel, add='+')
    graph.Widget.bind('<Double-Button-1>',
                      lambda e: window.write_event_value('-NODE-DBL-', (e.x, e.y)),
                      add='+')

    # ── Rendering & selection ────────────────────────────────────────────────

    def redraw() -> None:
        graph.erase()
        state['ghost_arrow'] = None
        state['arrows'].clear()
        state['hover'] = None
        state['ui_rects'].clear()
        z, pan = state['zoom'], state['pan']
        view = {nid: _view_node(n, z, pan) for nid, n in by_id.items()}
        label_positions = []
        state['hidden_edges'] = _implied_epic_edges(nodes, edges)
        for i, e in enumerate(edges):
            if i in state['hidden_edges']:
                continue
            a, b = view.get(e['from']), view.get(e['to'])
            if not a or not b:
                continue
            sel = i == state['selected_edge']
            seg = _edge_segment(a, b)
            label_offset = (0.0, 0.0)
            if seg:
                mid = ((seg[0][0] + seg[1][0]) / 2, (seg[0][1] + seg[1][1]) / 2)
                # Stack each colliding label one step further along the
                # perpendicular; tracking the *placed* (offset) positions keeps
                # 3+ coincident labels apart, not just the first pair.
                conflicts = sum(1 for p in label_positions
                                if math.hypot(mid[0] - p[0], mid[1] - p[1]) < 18)
                if conflicts:
                    dx, dy = seg[1][0] - seg[0][0], seg[1][1] - seg[0][1]
                    L = math.hypot(dx, dy) or 1
                    label_offset = (-dy / L * 16 * conflicts, dx / L * 16 * conflicts)
                label_positions.append((mid[0] + label_offset[0],
                                        mid[1] + label_offset[1]))
            _draw_directed_edge(graph, a, b, _edge_label(e),
                                color='#fbc02d' if sel else '#9e9e9e',
                                label_color='#f57f17' if sel else '#1565c0',
                                label_offset=label_offset)
        figmap.clear()
        for n in nodes:
            t = _node_ticket(n)
            sel = n['node_id'] == state['selected']
            existing = n.get('kind') == 'existing'
            # Red = draft missing required fields; orange = queued edits to a
            # pushed ticket (applied on Push).
            incomplete = (not existing) and not t.is_valid()[0]
            dirty = bool(n.get('orig_ticket'))
            if sel:
                lc, lw = '#ffeb3b', 3
            elif incomplete:
                lc, lw = '#e53935', 3
            elif dirty:
                lc, lw = '#fb8c00', 3
            else:
                lc, lw = 'black', 1
            prefix = '⚠ ' if incomplete else ('✎ ' if dirty else '')
            rect, txt = _draw_node(graph, view[n['node_id']], lc, lw, prefix,
                                   zoom=z, colors=state['colors'])
            figmap[n['node_id']] = {'rect': rect, 'text': txt}

        # ── Canvas-space UI: zoom buttons (top-right) — always on top ─────────
        # These are drawn in screen coords and never move with zoom/pan.
        for i, (sym, action) in enumerate([('−', 'zout'), ('⊙', 'zreset'), ('+', 'zin')]):
            bx = _CANVAS_W - 8 - (3 - i) * 26
            by_ = 8
            x1, y1, x2, y2 = bx, by_, bx + 22, by_ + 22
            graph.draw_rectangle((x1, y1), (x2, y2),
                                 fill_color='#eceff1', line_color='#90a4ae')
            graph.draw_text(sym, (bx + 11, by_ + 11), color='#37474f',
                            font=('Helvetica', 10, 'bold'))
            state['ui_rects'][action] = (x1, y1, x2, y2)

        # ── Color legend (bottom-left) — one swatch per type, stacked upward ──
        row_y = _CANVAS_H - 10
        for tname, tcolor in state['colors'].items():
            x1, y1, x2, y2 = 10, row_y - 10, 20, row_y
            graph.draw_rectangle((x1, y1), (x2, y2),
                                 fill_color=tcolor, line_color=tcolor)
            graph.draw_text(tname, (25, row_y - 5), color='#607d8b',
                            font=('Helvetica', 7), text_location=sg.TEXT_LOCATION_LEFT)
            state['ui_rects'][f'legend:{tname}'] = (x1, y1 - 2, x2 + 60, y2 + 2)
            row_y -= 14

    def _ui_at(pt) -> str | None:
        """Return the action key whose ui_rect contains the screen point, else None."""
        px, py = pt
        for action, (x1, y1, x2, y2) in state['ui_rects'].items():
            if x1 <= px <= x2 and y1 <= py <= y2:
                return action
        return None

    def fig_to_node(figs) -> str | None:
        rev = {}
        for nid, f in figmap.items():
            rev[f['rect']] = nid
            rev[f['text']] = nid
        # find_overlapping returns bottom→top; scan from the end so the node
        # drawn on top wins when nodes overlap.
        for f in reversed(list(figs or [])):
            if f in rev:
                return rev[f]
        return None

    def set_selected(nid=None, edge_idx=None) -> None:
        state['selected'] = nid
        state['selected_edge'] = edge_idx
        has = nid is not None or edge_idx is not None
        window['-EDIT-'].update(disabled=not has)
        window['-DEL-'].update(disabled=not has)
        redraw()

    def add_node(ticket: Ticket, x=None, y=None) -> dict:
        if x is None:
            x = 60 + (len(nodes) % 5) * 172
            y = 60 + (len(nodes) // 5) * 96
        node = _new_node(ticket, x, y)
        nodes.append(node)
        by_id[node['node_id']] = node
        return node

    def add_existing(issue: dict, x=None, y=None) -> dict:
        if x is None:
            x = 60 + (len(nodes) % 5) * 172
            y = 60 + (len(nodes) // 5) * 96
        node = _existing_node(issue, x, y, epic_cf=epic_cf)
        nodes.append(node)
        by_id[node['node_id']] = node
        return node

    # ── Spawn & edge-edit flows ──────────────────────────────────────────────

    def _root_epic_info() -> tuple[str | None, bool]:
        """(epic_key, epic_is_draft) for the plan's root Epic, or (None, False)
        when there isn't one. Feeds the node editor's 'This epic' button."""
        root = _root_epic_node(nodes, edges)
        if root is None:
            return None, False
        return root.get('jira_key'), not root.get('jira_key')

    def _rel_or_block(source: dict, other: dict, payload: dict) -> bool:
        """Apply a relationship unless it would nest a child under a ticket
        type Jira can't parent (the create/update would just fail later, after
        half the tree is pushed). The options list already filters these out
        where both ends are known; this guards the spawn flow, where the new
        node's type is chosen *after* the relationship."""
        if payload.get('category') == 'hierarchy':
            parent = other if payload.get('dir') == 'parent' else source
            if not _can_be_parent(parent):
                show_error('Only an Epic (or Initiative) can be a hierarchy '
                           'parent in Jira — use a dependency link '
                           "(e.g. 'relates to') instead.")
                return False
        _apply_relationship(edges, source, other, payload, plan, by_id)
        return True

    def _spawn_from(source: dict | None, side: str | None) -> None:
        """Add a node via the title-first dialog — from a hover arrow (with a
        relationship to ``source``) or from Add Ticket (independent)."""
        res = _spawn_dialog(jira, cache, state, with_relationship=source is not None,
                            source_node=source)
        if not res:
            return
        action, title, rel = res
        x = y = None
        if source is not None:
            sw, sh = _node_size(source)
            off = {'E': (sw + 90, 0), 'W': (-(_NODE_W + 90), 0),
                   'S': (0, sh + 80), 'N': (0, -(_NODE_H + 80))}[side or 'E']
            x = max(4, min(source['x'] + off[0], _CANVAS_W - _NODE_W - 4))
            y = max(4, min(source['y'] + off[1], _CANVAS_H - _NODE_H - 4))
        if action == 'create':
            ttype = show_type_selector()
            if not ttype:
                return
            t = TICKET_CLASSES[ttype]()
            t.summary = title
            ek, ed = _root_epic_info()
            if not _planner_node_edit(t, epic_key=ek, epic_is_draft=ed):
                return
            node = add_node(t, x, y)
        else:  # import an existing Jira ticket
            existing_keys = {n.get('jira_key') for n in nodes if n.get('jira_key')}
            issues_list = _pick_existing_issue(jira, config, initial_query=title,
                                               existing_keys=existing_keys)
            if not issues_list:
                return
            # Search results carry only summary/type/status — hydrate the full
            # issue (description, priority, labels, issuelinks ids) so the node
            # reflects Jira's real state. One GET behind an explicit action.
            node = None
            base_x, base_y = x, y
            for i, issue in enumerate(issues_list):
                nx = base_x if base_x is None else base_x + i * ((_NODE_W or 150) + 20)
                ny = base_y
                status, full, _ = run_with_busy(
                    lambda: jira.get_issue(issue['key'], fields=hydrate_fields),
                    message=f"Loading {issue['key']}…")
                node = add_existing(full if status == 'ok' and full else issue, nx, ny)
                if source is not None and rel:
                    _rel_or_block(source, node, rel)
            if node is None:
                return
        if action == 'create':
            if source is not None and rel:
                _rel_or_block(source, node, rel)
        set_selected(nid=node['node_id'])
        _status()

    def _pick_relationship_dialog(opts: list, current_label: str,
                                   title: str = 'Edit relationship') -> dict | None:
        """Small combo-picker for choosing a relationship. Returns the selected
        payload dict or None if cancelled. Shared by _edit_edge and link mode."""
        labels = [o[0] for o in opts]
        cur = current_label if current_label in labels else labels[0]
        lay = [[sg.Text(f'{title}  (the arrow will point along the phrase)',
                        font=('Helvetica', 11, 'bold'))],
               [sg.Combo(labels, default_value=cur, key='-R-', readonly=True,
                         size=(44, 1))],
               [sg.Push(), sg.Button('Apply', key='-OK-'), sg.Button('Cancel', key='-C-')]]
        w = sg.Window(title, lay, finalize=True, modal=True, keep_on_top=True)
        w.bind('<Escape>', '-C-')
        w.bind('<Return>', '-OK-')
        bring_to_front(w)
        ev, vals = _read(w)
        w.close()
        if ev != '-OK-' or vals.get('-R-') not in labels:
            return None
        return dict(opts[labels.index(vals['-R-'])][1])

    def _edit_edge(idx: int) -> None:
        """Re-type the selected relationship; a pushed one is queued for removal
        in Jira and replaced by the new (unpushed) edge."""
        e = edges[idx]
        src_n, tgt_n = by_id.get(e['from']), by_id.get(e['to'])
        opts = _relationship_options(jira, cache, state,
                                     source_node=src_n, target_node=tgt_n)
        labels = [o[0] for o in opts]
        cur = labels[0]
        if e.get('category') == 'link':
            for lbl, payload in opts:
                if (payload.get('category') == 'link'
                        and payload.get('rel') == e.get('rel')
                        and bool(payload.get('reverse')) == bool(e.get('reverse'))):
                    cur = lbl
                    break
        payload = _pick_relationship_dialog(opts, cur, title='Change relationship')
        if payload is None:
            return
        a, b = by_id.get(e['from']), by_id.get(e['to'])
        if not a or not b:
            return
        old = edges.pop(idx)
        if old.get('pushed'):
            if old.get('category') == 'link':
                _queue_unlink(plan, old, by_id)
            else:
                # Hierarchy: only clear in Jira when this isn't a same-child,
                # same-rel overwrite (push would set the new value anyway, and
                # the unlink phase runs after — clearing would undo it).
                same_overwrite = (payload.get('category') == 'hierarchy'
                                  and payload.get('dir') != 'parent'
                                  and _hierarchy_rel(a)['rel'] == old.get('rel'))
                if not same_overwrite:
                    _queue_unlink(plan, old, by_id)
        _apply_relationship(edges, a, b, payload, plan, by_id)
        set_selected()
        _status()

    def edit_node(n: dict) -> None:
        """Edit the ticket fields of node ``n`` in place; queue the changes for Push
        when saving an existing (pushed) ticket."""
        t = _node_ticket(n)
        ek, ed = _root_epic_info()
        if _planner_node_edit(t, jira=jira, node=n, epic_key=ek, epic_is_draft=ed):
            # First edit of a pushed ticket snapshots the Jira-side state so Push
            # can send exactly the changed fields.  Restore may have already set
            # orig_ticket — the guard below won't clobber that baseline.
            if n.get('kind') == 'existing' and not n.get('orig_ticket'):
                n['orig_ticket'] = dict(n['ticket'])
            n['ticket'] = t.to_dict()
            if (n.get('orig_ticket')
                    and not _node_field_diff(n['orig_ticket'], n['ticket'])):
                n.pop('orig_ticket', None)  # edited back — nothing queued
            redraw()

    redraw()
    _status()

    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-PCLOSE-'):
            if _worth_saving(plan):
                cache.save_plan(plan)
            else:
                # A plan with no local work left must not leave a stale file
                # behind — it would resurrect deleted nodes on reopen.
                cache.delete_plan(plan['plan_id'])
            break

        if event == '-CANVAS-':
            pt = values['-CANVAS-']
            if (pt == (None, None)
                    or state['arrow_armed'] is not None
                    or state['ui_armed'] is not None):
                continue
            wpt = _to_world(pt)
            if state['drag_node'] is not None:
                n = by_id[state['drag_node']]
                n['x'] = int(wpt[0] - state['drag_off'][0])
                n['y'] = int(wpt[1] - state['drag_off'][1])
                state['moved'] = True
                redraw()
            elif state['resize_node'] is not None:
                n = by_id[state['resize_node']]
                n['w'] = max(_MIN_W, int(wpt[0] - n['x']))
                n['h'] = max(_MIN_H, int(wpt[1] - n['y']))
                state['moved'] = True
                redraw()
            elif state['pan_press'] is not None:
                p0, pan0 = state['pan_press']
                state['pan'] = (pan0[0] + pt[0] - p0[0], pan0[1] + pt[1] - p0[1])
                state['moved'] = True
                redraw()
            elif not state['body_press']:  # first press of this gesture
                # Check canvas UI buttons before anything else (screen coords).
                action = _ui_at(pt)
                if action is not None:
                    state['ui_armed'] = action
                    continue
                figs = graph.get_figures_at_location(pt)
                arrow = next((state['arrows'][f] for f in figs
                              if f in state['arrows']), None)
                if arrow is not None:
                    # Press landed on a hover arrow — spawn on release, no drag.
                    state['arrow_armed'] = arrow
                    continue
                handle = _handle_at(*wpt)
                if handle is not None:
                    kind, n = handle
                    if kind == 'move':
                        state.update(drag_node=n['node_id'], moved=False,
                                     drag_off=(wpt[0] - n['x'], wpt[1] - n['y']))
                    else:
                        state.update(resize_node=n['node_id'], moved=False)
                elif _node_at(*wpt) is not None:
                    # Body press: selection only, resolved on release. Sticky so
                    # dragging across a grip mid-gesture doesn't start a move.
                    state['body_press'] = True
                else:
                    # Empty canvas: pan gesture (screen-space deltas).
                    state['pan_press'] = (pt, state['pan'])
                    state['moved'] = False
            continue

        if event == '-CANVAS-+UP':
            pt = values.get('-CANVAS-')
            armed, state['arrow_armed'] = state['arrow_armed'], None
            ui_action, state['ui_armed'] = state['ui_armed'], None
            # Pop link_from early so every path below can inspect it.
            link_src_id, state['link_from'] = state['link_from'], None
            dragged = ((state['drag_node'] is not None
                        or state['resize_node'] is not None) and state['moved'])
            panned = state['pan_press'] is not None and state['moved']
            grabbed = state['drag_node'] or state['resize_node']
            state.update(drag_node=None, resize_node=None, pan_press=None,
                         moved=False, body_press=False)
            if ui_action is not None:
                if link_src_id:
                    state['link_from'] = link_src_id  # keep link mode armed
                if ui_action == 'zin':
                    _set_zoom(state['zoom'] * 1.2)
                elif ui_action == 'zout':
                    _set_zoom(state['zoom'] / 1.2)
                elif ui_action == 'zreset':
                    state['zoom'] = 1.0
                    state['pan'] = (0, 0)
                    redraw()
                    _status()
                elif ui_action.startswith('legend:'):
                    tname = ui_action.split(':', 1)[1]
                    _, hexcolor = colorchooser.askcolor(
                        color=state['colors'].get(tname),
                        title=f'{tname} color',
                        parent=graph.Widget)
                    if hexcolor:
                        state['colors'][tname] = hexcolor
                        config.setdefault('planner', {}).setdefault('node_colors', {})[tname] = hexcolor
                        from .main import save_config  # lazy — avoids import cycle
                        save_config(config)
                        redraw()
                continue
            if armed is not None:
                src = by_id.get(armed[0])
                if src is not None:
                    if armed[1] == 'N':
                        # N arrow enters link mode rather than spawning a new node.
                        state['link_from'] = armed[0]
                        set_selected(nid=armed[0])
                        _status('Link mode: click the target node (Esc cancels)')
                    else:
                        _spawn_from(src, armed[1])
                continue
            if panned:
                # A drag while in link mode just moves the node; keep link mode armed.
                if link_src_id:
                    state['link_from'] = link_src_id
                continue  # the view moved; selection unchanged
            if dragged:
                # Node/resize drag while link mode is active — keep link mode alive.
                if link_src_id:
                    state['link_from'] = link_src_id
                set_selected(nid=grabbed)
            else:
                valid = pt and pt != (None, None)
                clicked = fig_to_node(graph.get_figures_at_location(pt)) if valid else None
                if link_src_id is not None:
                    # Link mode: this plain click resolves the target.
                    _clear_ghost()
                    if not clicked or clicked == link_src_id:
                        # Same node or empty click — cancel link mode.
                        _status()
                    else:
                        src_node = by_id.get(link_src_id)
                        tgt_node = by_id.get(clicked)
                        if src_node and tgt_node:
                            opts = _relationship_options(jira, cache, state,
                                                         source_node=src_node,
                                                         target_node=tgt_node)
                            labels = [o[0] for o in opts]
                            payload = _pick_relationship_dialog(
                                opts, labels[0], title='Link')
                            if payload is not None:
                                _rel_or_block(src_node, tgt_node, payload)
                            set_selected()
                            _status()
                        else:
                            _status()
                elif clicked is not None:
                    set_selected(nid=clicked)
                else:
                    # ~10 screen px tolerance regardless of zoom.
                    eidx = (_edge_at_point(_to_world(pt), by_id, edges,
                                           threshold=10 / max(state['zoom'], 0.1),
                                           skip=state['hidden_edges'])
                            if valid else None)
                    set_selected(edge_idx=eidx)
            continue

        if event == '-NODE-DBL-':
            # Double-click to edit: cancel any in-flight gesture, then open the
            # node editor directly — no extra click required.
            state.update(drag_node=None, resize_node=None, pan_press=None,
                         moved=False, body_press=False)
            state['arrow_armed'] = None
            state['ui_armed'] = None
            dbl_pt = values.get('-NODE-DBL-')
            if dbl_pt and dbl_pt != (None, None):
                wx, wy = _to_world(dbl_pt)
                dbl_node = _node_at(wx, wy)
                if dbl_node:
                    set_selected(nid=dbl_node['node_id'])
                    edit_node(dbl_node)
                else:
                    # Try double-clicking an edge
                    eidx = _edge_at_point(_to_world(dbl_pt), by_id, edges,
                                          threshold=10 / max(state['zoom'], 0.1),
                                          skip=state['hidden_edges'])
                    if eidx is not None:
                        set_selected(edge_idx=eidx)
                        _edit_edge(eidx)

        elif event == '-NSPAWN-':
            # n/N hotkey: spawn from the selected node (S-side) or add freely.
            if state['selected'] and state['selected'] in by_id:
                _spawn_from(by_id[state['selected']], 'S')
            else:
                _spawn_from(None, None)

        elif event == '-PESC-':
            # Escape: cancel link mode first; else clear selection. Never closes.
            if state['link_from']:
                state['link_from'] = None
                _clear_ghost()
                _status()
            elif state['selected'] is not None or state['selected_edge'] is not None:
                set_selected()

        elif event == '-ADD-':
            _spawn_from(None, None)

        elif event == '-IMPORT-':
            existing = {n.get('jira_key') for n in nodes if n.get('jira_key')}
            issues_list = _pick_existing_issue(jira, config, existing_keys=existing)
            last_node = None
            for issue in issues_list:
                status, full, _ = run_with_busy(
                    lambda: jira.get_issue(issue['key'], fields=hydrate_fields),
                    message=f"Loading {issue['key']}…")
                last_node = add_existing(full if status == 'ok' and full else issue)
                set_selected(nid=last_node['node_id'])
            _status()

        elif event == '-EDIT-':
            if state['selected_edge'] is not None and state['selected_edge'] < len(edges):
                _edit_edge(state['selected_edge'])
            elif state['selected']:
                n = by_id.get(state['selected'])
                if n:
                    edit_node(n)

        elif event == '-DEL-':
            if state['selected_edge'] is not None and state['selected_edge'] < len(edges):
                e = edges[state['selected_edge']]
                warn = ('Remove this relationship?'
                        + ('\n\nIt already exists in Jira — it will be removed '
                           'there on the next Push.' if e.get('pushed') else ''))
                if _yn_dialog(warn):
                    if e.get('pushed'):
                        _queue_unlink(plan, e, by_id)
                    edges.pop(state['selected_edge'])
                    set_selected()
            elif state['selected']:
                nid = state['selected']
                doomed = [e for e in edges if e['from'] == nid or e['to'] == nid]
                pushed_conns = [e for e in doomed if e.get('pushed')]
                msg = 'Delete this node and its connections?'
                if pushed_conns:
                    msg += (f"\n\n{len(pushed_conns)} of its relationship(s) already "
                            "exist in Jira and will be removed there on the next Push. "
                            "(The ticket itself is never deleted from Jira.)")
                if _yn_dialog(msg):
                    for e in pushed_conns:
                        _queue_unlink(plan, e, by_id)
                    nodes[:] = [n for n in nodes if n['node_id'] != nid]
                    edges[:] = [e for e in edges if e['from'] != nid and e['to'] != nid]
                    by_id.pop(nid, None)
                    set_selected()

        elif event == '-PSAVE-':
            cache.save_plan(plan)
            sg.popup_quick_message('Plan saved.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')

        elif event == '-LAYOUT-':
            _auto_layout(nodes, edges)
            set_selected()
            _status()

        elif event == '-PUSH-':
            if not nodes and not plan.get('pending_unlinks'):
                sg.popup('Nothing to push yet.', modal=True, keep_on_top=True)
                continue
            # Validation first: don't let the user review/confirm a diff that
            # could never apply.
            # Epic inheritance comes FIRST: drafts that will receive the epic
            # from the push (checked here, or hierarchy children) must not be
            # flagged for an empty epic_link by the required-fields gate.
            root_epic = _root_epic_node(nodes, edges)
            inherit_ids: list[str] = []
            if root_epic is not None:
                checked = _epic_inherit_dialog(nodes, edges, root_epic)
                if checked is None:
                    continue  # user cancelled the push
                inherit_ids = checked
            incomplete = _incomplete_nodes(plan, inherit_ids)
            if incomplete:
                show_error('Push blocked — fix required fields first:\n  '
                           + '\n  '.join(incomplete), title='Push to Jira')
                continue
            # Touching content already in Jira warrants the visual diff review;
            # a pure-new plan keeps the simple confirm.
            if _touches_existing(plan, by_id):
                go = _confirm_push_with_preview(plan, by_id, colors=state['colors'])
            else:
                go = _yn_dialog('Create the draft tickets and links in Jira now?')
            if go:
                cache.save_plan(plan)  # state on disk before any network work
                status, result, tb = run_with_busy(
                    lambda: _push_plan_to_jira(jira, config, plan,
                                               inherit_ids=inherit_ids),
                    message='Pushing plan to Jira…')
                if status == 'cancelled':
                    # The worker finishes in the background; keys land in `plan`.
                    _status('Push abandoned — reopen the plan later to see what landed')
                    continue
                if status == 'error':
                    cache.save_plan(plan)
                    redraw()
                    show_error(f"Push failed:\n{result}", tb=tb)
                else:
                    summary, clean = result
                    if clean:
                        # Jira now holds the whole tree — retire the local plan.
                        # Open from Jira can rebuild the graph on demand.
                        cache.delete_plan(plan['plan_id'])
                        sg.popup(summary + '\n\nAll pushed — the local plan was '
                                 'removed. Use "Open from Jira" to revisit it.',
                                 title='Push to Jira', modal=True, keep_on_top=True)
                        break
                    cache.save_plan(plan)
                    redraw()
                    sg.popup(summary, title='Push to Jira', modal=True, keep_on_top=True)

        _status()

    window.close()


def _plan_label(p: dict, project_key: str) -> str:
    """One picker row. Jira-backed plans (name starts with the project key) that
    still hold unpushed local work get a leading '*' — the offline tell that
    edits to live tickets are waiting."""
    star = (p.get('name', '').startswith(project_key + '-')
            and _worth_saving(p))
    prefix = '* ' if star else '  '
    return (prefix
            + f"{p.get('name', '(unnamed)'):30s}  {len(p.get('nodes', []))} node(s)"
            f"   {(p.get('created_at') or '')[:10]}")


def show_plan_picker(cache: Cache, jira: JiraClient, config: dict) -> None:
    """List initiative plans; open / create / delete. Loops until closed."""
    while True:
        plans = cache.list_plans()
        proj_key = (config.get('jira') or {}).get('project_key', '')
        labels = [_plan_label(p, proj_key) for p in plans]
        any_starred = any(lbl.startswith('* ') for lbl in labels)
        starred_row = ([sg.Text('* unpushed local changes', font=('Helvetica', 8))]
                       if any_starred else [])
        layout = [
            [sg.Text('Initiative Plans', font=('Helvetica', 13, 'bold'))],
            [sg.Listbox(labels, size=(60, min(len(labels) + 1, 12)), key='-PL-',
                        font=('Consolas', 10), enable_events=False,
                        select_mode=sg.LISTBOX_SELECT_MODE_BROWSE)],
            starred_row,
            [sg.Push(),
             sg.Button('Open', key='-OPEN-'),
             sg.Button('(O) Open from Jira', key='-OPENJ-',
                       tooltip='Load an existing ticket and graph its children '
                               'and dependency links'),
             sg.Button('(N) New plan', key='-NEWP-'),
             sg.Button('Delete', key='-DELP-'),
             sg.Button('Close', key='-CLOSE-')],
        ]
        window = sg.Window('Plans', layout, finalize=True, modal=True, keep_on_top=True)
        window.bind('<Escape>', '-CLOSE-')
        window.bind('<Return>', '-OPEN-')
        window.bind('o', '-OPENJ-')
        window.bind('O', '-OPENJ-')
        window.bind('n', '-NEWP-')
        window.bind('N', '-NEWP-')
        window.bind('<Delete>', '-DELP-')
        bring_to_front(window)
        if plans:
            _soft_select(window, 0, key='-PL-')
        ev, vals = _read(window)
        window.close()

        if ev in (sg.WIN_CLOSED, '-CLOSE-'):
            return
        if ev == '-OPENJ-':
            issues = _pick_existing_issue(jira, config)
            issue = issues[0] if issues else None
            if issue:
                status, plan, tb = run_with_busy(
                    lambda: _plan_from_jira(jira, config, issue['key']),
                    message=f"Loading {issue['key']} from Jira…")
                if status == 'error':
                    show_error(f"Could not load {issue['key']}:\n{plan}", tb=tb)
                elif status == 'ok':
                    show_plan_canvas(cache, jira, config, plan)
        elif ev == '-NEWP-':
            name = sg.popup_get_text('Name this initiative:', title='New plan',
                                     keep_on_top=True)
            if name and name.strip():
                plan = new_plan(name.strip())
                # Seed the initiative's Epic from the name so the canvas isn't blank
                # and the plan has an anchor. It's an ordinary draft node thereafter.
                epic = TICKET_CLASSES['Epic']()
                epic.summary = name.strip()
                epic.epic_name = name.strip()
                plan['nodes'].append(_new_node(epic, 60, 40))
                cache.save_plan(plan)
                show_plan_canvas(cache, jira, config, plan)
        elif ev in ('-OPEN-', '-DELP-') and vals.get('-PL-'):
            idx = labels.index(vals['-PL-'][0])
            target = plans[idx]
            if ev == '-OPEN-':
                plan = cache.load_plan(target['plan_id'])
                if plan:
                    _migrate_plan(plan)
                    show_plan_canvas(cache, jira, config, plan)
            elif _yn_dialog(f"Delete plan '{target.get('name')}'?"):
                cache.delete_plan(target['plan_id'])
