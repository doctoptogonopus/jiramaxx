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
import PySimpleGUI as sg
from .models import Ticket, TICKET_CLASSES, FIELD_META, ticket_from_dict
from .cache import Cache, new_plan
from .api import JiraClient
from .utils import safe_read as _read, show_error, bring_to_front, run_with_busy
from .ui import _build_field_row, _soft_select, show_type_selector, _epic_link_cf

_NODE_W, _NODE_H = 150, 50          # default size; nodes carry their own w/h once resized
_MIN_W, _MIN_H = 90, 36
_HANDLE = 16                        # px hit zone of the corner grips
_CANVAS_W, _CANVAS_H = 920, 560
_NODE_COLORS = {'Story': '#1565c0', 'Bug': '#c62828', 'Task': '#2e7d32',
                'Epic': '#6a1b9a', 'Initiative': '#00838f'}


def _node_size(n: dict) -> tuple[int, int]:
    return int(n.get('w') or _NODE_W), int(n.get('h') or _NODE_H)


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


def _set_parent_edge(edges: list, parent_node: dict, child_node: dict) -> None:
    """Make ``child_node`` a hierarchy-child of ``parent_node``. A node has exactly
    one hierarchy parent, so any existing hierarchy edge into the child is dropped
    first, then the inferred edge (parent → child) is added."""
    cid = child_node['node_id']
    edges[:] = [e for e in edges
                if not (e.get('category') == 'hierarchy' and e.get('to') == cid)]
    edges.append({'from': parent_node['node_id'], 'to': cid, **_hierarchy_rel(parent_node)})


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


def _edge_at_point(pt, by_id: dict, edges: list, threshold: float = 6.0) -> int | None:
    """Index of the edge whose drawn segment is nearest to ``pt`` (within the
    threshold), or None. Lets arrows be selected like nodes."""
    best, best_d = None, threshold
    for i, e in enumerate(edges):
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
                        color: str = '#9e9e9e', label_color: str = '#1565c0') -> None:
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
    graph.draw_text(label, ((ca[0] + tip[0]) / 2, (ca[1] + tip[1]) / 2),
                    color=label_color, font=('Helvetica', 7))


def _draw_node(graph, n: dict, line_color: str = 'black', line_width: int = 1,
               head_prefix: str = '') -> tuple:
    """Draw one node rectangle + caption + corner grips; returns
    (rect_fig, text_fig). The caller decides the border styling (selection /
    incomplete / modified / diff colors). The top-right ≡ grip is the *move*
    handle, the bottom-right ◢ hatch the *resize* handle (the body itself only
    selects, so clicks never accidentally drag)."""
    x, y = n['x'], n['y']
    w, h = _node_size(n)
    t = _node_ticket(n)
    fill = _NODE_COLORS.get(t.ticket_type, '#455a64')
    rect = graph.draw_rectangle((x, y), (x + w, y + h), fill_color=fill,
                                line_color=line_color, line_width=line_width)
    head = head_prefix + (n.get('jira_key') or t.ticket_type)
    chars = max(8, int(w / 7.5))
    txt = graph.draw_text(f"{head}\n{(t.summary or '(no title)')[:chars]}",
                          (x + w / 2, y + h / 2),
                          color='white', font=('Helvetica', 8))
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


def _planner_node_edit(ticket: Ticket) -> bool:
    """Edit a planner node's ticket fields (Save/Cancel — no Jira submit, that's
    deferred to Push). Mutates ``ticket`` in place; returns True if saved.
    Existing (already-in-Jira) nodes are editable too: their changes are queued
    and applied to the real ticket on Push (two-way editing)."""
    fields = ticket.all_form_fields()
    layout = [
        [sg.Text(f'Edit {ticket.ticket_type}', font=('Helvetica', 12, 'bold'))],
        [sg.HSep()],
        *[_build_field_row(f, ticket) for f in fields],
        [sg.HSep()],
        [sg.Push(),
         sg.Button('Save', key='-SAVE-', bind_return_key=False),
         sg.Button('Cancel', key='-CANCEL-')],
    ]
    window = sg.Window('Plan node', layout, finalize=True, modal=True,
                       keep_on_top=True, return_keyboard_events=False)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    saved = False
    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-CANCEL-'):
            break
        if event == '-SAVE-':
            ticket.apply_form_values(values)
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


def _link_options(jira: JiraClient, state: dict) -> list[tuple[str, dict]]:
    """Link options with the type list fetched once per canvas (memoized in state)."""
    if state.get('link_types') is None:
        status, types, _ = run_with_busy(jira.get_issue_link_types,
                                         message='Fetching link types…')
        state['link_types'] = types if status == 'ok' else []
    return _build_link_options(state['link_types'])


def _relationship_options(jira: JiraClient, state: dict) -> list[tuple[str, dict]]:
    """Everything a new (or re-typed) edge can be: hierarchy first — Child is the
    default — then every dependency-link phrase."""
    return ([('Child  (nests under this ticket)',
              {'category': 'hierarchy', 'dir': 'child'}),
             ('Parent  (this ticket nests under it)',
              {'category': 'hierarchy', 'dir': 'parent'})]
            + _link_options(jira, state))


def _apply_relationship(edges: list, source: dict, other: dict, payload: dict) -> None:
    """Wire ``other`` to ``source`` per a relationship payload from
    :func:`_relationship_options`. Hierarchy goes through ``_set_parent_edge`` (one
    parent per child); links append a directed source → other edge."""
    if payload.get('category') == 'hierarchy':
        if payload.get('dir') == 'parent':
            _set_parent_edge(edges, other, source)
        else:
            _set_parent_edge(edges, source, other)
    else:
        edges.append({'from': source['node_id'], 'to': other['node_id'],
                      'category': 'link', 'rel': payload.get('rel'),
                      'reverse': bool(payload.get('reverse'))})


def _spawn_dialog(jira: JiraClient, state: dict,
                  with_relationship: bool) -> tuple[str, str, dict | None] | None:
    """Title-first dialog for adding a node. With ``with_relationship`` (arrow
    click) it adds a Relationship dropdown (Child default). Returns
    ``(action, title, rel_payload)`` where action is 'create' or 'import', or
    None if cancelled. Import lets the title double as the search query."""
    rel_opts = _relationship_options(jira, state) if with_relationship else []
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
                         initial_query: str = '') -> dict | None:
    """Search Jira for an existing issue and return the chosen raw issue dict (or
    None). Type a key (e.g. PAY-12) or words; Search runs a live JQL query.
    ``initial_query`` pre-fills the search box (e.g. the title typed in the spawn
    dialog before choosing Import)."""
    proj = config.get('jira', {}).get('project_key', '')
    issues: list[dict] = []
    layout = [
        [sg.Text('Add an existing ticket', font=('Helvetica', 12, 'bold'))],
        [sg.Input(initial_query, key='-Q-', size=(36, 1)), sg.Button('Search', key='-S-', bind_return_key=True)],
        [sg.Listbox([], size=(52, 12), key='-RES-', font=('Consolas', 10),
                    select_mode=sg.LISTBOX_SELECT_MODE_BROWSE)],
        [sg.Text('', key='-INFO-', font=('Helvetica', 8))],
        [sg.Push(), sg.Button('Add', key='-ADD-'), sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Add existing ticket', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    bring_to_front(w)
    w['-Q-'].set_focus()

    def _labels():
        return [f"{i.get('key', '')}  {((i.get('fields') or {}).get('summary') or '')[:50]}"
                for i in issues]

    chosen = None
    while True:
        ev, vals = _read(w)
        if ev in (sg.WIN_CLOSED, '-C-'):
            break
        if ev == '-S-':
            q = (vals.get('-Q-') or '').strip()
            if not q:
                continue
            status, found, _ = run_with_busy(lambda: jira.search_issues(proj, q),
                                             message='Searching…')
            if status != 'ok':
                if status == 'error':
                    w['-INFO-'].update(f"Search failed: {found}")
                continue
            issues = found
            w['-RES-'].update(_labels())
            w['-INFO-'].update(f"{len(issues)} match(es)" if issues else 'No matches.')
            if issues:
                _soft_select(w, 0, key='-RES-')
        elif ev == '-ADD-' and vals.get('-RES-'):
            chosen = issues[_labels().index(vals['-RES-'][0])]
            break
    w.close()
    return chosen


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


def _existing_node(issue: dict, x: int, y: int) -> dict:
    """Build an 'existing' planner node from a raw Jira issue dict, carrying the
    real field values (where fetched) so edits diff against Jira's state."""
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
    n = _new_node(t, x, y)
    n['kind'] = 'existing'
    n['jira_key'] = issue.get('key')
    n['ticket'] = t.to_dict()
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


def _push_plan_to_jira(jira: JiraClient, config: dict, plan: dict) -> tuple[str, bool]:
    """Create every not-yet-pushed draft node as a real issue (parents before
    children), then apply the not-yet-pushed hierarchy updates and issue links.
    Returns ``(summary, clean)`` — clean means everything succeeded, so the
    caller can retire the local plan (Jira now holds the whole tree). Jira keys
    are written back into the nodes and successful edges get ``pushed: True``,
    so a retry after a partial failure only attempts what failed."""
    proj = config['jira']['project_key']
    nodes = {n['node_id']: n for n in plan['nodes']}

    # Block the whole push if any draft node is missing required fields — surface
    # exactly what's missing rather than creating a partial tree that fails midway.
    incomplete = []
    for n in plan['nodes']:
        if n.get('kind') == 'existing' or n.get('jira_key'):
            continue
        t = _node_ticket(n)
        ok, missing = t.is_valid()
        if not ok:
            head = n.get('jira_key') or t.ticket_type
            incomplete.append(f"{head} \"{(t.summary or '(no title)')[:30]}\": "
                              f"{', '.join(missing)}")
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
            ticket = _node_ticket(n)
            rel = parent_rel.get(nid)
            if parent_key and rel == 'subtask':
                ticket.parent = parent_key
            elif parent_key and rel == 'epic-child':
                ticket.epic_link = parent_key
            try:
                resp = jira.create_issue(ticket.to_jira_payload(proj))
                n['jira_key'] = resp.get('key')
                n['ticket'] = ticket.to_dict()
                created += 1
                created_keys.append(n['jira_key'] or nid)
                if parent_key:
                    # The hierarchy edge was satisfied at create time.
                    parent_edge[nid]['pushed'] = True
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

    msg = (f"Created {created} issue(s), updated {updated} existing, "
           f"{linked} link(s), removed {removed} relationship(s).")
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


def _plan_from_jira(jira: JiraClient, config: dict, root_key: str) -> dict:
    """Hydrate an in-memory plan from Jira: the issue, its children, and the
    dependency links among them, all as read-only existing nodes with edges
    already marked ``pushed``. Never saved to disk by itself — only local work
    added on top makes it worth persisting (see :func:`_worth_saving`)."""
    epic_cf = _epic_link_cf(config)
    root = jira.get_issue(root_key)
    rkey = root.get('key', root_key)
    children = jira.get_children(rkey, epic_cf)

    plan = new_plan(rkey, epic_key=rkey)
    summ = ((root.get('fields') or {}).get('summary') or '')[:24]
    plan['name'] = f"{rkey} {summ}".strip()

    issues = [root] + children
    by_key: dict[str, dict] = {}
    for idx, issue in enumerate(issues):
        if idx == 0:
            x, y = 60, 40  # root anchors the top; children grid below it
        else:
            x = 60 + ((idx - 1) % 5) * 172
            y = 170 + ((idx - 1) // 5) * 96
        node = _existing_node(issue, x, y)
        plan['nodes'].append(node)
        by_key[issue.get('key')] = node

    root_node = by_key[rkey]
    for child in children:
        plan['edges'].append({'from': root_node['node_id'],
                              'to': by_key[child['key']]['node_id'],
                              **_hierarchy_rel(root_node), 'pushed': True})

    # Dependency links among the fetched issues. Each link shows up on both of
    # its endpoints, so dedupe by link id. Direction mirrors create_issue_link's
    # verified mapping: reverse=False ⇒ the edge runs inwardIssue → outwardIssue,
    # and an issue's own entry names only the *other* endpoint.
    seen: set = set()
    for issue in issues:
        ikey = issue.get('key')
        for ln in ((issue.get('fields') or {}).get('issuelinks') or []):
            rel = (ln.get('type') or {}).get('name') or 'Relates'
            out_key = (ln.get('outwardIssue') or {}).get('key')
            in_key = (ln.get('inwardIssue') or {}).get('key')
            if out_key:                      # this issue → outward issue
                src, dst = ikey, out_key
            elif in_key:                     # inward issue → this issue
                src, dst = in_key, ikey
            else:
                continue
            lid = ln.get('id') or (rel, src, dst)
            if lid in seen or src not in by_key or dst not in by_key:
                continue
            seen.add(lid)
            plan['edges'].append({'from': by_key[src]['node_id'],
                                  'to': by_key[dst]['node_id'],
                                  'category': 'link', 'rel': rel,
                                  'reverse': False, 'pushed': True,
                                  'link_id': ln.get('id')})
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
        line = f"Relationship: {_name(a)} —{_edge_label(e)}→ {_name(b)}"
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


def _confirm_push_with_preview(plan: dict, by_id: dict) -> bool:
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

    for e in plan.get('edges', []):
        a, b = by_id.get(e['from']), by_id.get(e['to'])
        if not a or not b:
            continue
        new = not e.get('pushed')
        _draw_directed_edge(graph, a, b, _edge_label(e),
                            color='#2e7d32' if new else '#9e9e9e',
                            label_color='#2e7d32' if new else '#1565c0')
    key_to_node = {n.get('jira_key'): n for n in plan.get('nodes', []) if n.get('jira_key')}
    for u in plan.get('pending_unlinks') or []:
        a, b = key_to_node.get(u.get('from_key')), key_to_node.get(u.get('to_key'))
        rel = ('child' if u.get('hier') else u.get('rel')) or 'link'
        if a and b:
            _draw_directed_edge(graph, a, b, f'✕ {rel}',
                                color='#e53935', label_color='#e53935')
    for n in plan.get('nodes', []):
        new = n.get('kind') != 'existing' and not n.get('jira_key')
        if new:
            _draw_node(graph, n, line_color='#2e7d32', line_width=3, head_prefix='+ ')
        elif n.get('orig_ticket'):
            _draw_node(graph, n, line_color='#fb8c00', line_width=3, head_prefix='✎ ')
        else:
            _draw_node(graph, n)
    w['-DIFFS-'].update(_push_change_summary(plan, by_id) or '(no changes)')

    ev, _ = _read(w)
    w.close()
    return ev == '-YES-'


def show_plan_canvas(cache: Cache, jira: JiraClient, config: dict, plan: dict) -> None:
    """Draggable relationship graph for one initiative plan."""
    nodes = plan.setdefault('nodes', [])
    edges = plan.setdefault('edges', [])
    by_id = {n['node_id']: n for n in nodes}
    state = {'selected': None, 'selected_edge': None,
             'drag_node': None, 'drag_off': (0, 0), 'moved': False,
             'resize_node': None, 'body_press': False,
             'link_types': None, 'arrows': {}, 'hover': None, 'arrow_armed': None}

    graph = sg.Graph((_CANVAS_W, _CANVAS_H), (0, _CANVAS_H), (_CANVAS_W, 0),
                     key='-CANVAS-', enable_events=True, drag_submits=True,
                     background_color='#fafafa')
    layout = [
        [sg.Text(plan.get('name', 'Plan'), font=('Helvetica', 13, 'bold')),
         sg.Push(), sg.Text('', key='-PSTATUS-', font=('Helvetica', 8))],
        [sg.Button('Add Ticket', key='-ADD-'),
         sg.Button('Edit', key='-EDIT-', disabled=True),
         sg.Button('Delete', key='-DEL-', disabled=True),
         sg.Push(),
         sg.Button('Push to Jira', key='-PUSH-'),
         sg.Button('Save', key='-PSAVE-'),
         sg.Button('Close', key='-PCLOSE-')],
        [graph],
        [sg.Text('Hover a node and click a side arrow to add a connected ticket · '
                 'drag the ≡ grip (top-right) to move, the ◢ grip (bottom-right) to '
                 'resize · click a node or a line to select it (Edit / Delete).',
                 font=('Helvetica', 8))],
    ]
    window = sg.Window(f"Plan — {plan.get('name', '')}", layout, finalize=True,
                       return_keyboard_events=False)
    bring_to_front(window)

    figmap: dict = {}

    def _status(msg: str = '') -> None:
        window['-PSTATUS-'].update(
            msg or f"{len(nodes)} node(s) · {len(edges)} edge(s)")

    # ── Hover arrows (the spawn affordance) ──────────────────────────────────
    # Drawn straight on the tk canvas from the <Motion> callback; graph coords
    # equal widget pixels for this Graph (origin top-left, y down).

    _GAP, _ARROW_L, _ARROW_HALF, _HALO = 8, 20, 9, 34

    def _clear_arrows() -> None:
        for fid in list(state['arrows']):
            try:
                graph.delete_figure(fid)
            except Exception:
                pass
        state['arrows'].clear()
        state['hover'] = None

    def _draw_arrows(n: dict) -> None:
        _clear_arrows()
        state['hover'] = n['node_id']
        x, y = n['x'], n['y']
        w, h = _node_size(n)
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
            try:
                # stipple ≈ translucency (tk canvas has no real alpha)
                fid = graph.Widget.create_polygon(
                    *[c for p in pts for c in p],
                    fill='#90a4ae', stipple='gray50', outline='')
            except Exception:
                return
            state['arrows'][fid] = (n['node_id'], side)

    def _node_at(px, py) -> dict | None:
        for n in nodes:
            w, h = _node_size(n)
            if n['x'] <= px <= n['x'] + w and n['y'] <= py <= n['y'] + h:
                return n
        return None

    def _handle_at(px, py) -> tuple | None:
        """('move'|'resize', node) when the point is inside a corner grip:
        top-right ≡ moves the node, bottom-right ◢ resizes it."""
        for n in nodes:
            w, h = _node_size(n)
            x, y = n['x'], n['y']
            if x + w - _HANDLE <= px <= x + w:
                if y <= py <= y + _HANDLE:
                    return 'move', n
                if y + h - _HANDLE <= py <= y + h:
                    return 'resize', n
        return None

    def _on_motion(ev) -> None:
        if state['drag_node'] is not None or state['resize_node'] is not None:
            return
        n = _node_at(ev.x, ev.y)
        # Cursor hints over the grips.
        handle = _handle_at(ev.x, ev.y)
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
                # Keep the arrows alive while crossing the gap toward them.
                if (hov['x'] - _HALO <= ev.x <= hov['x'] + w + _HALO
                        and hov['y'] - _HALO <= ev.y <= hov['y'] + h + _HALO):
                    return
            _clear_arrows()

    # add='+' is load-bearing: PySimpleGUI delivers drag_submits events through
    # its own <Motion> binding on this canvas — a plain bind() would replace it
    # and silently kill node dragging.
    graph.Widget.bind('<Motion>', _on_motion, add='+')
    graph.Widget.bind('<Leave>', lambda e: _clear_arrows(), add='+')

    # ── Rendering & selection ────────────────────────────────────────────────

    def redraw() -> None:
        graph.erase()
        state['arrows'].clear()
        state['hover'] = None
        for i, e in enumerate(edges):
            a, b = by_id.get(e['from']), by_id.get(e['to'])
            if not a or not b:
                continue
            sel = i == state['selected_edge']
            _draw_directed_edge(graph, a, b, _edge_label(e),
                                color='#fbc02d' if sel else '#9e9e9e',
                                label_color='#f57f17' if sel else '#1565c0')
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
            rect, txt = _draw_node(graph, n, lc, lw, prefix)
            figmap[n['node_id']] = {'rect': rect, 'text': txt}

    def fig_to_node(figs) -> str | None:
        rev = {}
        for nid, f in figmap.items():
            rev[f['rect']] = nid
            rev[f['text']] = nid
        for f in (figs or []):
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
        node = _existing_node(issue, x, y)
        nodes.append(node)
        by_id[node['node_id']] = node
        return node

    # ── Spawn & edge-edit flows ──────────────────────────────────────────────

    def _spawn_from(source: dict | None, side: str | None) -> None:
        """Add a node via the title-first dialog — from a hover arrow (with a
        relationship to ``source``) or from Add Ticket (independent)."""
        res = _spawn_dialog(jira, state, with_relationship=source is not None)
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
            if not _planner_node_edit(t):
                return
            node = add_node(t, x, y)
        else:  # import an existing Jira ticket
            issue = _pick_existing_issue(jira, config, initial_query=title)
            if not issue:
                return
            node = add_existing(issue, x, y)
        if source is not None and rel:
            _apply_relationship(edges, source, node, rel)
        set_selected(nid=node['node_id'])
        _status()

    def _edit_edge(idx: int) -> None:
        """Re-type the selected relationship; a pushed one is queued for removal
        in Jira and replaced by the new (unpushed) edge."""
        e = edges[idx]
        opts = _relationship_options(jira, state)
        labels = [o[0] for o in opts]
        cur = labels[0]
        if e.get('category') == 'link':
            for lbl, payload in opts:
                if (payload.get('category') == 'link'
                        and payload.get('rel') == e.get('rel')
                        and bool(payload.get('reverse')) == bool(e.get('reverse'))):
                    cur = lbl
                    break
        lay = [[sg.Text('Change relationship  (source → target)',
                        font=('Helvetica', 11, 'bold'))],
               [sg.Combo(labels, default_value=cur, key='-R-', readonly=True,
                         size=(44, 1))],
               [sg.Push(), sg.Button('Apply', key='-OK-'), sg.Button('Cancel', key='-C-')]]
        w = sg.Window('Edit relationship', lay, finalize=True, modal=True,
                      keep_on_top=True)
        w.bind('<Escape>', '-C-')
        w.bind('<Return>', '-OK-')
        bring_to_front(w)
        ev, vals = _read(w)
        w.close()
        if ev != '-OK-' or vals.get('-R-') not in labels:
            return
        payload = dict(opts[labels.index(vals['-R-'])][1])
        a, b = by_id.get(e['from']), by_id.get(e['to'])
        if not a or not b:
            return
        if e.get('pushed'):
            _queue_unlink(plan, e, by_id)
        edges.pop(idx)
        _apply_relationship(edges, a, b, payload)
        set_selected()
        _status()

    redraw()
    _status()

    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-PCLOSE-'):
            if _worth_saving(plan):
                cache.save_plan(plan)
            break

        if event == '-CANVAS-':
            pt = values['-CANVAS-']
            if pt == (None, None) or state['arrow_armed'] is not None:
                continue
            if state['drag_node'] is not None:
                n = by_id[state['drag_node']]
                n['x'] = int(pt[0] - state['drag_off'][0])
                n['y'] = int(pt[1] - state['drag_off'][1])
                state['moved'] = True
                redraw()
            elif state['resize_node'] is not None:
                n = by_id[state['resize_node']]
                n['w'] = max(_MIN_W, int(pt[0] - n['x']))
                n['h'] = max(_MIN_H, int(pt[1] - n['y']))
                state['moved'] = True
                redraw()
            elif not state['body_press']:  # first press of this gesture
                figs = graph.get_figures_at_location(pt)
                arrow = next((state['arrows'][f] for f in figs
                              if f in state['arrows']), None)
                if arrow is not None:
                    # Press landed on a hover arrow — spawn on release, no drag.
                    state['arrow_armed'] = arrow
                    continue
                handle = _handle_at(*pt)
                if handle is not None:
                    kind, n = handle
                    if kind == 'move':
                        state.update(drag_node=n['node_id'], moved=False,
                                     drag_off=(pt[0] - n['x'], pt[1] - n['y']))
                    else:
                        state.update(resize_node=n['node_id'], moved=False)
                else:
                    # Body press: selection only, resolved on release. Sticky so
                    # dragging across a grip mid-gesture doesn't start a move.
                    state['body_press'] = True
            continue

        if event == '-CANVAS-+UP':
            pt = values.get('-CANVAS-')
            armed, state['arrow_armed'] = state['arrow_armed'], None
            dragged = ((state['drag_node'] is not None
                        or state['resize_node'] is not None) and state['moved'])
            grabbed = state['drag_node'] or state['resize_node']
            state.update(drag_node=None, resize_node=None,
                         moved=False, body_press=False)
            if armed is not None:
                src = by_id.get(armed[0])
                if src is not None:
                    _spawn_from(src, armed[1])
                continue
            if dragged:
                set_selected(nid=grabbed)
            else:
                valid = pt and pt != (None, None)
                clicked = fig_to_node(graph.get_figures_at_location(pt)) if valid else None
                if clicked is not None:
                    set_selected(nid=clicked)
                else:
                    eidx = _edge_at_point(pt, by_id, edges) if valid else None
                    set_selected(edge_idx=eidx)
            continue

        if event == '-ADD-':
            _spawn_from(None, None)

        elif event == '-EDIT-':
            if state['selected_edge'] is not None and state['selected_edge'] < len(edges):
                _edit_edge(state['selected_edge'])
            elif state['selected']:
                n = by_id.get(state['selected'])
                if n:
                    t = _node_ticket(n)
                    if _planner_node_edit(t):
                        # First edit of a pushed ticket snapshots the Jira-side
                        # state so Push can send exactly the changed fields.
                        if n.get('kind') == 'existing' and not n.get('orig_ticket'):
                            n['orig_ticket'] = dict(n['ticket'])
                        n['ticket'] = t.to_dict()
                        if (n.get('orig_ticket')
                                and not _node_field_diff(n['orig_ticket'], n['ticket'])):
                            n.pop('orig_ticket', None)  # edited back — nothing queued
                        redraw()

        elif event == '-DEL-':
            if state['selected_edge'] is not None and state['selected_edge'] < len(edges):
                e = edges[state['selected_edge']]
                warn = ('Remove this relationship?'
                        + ('\n\nIt already exists in Jira — it will be removed '
                           'there on the next Push.' if e.get('pushed') else ''))
                if sg.popup_yes_no(warn, modal=True, keep_on_top=True) == 'Yes':
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
                if sg.popup_yes_no(msg, modal=True, keep_on_top=True) == 'Yes':
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

        elif event == '-PUSH-':
            if not nodes and not plan.get('pending_unlinks'):
                sg.popup('Nothing to push yet.', modal=True, keep_on_top=True)
                continue
            # Touching content already in Jira warrants the visual diff review;
            # a pure-new plan keeps the simple confirm.
            if _touches_existing(plan, by_id):
                go = _confirm_push_with_preview(plan, by_id)
            else:
                go = sg.popup_yes_no('Create the draft tickets and links in Jira now?',
                                     title='Push to Jira', modal=True,
                                     keep_on_top=True) == 'Yes'
            if go:
                cache.save_plan(plan)  # state on disk before any network work
                status, result, tb = run_with_busy(
                    lambda: _push_plan_to_jira(jira, config, plan),
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


def show_plan_picker(cache: Cache, jira: JiraClient, config: dict) -> None:
    """List initiative plans; open / create / delete. Loops until closed."""
    while True:
        plans = cache.list_plans()
        labels = [f"{p.get('name', '(unnamed)'):30s}  {len(p.get('nodes', []))} node(s)"
                  f"   {(p.get('created_at') or '')[:10]}" for p in plans]
        layout = [
            [sg.Text('Initiative Plans', font=('Helvetica', 13, 'bold'))],
            [sg.Listbox(labels, size=(60, min(len(labels) + 1, 12)), key='-PL-',
                        font=('Consolas', 10), enable_events=False,
                        select_mode=sg.LISTBOX_SELECT_MODE_BROWSE)],
            [sg.Push(),
             sg.Button('Open', key='-OPEN-'),
             sg.Button('Open from Jira', key='-OPENJ-',
                       tooltip='Load an existing ticket and graph its children '
                               'and dependency links'),
             sg.Button('New plan', key='-NEWP-'),
             sg.Button('Delete', key='-DELP-'),
             sg.Button('Close', key='-CLOSE-')],
        ]
        window = sg.Window('Plans', layout, finalize=True, modal=True, keep_on_top=True)
        window.bind('<Escape>', '-CLOSE-')
        window.bind('<Return>', '-OPEN-')
        bring_to_front(window)
        if plans:
            _soft_select(window, 0, key='-PL-')
        ev, vals = _read(window)
        window.close()

        if ev in (sg.WIN_CLOSED, '-CLOSE-'):
            return
        if ev == '-OPENJ-':
            issue = _pick_existing_issue(jira, config)
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
                    show_plan_canvas(cache, jira, config, plan)
            elif sg.popup_yes_no(f"Delete plan '{target.get('name')}'?",
                                 modal=True, keep_on_top=True) == 'Yes':
                cache.delete_plan(target['plan_id'])
