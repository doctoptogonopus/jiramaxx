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
import traceback as _tb
import PySimpleGUI as sg
from .models import Ticket, TICKET_CLASSES, ticket_from_dict
from .cache import Cache, new_plan
from .api import JiraClient
from .utils import safe_read as _read, show_error, bring_to_front
from .ui import _build_field_row, _fkey, _soft_select, show_type_selector, _epic_link_cf

_NODE_W, _NODE_H = 150, 50
_CANVAS_W, _CANVAS_H = 920, 560
_NODE_COLORS = {'Story': '#1565c0', 'Bug': '#c62828', 'Task': '#2e7d32',
                'Epic': '#6a1b9a', 'Initiative': '#00838f'}


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


def _draw_directed_edge(graph, a: dict, b: dict, label: str) -> None:
    """Draw an edge a → b as a line into b's border with an arrowhead, plus a label.
    Stopping at the rectangle border keeps the head visible instead of buried under
    the node. Uses only ``draw_line`` so it works across PySimpleGUI versions."""
    ca = (a['x'] + _NODE_W / 2, a['y'] + _NODE_H / 2)
    cb = (b['x'] + _NODE_W / 2, b['y'] + _NODE_H / 2)
    dx, dy = ca[0] - cb[0], ca[1] - cb[1]
    if dx == 0 and dy == 0:
        return
    sx = (_NODE_W / 2) / abs(dx) if dx else float('inf')
    sy = (_NODE_H / 2) / abs(dy) if dy else float('inf')
    s = min(sx, sy)
    tip = (cb[0] + dx * s, cb[1] + dy * s)
    graph.draw_line(ca, tip, color='#9e9e9e', width=2)
    # Two wings ~14px at ±25° off the back-vector (tip → a).
    ang = math.atan2(dy, dx)
    L, w = 14, math.radians(25)
    for sign in (+1, -1):
        graph.draw_line(tip, (tip[0] + L * math.cos(ang + sign * w),
                              tip[1] + L * math.sin(ang + sign * w)),
                        color='#9e9e9e', width=2)
    graph.draw_text(label, ((ca[0] + tip[0]) / 2, (ca[1] + tip[1]) / 2),
                    color='#1565c0', font=('Helvetica', 7))


def _planner_node_edit(ticket: Ticket, read_only: bool = False) -> bool:
    """Edit a planner node's ticket fields (Save/Cancel — no Jira submit, that's
    deferred to Push). Mutates ``ticket`` in place; returns True if saved."""
    heading = f"{'View' if read_only else 'Edit'} {ticket.ticket_type}"
    fields = ticket.all_form_fields()
    layout = [
        [sg.Text(heading, font=('Helvetica', 12, 'bold'))],
        [sg.HSep()],
        *[_build_field_row(f, ticket) for f in fields],
        [sg.HSep()],
        [sg.Push(),
         *([] if read_only else [sg.Button('Save', key='-SAVE-', bind_return_key=False)]),
         sg.Button('Close' if read_only else 'Cancel', key='-CANCEL-')],
    ]
    window = sg.Window('Plan node', layout, finalize=True, modal=True,
                       keep_on_top=True, return_keyboard_events=False)
    window.bind('<Escape>', '-CANCEL-')
    bring_to_front(window)
    if read_only:
        for f in fields:
            try:
                window[_fkey(f)].update(disabled=True)
            except Exception:
                pass
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


def _pick_link(jira: JiraClient, state: dict) -> dict | None:
    """Pick a *dependency link* type for a new edge (source → target). Returns
    {'category':'link','rel':name,'reverse':bool} or None if cancelled. Hierarchy is
    NOT chosen here — it's inherent (Add Child / Set Parent). Link types are fetched
    once. No type gating; Push reports any per-edge rejections from Jira."""
    if state.get('link_types') is None:
        try:
            state['link_types'] = jira.get_issue_link_types()
        except Exception:
            state['link_types'] = []
    opts: list[tuple[str, dict]] = []
    for lt in state['link_types']:
        name = lt.get('name', '')
        outw = lt.get('outward', name) or name
        inw = lt.get('inward', name) or name
        opts.append((f"{outw}  ({name})", {'category': 'link', 'rel': name, 'reverse': False}))
        opts.append((f"{inw}  ({name})", {'category': 'link', 'rel': name, 'reverse': True}))
    if not opts:
        sg.popup('No issue-link types available from Jira.', modal=True, keep_on_top=True)
        return None
    labels = [o[0] for o in opts]
    layout = [
        [sg.Text('Dependency link  (source → target)', font=('Helvetica', 11, 'bold'))],
        [sg.Combo(labels, default_value=labels[0], key='-R-', readonly=True, size=(44, 1))],
        [sg.Push(), sg.Button('OK', key='-OK-'), sg.Button('Cancel', key='-C-')],
    ]
    w = sg.Window('Dependency link', layout, finalize=True, modal=True, keep_on_top=True)
    w.bind('<Escape>', '-C-')
    w.bind('<Return>', '-OK-')
    bring_to_front(w)
    ev, vals = _read(w)
    w.close()
    if ev == '-OK-' and vals.get('-R-'):
        return dict(opts[labels.index(vals['-R-'])][1])
    return None


def _pick_existing_issue(jira: JiraClient, config: dict) -> dict | None:
    """Search Jira for an existing issue and return the chosen raw issue dict (or
    None). Type a key (e.g. PAY-12) or words; Search runs a live JQL query."""
    proj = config.get('jira', {}).get('project_key', '')
    issues: list[dict] = []
    layout = [
        [sg.Text('Add an existing ticket', font=('Helvetica', 12, 'bold'))],
        [sg.Input('', key='-Q-', size=(36, 1)), sg.Button('Search', key='-S-', bind_return_key=True)],
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
            try:
                issues = jira.search_issues(proj, q)
            except Exception as exc:
                w['-INFO-'].update(f"Search failed: {exc}")
                continue
            w['-RES-'].update(_labels())
            w['-INFO-'].update(f"{len(issues)} match(es)" if issues else 'No matches.')
            if issues:
                _soft_select(w, 0, key='-RES-')
        elif ev == '-ADD-' and vals.get('-RES-'):
            chosen = issues[_labels().index(vals['-RES-'][0])]
            break
    w.close()
    return chosen


def _existing_node(issue: dict, x: int, y: int) -> dict:
    """Build a read-only 'existing' planner node from a raw Jira issue dict."""
    f = issue.get('fields', {}) or {}
    itype = ((f.get('issuetype') or {}).get('name')) or 'Task'
    cls = TICKET_CLASSES.get(itype, TICKET_CLASSES['Task'])
    t = cls()
    t.summary = f.get('summary', '') or ''
    n = _new_node(t, x, y)
    n['kind'] = 'existing'
    n['jira_key'] = issue.get('key')
    n['ticket'] = t.to_dict()
    return n


def _push_plan_to_jira(jira: JiraClient, config: dict, plan: dict) -> str:
    """Create every not-yet-pushed draft node as a real issue (parents before
    children), then create the issue links. Returns a summary string; writes the
    Jira keys back into the plan nodes."""
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
                + "\n  ".join(incomplete))

    parent_of, parent_rel = {}, {}
    for e in plan['edges']:
        if e.get('category') == 'hierarchy':
            parent_of[e['to']] = e['from']
            parent_rel[e['to']] = e.get('rel')

    created, failures = 0, []
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
        if e.get('category') != 'hierarchy':
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
            updated += 1
        except Exception as exc:
            failures.append(f"update {child['jira_key']}: {exc}")

    linked = 0
    for e in plan['edges']:
        if e.get('category') != 'link':
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
            linked += 1
        except Exception as exc:
            failures.append(f"link {e.get('rel')}: {exc}")

    msg = (f"Created {created} issue(s), updated {updated} existing, "
           f"and {linked} link(s).")
    if failures:
        msg += f"\n\n{len(failures)} problem(s):\n  " + '\n  '.join(failures[:20])
    return msg


def show_plan_canvas(cache: Cache, jira: JiraClient, config: dict, plan: dict) -> None:
    """Draggable relationship graph for one initiative plan."""
    nodes = plan.setdefault('nodes', [])
    edges = plan.setdefault('edges', [])
    by_id = {n['node_id']: n for n in nodes}
    state = {'selected': None, 'link_mode': False, 'link_src': None,
             'drag_node': None, 'drag_off': (0, 0), 'moved': False, 'link_types': None,
             'parent_mode': False, 'parent_child': None}

    graph = sg.Graph((_CANVAS_W, _CANVAS_H), (0, _CANVAS_H), (_CANVAS_W, 0),
                     key='-CANVAS-', enable_events=True, drag_submits=True,
                     background_color='#fafafa')
    layout = [
        [sg.Text(plan.get('name', 'Plan'), font=('Helvetica', 13, 'bold')),
         sg.Push(), sg.Text('', key='-PSTATUS-', font=('Helvetica', 8))],
        [sg.Button('Add Ticket', key='-ADD-'),
         sg.Button('Add Existing', key='-ADDEX-'),
         sg.Button('Add Child', key='-CHILD-', disabled=True),
         sg.Button('Set Parent', key='-SETPARENT-', disabled=True),
         sg.Button('Edit', key='-EDIT-', disabled=True),
         sg.Button('Delete', key='-DEL-', disabled=True),
         sg.Button('Link mode', key='-LINK-'),
         sg.Push(),
         sg.Button('Push to Jira', key='-PUSH-'),
         sg.Button('Save', key='-PSAVE-'),
         sg.Button('Close', key='-PCLOSE-')],
        [graph],
        [sg.Text('Drag to move · click to select. Add Child nests automatically · '
                 'Set Parent re-nests an existing node · Link mode adds dependency links.',
                 font=('Helvetica', 8))],
    ]
    window = sg.Window(f"Plan — {plan.get('name', '')}", layout, finalize=True,
                       return_keyboard_events=False)
    bring_to_front(window)

    figmap: dict = {}

    def _status(msg: str = '') -> None:
        window['-PSTATUS-'].update(
            msg or f"{len(nodes)} node(s) · {len(edges)} edge(s)")

    def redraw() -> None:
        graph.erase()
        for e in edges:
            a, b = by_id.get(e['from']), by_id.get(e['to'])
            if not a or not b:
                continue
            _draw_directed_edge(graph, a, b, _edge_label(e))
        figmap.clear()
        for n in nodes:
            x, y = n['x'], n['y']
            t = _node_ticket(n)
            sel = n['node_id'] == state['selected']
            existing = n.get('kind') == 'existing'
            # Flag draft nodes that aren't ready to push (missing required fields).
            incomplete = (not existing) and not t.is_valid()[0]
            fill = _NODE_COLORS.get(t.ticket_type, '#455a64')
            if sel:
                line_color, line_width = '#ffeb3b', 3
            elif incomplete:
                line_color, line_width = '#e53935', 3
            else:
                line_color, line_width = 'black', 1
            rect = graph.draw_rectangle((x, y), (x + _NODE_W, y + _NODE_H),
                                        fill_color=fill,
                                        line_color=line_color, line_width=line_width)
            head = n.get('jira_key') or t.ticket_type
            if incomplete:
                head = '⚠ ' + head
            txt = graph.draw_text(f"{head}\n{(t.summary or '(no title)')[:20]}",
                                  (x + _NODE_W / 2, y + _NODE_H / 2),
                                  color='white', font=('Helvetica', 8))
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

    def set_selected(nid) -> None:
        state['selected'] = nid
        for k in ('-CHILD-', '-SETPARENT-', '-EDIT-', '-DEL-'):
            window[k].update(disabled=nid is None)
        redraw()

    def add_node(ticket: Ticket, x=None, y=None) -> dict:
        if x is None:
            x = 60 + (len(nodes) % 5) * 172
            y = 60 + (len(nodes) // 5) * 96
        node = _new_node(ticket, x, y)
        nodes.append(node)
        by_id[node['node_id']] = node
        return node

    redraw()
    _status()

    while True:
        event, values = _read(window)
        if event in (sg.WIN_CLOSED, '-PCLOSE-'):
            cache.save_plan(plan)
            break

        if event == '-CANVAS-':
            pt = values['-CANVAS-']
            if pt == (None, None) or state['link_mode'] or state['parent_mode']:
                continue
            if state['drag_node'] is None:
                nid = fig_to_node(graph.get_figures_at_location(pt))
                if nid is not None:
                    n = by_id[nid]
                    state.update(drag_node=nid, moved=False,
                                 drag_off=(pt[0] - n['x'], pt[1] - n['y']))
            else:
                n = by_id[state['drag_node']]
                n['x'] = int(pt[0] - state['drag_off'][0])
                n['y'] = int(pt[1] - state['drag_off'][1])
                state['moved'] = True
                redraw()
            continue

        if event == '-CANVAS-+UP':
            pt = values.get('-CANVAS-')
            clicked = fig_to_node(graph.get_figures_at_location(pt)) if pt and pt != (None, None) else None
            if state['drag_node'] is not None and state['moved']:
                set_selected(state['drag_node'])
            elif state['parent_mode']:
                # One-shot: the selected node is the child; the clicked node is its parent.
                if clicked and clicked != state['parent_child']:
                    _set_parent_edge(edges, by_id[clicked], by_id[state['parent_child']])
                    _status('Parent set')
                else:
                    _status('')
                state['parent_mode'] = False
                state['parent_child'] = None
                redraw()
            elif state['link_mode']:
                if clicked is None:
                    state['link_src'] = None
                    _status('Link mode: click a source node')
                elif state['link_src'] is None:
                    state['link_src'] = clicked
                    _status('Link mode: now click the target node')
                elif clicked != state['link_src']:
                    rel = _pick_link(jira, state)
                    if rel:
                        edges.append({'from': state['link_src'], 'to': clicked, **rel})
                    state['link_src'] = None
                    redraw()
                    _status('Link added — click a source node for the next')
                else:
                    state['link_src'] = None
                    _status('Link mode: click a source node')
            else:
                set_selected(clicked)
            state['drag_node'] = None
            state['moved'] = False
            continue

        if event == '-LINK-':
            state['link_mode'] = not state['link_mode']
            state['link_src'] = None
            state['parent_mode'] = False  # the two modes are mutually exclusive
            state['parent_child'] = None
            window['-LINK-'].update(('● ' if state['link_mode'] else '') + 'Link mode')
            _status('Link mode: click a source node' if state['link_mode'] else '')
            continue

        if event == '-SETPARENT-' and state['selected']:
            state['parent_mode'] = True
            state['parent_child'] = state['selected']
            state['link_mode'] = False
            window['-LINK-'].update('Link mode')
            _status('Set parent: click the parent node')
            continue

        if event == '-ADDEX-':
            issue = _pick_existing_issue(jira, config)
            if issue:
                node = _existing_node(issue, 60 + (len(nodes) % 5) * 172,
                                      60 + (len(nodes) // 5) * 96)
                nodes.append(node)
                by_id[node['node_id']] = node
                redraw()

        if event == '-ADD-':
            ttype = show_type_selector()
            if ttype:
                t = TICKET_CLASSES[ttype]()
                if _planner_node_edit(t):
                    add_node(t)
                    redraw()

        elif event == '-CHILD-' and state['selected']:
            parent = by_id.get(state['selected'])
            if parent:
                # Hierarchy is inherent: the child is auto-nested under the parent
                # (type inferred). The child can be a new draft or an existing ticket.
                choice = sg.popup('Add child as a new draft or an existing ticket?',
                                  title='Add Child', custom_text=('New', 'Existing'),
                                  modal=True, keep_on_top=True)
                if choice == 'New':
                    ttype = show_type_selector()
                    if ttype:
                        t = TICKET_CLASSES[ttype]()
                        if _planner_node_edit(t):
                            child = add_node(t, x=parent['x'] + 30, y=parent['y'] + 110)
                            _set_parent_edge(edges, parent, child)
                            redraw()
                elif choice == 'Existing':
                    issue = _pick_existing_issue(jira, config)
                    if issue:
                        child = _existing_node(issue, parent['x'] + 30, parent['y'] + 110)
                        nodes.append(child)
                        by_id[child['node_id']] = child
                        _set_parent_edge(edges, parent, child)
                        redraw()

        elif event == '-EDIT-' and state['selected']:
            n = by_id.get(state['selected'])
            if n:
                t = _node_ticket(n)
                if _planner_node_edit(t, read_only=(n.get('kind') == 'existing')):
                    n['ticket'] = t.to_dict()
                    redraw()

        elif event == '-DEL-' and state['selected']:
            nid = state['selected']
            if sg.popup_yes_no('Delete this node and its connections?',
                               modal=True, keep_on_top=True) == 'Yes':
                nodes[:] = [n for n in nodes if n['node_id'] != nid]
                edges[:] = [e for e in edges if e['from'] != nid and e['to'] != nid]
                by_id.pop(nid, None)
                set_selected(None)

        elif event == '-PSAVE-':
            cache.save_plan(plan)
            sg.popup_quick_message('Plan saved.', auto_close_duration=1,
                                   background_color='#2e7d32', text_color='white')

        elif event == '-PUSH-':
            if not nodes:
                sg.popup('Nothing to push yet.', modal=True, keep_on_top=True)
            elif sg.popup_yes_no('Create the draft tickets and links in Jira now?',
                                 title='Push to Jira', modal=True, keep_on_top=True) == 'Yes':
                cache.save_plan(plan)
                try:
                    summary = _push_plan_to_jira(jira, config, plan)
                except Exception as exc:
                    show_error(f"Push failed:\n{exc}", tb=_tb.format_exc())
                    summary = None
                cache.save_plan(plan)
                redraw()
                if summary:
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
        if ev == '-NEWP-':
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
