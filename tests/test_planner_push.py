from jiramaxx.cache import new_plan
from jiramaxx.models import Epic, Story, Task
from jiramaxx.planner import (_adf_to_text, _apply_relationship,
                              _build_link_options, _changed_jira_fields,
                              _edge_at_point, _edge_segment, _incomplete_nodes,
                              _new_node, _node_field_diff, _node_size,
                              _plan_from_jira, _push_change_summary,
                              _push_plan_to_jira, _queue_unlink,
                              _set_parent_edge, _touches_existing, _view_node,
                              _worth_saving)

CONFIG = {'jira': {'project_key': 'T',
                   'custom_fields': {'epic_link': 'customfield_10014'}}}


class FakeJira:
    """Records create/update/link/unlink calls; can fail creates by summary."""

    def __init__(self, fail_summaries=()):
        self.created, self.updated, self.links = [], [], []
        self.deleted_links, self.issue_lookups = [], []
        self.issuelinks_by_key: dict = {}
        self.fail_summaries = set(fail_summaries)
        self.fail_unlinks = False
        self._n = 0

    def create_issue(self, payload):
        summary = payload['fields']['summary']
        if summary in self.fail_summaries:
            raise RuntimeError(f'boom: {summary}')
        self._n += 1
        key = f'T-{self._n}'
        self.created.append((key, payload))
        return {'key': key}

    def update_issue(self, key, fields):
        self.updated.append((key, fields))

    def create_issue_link(self, inward, outward, rel):
        self.links.append((inward, outward, rel))

    def delete_issue_link(self, link_id):
        if self.fail_unlinks:
            raise RuntimeError('unlink boom')
        self.deleted_links.append(link_id)

    def get_issue(self, key, fields=''):
        self.issue_lookups.append(key)
        return {'key': key,
                'fields': {'issuelinks': self.issuelinks_by_key.get(key, [])}}


def epic_node(x=0, y=0, summary='Big epic'):
    return _new_node(Epic(summary=summary, description='d', epic_name=summary), x, y)


def story_node(summary, x=0, y=0):
    return _new_node(Story(summary=summary, description='d', story_points='3'), x, y)


def task_node(summary, x=0, y=0):
    return _new_node(Task(summary=summary, description='d'), x, y)


def plan_with(nodes, edges=None):
    p = new_plan('test')
    p['nodes'] = list(nodes)
    p['edges'] = list(edges or [])
    return p


def test_parent_created_before_child_and_epic_link_set():
    parent, child = epic_node(), story_node('story A')
    edges = []
    _set_parent_edge(edges, parent, child)
    plan = plan_with([child, parent], edges)  # child listed first on purpose

    jira = FakeJira()
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)

    assert clean
    assert [p['fields']['summary'] for _, p in jira.created] == ['Big epic', 'story A']
    child_payload = jira.created[1][1]['fields']
    assert child_payload['customfield_10014'] == jira.created[0][0]  # epic-child
    assert 'T-1' in msg and 'T-2' in msg  # created keys are reported


def test_non_epic_parent_maps_to_subtask_field():
    parent, child = task_node('parent task'), task_node('child task')
    edges = []
    _set_parent_edge(edges, parent, child)
    jira = FakeJira()
    _, clean = _push_plan_to_jira(jira, CONFIG, plan_with([parent, child], edges))
    assert clean
    assert jira.created[1][1]['fields']['parent'] == {'key': jira.created[0][0]}


def test_incomplete_draft_blocks_whole_push():
    bad = story_node('no points')
    bad['ticket']['story_points'] = ''
    jira = FakeJira()
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan_with([bad]))
    assert not clean
    assert 'Push blocked' in msg and 'story_points' in msg
    assert jira.created == []


def test_link_direction_and_reverse():
    a, b = task_node('a'), task_node('b')
    edges = [{'from': a['node_id'], 'to': b['node_id'],
              'category': 'link', 'rel': 'Blocks', 'reverse': False}]
    jira = FakeJira()
    _, clean = _push_plan_to_jira(jira, CONFIG, plan_with([a, b], edges))
    assert clean
    assert jira.links == [(a['jira_key'], b['jira_key'], 'Blocks')]

    a2, b2 = task_node('a2'), task_node('b2')
    edges2 = [{'from': a2['node_id'], 'to': b2['node_id'],
               'category': 'link', 'rel': 'Blocks', 'reverse': True}]
    jira2 = FakeJira()
    _push_plan_to_jira(jira2, CONFIG, plan_with([a2, b2], edges2))
    assert jira2.links == [(b2['jira_key'], a2['jira_key'], 'Blocks')]


def test_retry_after_partial_failure_skips_what_succeeded():
    parent, child = task_node('parent ok'), task_node('child fails')
    edges = []
    _set_parent_edge(edges, parent, child)
    edges.append({'from': parent['node_id'], 'to': child['node_id'],
                  'category': 'link', 'rel': 'Relates', 'reverse': False})
    plan = plan_with([parent, child], edges)

    jira = FakeJira(fail_summaries={'child fails'})
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert not clean
    assert len(jira.created) == 1  # parent landed, child failed, link impossible

    jira.fail_summaries.clear()
    msg2, clean2 = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean2
    summaries = [p['fields']['summary'] for _, p in jira.created]
    assert summaries == ['parent ok', 'child fails']  # parent NOT recreated
    assert len(jira.links) == 1  # link created exactly once across both pushes


def test_repush_of_clean_plan_does_nothing():
    a, b = task_node('a'), task_node('b')
    edges = [{'from': a['node_id'], 'to': b['node_id'],
              'category': 'link', 'rel': 'Blocks', 'reverse': False}]
    plan = plan_with([a, b], edges)
    jira = FakeJira()
    _push_plan_to_jira(jira, CONFIG, plan)
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean
    assert len(jira.created) == 2 and len(jira.links) == 1  # unchanged
    assert 'Created 0' in msg


def test_existing_child_updated_once():
    parent = epic_node()
    existing = task_node('was already real')
    existing['kind'] = 'existing'
    existing['jira_key'] = 'E-9'
    edges = []
    _set_parent_edge(edges, parent, existing)
    plan = plan_with([parent, existing], edges)

    jira = FakeJira()
    _, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean
    assert jira.updated == [('E-9', {'customfield_10014': jira.created[0][0]})]

    _push_plan_to_jira(jira, CONFIG, plan)
    assert len(jira.updated) == 1  # hierarchy edge marked pushed — not re-applied


# ── Hydration (Open from Jira) ────────────────────────────────────────────────

class FakeJiraHydrate:
    """Serves a hierarchy via per-parent children maps + a pool of linked
    issues fetchable by key (mirrors get_issue/get_children/get_issues_by_keys)."""

    def __init__(self, issues_by_key: dict, children_of: dict | None = None):
        self.issues = issues_by_key
        self.children_of = children_of or {}
        self.children_calls: list = []

    def get_issue(self, key, fields=''):
        return self.issues[key]

    def get_children(self, keys, epic_link_cf=None, fields='', max_total=200):
        keys = [keys] if isinstance(keys, str) else list(keys)
        self.children_calls.append(keys)
        return [self.issues[c] for k in keys for c in self.children_of.get(k, [])]

    def get_issues_by_keys(self, keys, fields='', max_total=100):
        return [self.issues[k] for k in keys if k in self.issues]


def _issue(key, itype, summary, issuelinks=None, parent=None, epic=None):
    f = {'summary': summary, 'issuetype': {'name': itype},
         'issuelinks': issuelinks or []}
    if parent:
        f['parent'] = {'key': parent}
    if epic:
        f['customfield_10014'] = epic
    return {'key': key, 'fields': f}


def test_hydration_builds_existing_nodes_and_edges():
    blocks = {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'}
    root = _issue('E-1', 'Epic', 'Root epic',
                  issuelinks=[{'id': '77', 'type': blocks,
                               'outwardIssue': {'key': 'C-1'}}])
    child = _issue('C-1', 'Story', 'Child story', epic='E-1',
                   issuelinks=[{'id': '77', 'type': blocks,
                                'inwardIssue': {'key': 'E-1'}}])
    jira = FakeJiraHydrate({'E-1': root, 'C-1': child}, {'E-1': ['C-1']})
    plan = _plan_from_jira(jira, CONFIG, 'E-1')

    assert plan['name'].startswith('E-1')
    assert all(n['kind'] == 'existing' for n in plan['nodes'])
    assert {n['jira_key'] for n in plan['nodes']} == {'E-1', 'C-1'}

    hier = [e for e in plan['edges'] if e.get('category') == 'hierarchy']
    links = [e for e in plan['edges'] if e.get('category') == 'link']
    assert len(hier) == 1 and hier[0]['rel'] == 'epic-child' and hier[0]['pushed']
    # Link 77 appears on both endpoints but maps to exactly one E-1 → C-1 edge.
    assert len(links) == 1 and links[0]['rel'] == 'Blocks' and links[0]['pushed']
    by_id = {n['node_id']: n['jira_key'] for n in plan['nodes']}
    assert by_id[links[0]['from']] == 'E-1' and by_id[links[0]['to']] == 'C-1'

    # A purely hydrated graph holds no local work → never persisted.
    assert not _worth_saving(plan)


def test_hydration_recurses_and_pulls_linked_tickets():
    blocks = {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'}
    root = _issue('E-1', 'Epic', 'Root epic',
                  issuelinks=[{'id': '70', 'type': blocks,
                               'outwardIssue': {'key': 'X-9'}}])
    story = _issue('S-1', 'Story', 'Story', epic='E-1')
    sub = _issue('S-2', 'Task', 'Grandchild', parent='S-1')
    # X-9 is outside the tree, linked off the root; its own links must NOT be
    # expanded into further fetches (one hop only).
    outside = _issue('X-9', 'Bug', 'Outside blocker',
                     issuelinks=[{'id': '70', 'type': blocks,
                                  'inwardIssue': {'key': 'E-1'}},
                                 {'id': '71', 'type': blocks,
                                  'outwardIssue': {'key': 'Z-1'}}])
    jira = FakeJiraHydrate({'E-1': root, 'S-1': story, 'S-2': sub, 'X-9': outside},
                           {'E-1': ['S-1'], 'S-1': ['S-2']})
    plan = _plan_from_jira(jira, CONFIG, 'E-1')

    keys = {n['jira_key'] for n in plan['nodes']}
    assert keys == {'E-1', 'S-1', 'S-2', 'X-9'}        # grandchild + linked, no Z-1
    by_id = {n['node_id']: n['jira_key'] for n in plan['nodes']}
    hier = {(by_id[e['from']], by_id[e['to']], e['rel'])
            for e in plan['edges'] if e.get('category') == 'hierarchy'}
    assert hier == {('E-1', 'S-1', 'epic-child'), ('S-1', 'S-2', 'subtask')}
    links = [e for e in plan['edges'] if e.get('category') == 'link']
    assert len(links) == 1 and links[0]['link_id'] == '70'  # Z-1 edge dropped
    # One get_children call per level (E-1, then S-1, then S-2's empty level).
    assert jira.children_calls[0] == ['E-1'] and jira.children_calls[1] == ['S-1']


def test_hydration_respects_max_nodes_guard():
    issues = {'E-1': _issue('E-1', 'Epic', 'root')}
    issues.update({f'C-{i}': _issue(f'C-{i}', 'Task', f'c{i}', parent='E-1')
                   for i in range(10)})
    jira = FakeJiraHydrate(issues, {'E-1': [f'C-{i}' for i in range(10)]})
    plan = _plan_from_jira(jira, CONFIG, 'E-1', max_nodes=4)
    assert len(plan['nodes']) == 4  # root + 3 children, capped


def test_worth_saving_with_local_work():
    plan = new_plan('x')
    plan['nodes'].append(task_node('draft'))
    assert _worth_saving(plan)
    plan2 = new_plan('y')
    plan2['edges'].append({'from': 'a', 'to': 'b', 'category': 'link',
                           'rel': 'Blocks'})  # not yet pushed
    assert _worth_saving(plan2)


def test_worth_saving_with_queued_edits():
    plan = new_plan('z')
    n = task_node('was real')
    n['kind'] = 'existing'
    n['jira_key'] = 'E-1'
    n['orig_ticket'] = dict(n['ticket'])
    n['ticket'] = {**n['ticket'], 'summary': 'edited'}
    plan['nodes'].append(n)
    assert _worth_saving(plan)

    plan2 = new_plan('w')
    plan2['pending_unlinks'] = [{'rel': 'Blocks', 'from_key': 'A-1', 'to_key': 'A-2'}]
    assert _worth_saving(plan2)


# ── Relationship helpers (hover-arrow spawn flow) ─────────────────────────────

def test_build_link_options_both_directions():
    types = [{'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'}]
    opts = _build_link_options(types)
    assert [lbl for lbl, _ in opts] == ['blocks  (Blocks)', 'is blocked by  (Blocks)']
    assert opts[0][1] == {'category': 'link', 'rel': 'Blocks', 'reverse': False}
    assert opts[1][1]['reverse'] is True
    assert _build_link_options([]) == []


def test_apply_relationship_child_parent_link():
    src, other = epic_node(), story_node('s')
    edges: list = []
    _apply_relationship(edges, src, other, {'category': 'hierarchy', 'dir': 'child'})
    assert edges == [{'from': src['node_id'], 'to': other['node_id'],
                      'category': 'hierarchy', 'rel': 'epic-child'}]

    _apply_relationship(edges, src, other, {'category': 'hierarchy', 'dir': 'parent'})
    # source now nests under the new node (Story parent ⇒ subtask); the old
    # child edge is untouched (it points at `other`, not `src`).
    assert {'from': other['node_id'], 'to': src['node_id'],
            'category': 'hierarchy', 'rel': 'subtask'} in edges

    edges2: list = []
    _apply_relationship(edges2, src, other,
                        {'category': 'link', 'rel': 'Blocks', 'reverse': True})
    assert edges2 == [{'from': src['node_id'], 'to': other['node_id'],
                       'category': 'link', 'rel': 'Blocks', 'reverse': True}]


def test_edge_at_point_geometry():
    a, b = task_node('a'), task_node('b')
    a['x'], a['y'] = 0, 0
    b['x'], b['y'] = 300, 0     # horizontal edge along y=25 (node centers)
    by_id = {a['node_id']: a, b['node_id']: b}
    edges = [{'from': a['node_id'], 'to': b['node_id'],
              'category': 'link', 'rel': 'Blocks'}]
    assert _edge_at_point((220, 27), by_id, edges) == 0      # near the line
    assert _edge_at_point((220, 60), by_id, edges) is None   # in bbox, far from line


# ── Two-way editing of pushed content ─────────────────────────────────────────

def existing(summary, key):
    n = task_node(summary)
    n['kind'] = 'existing'
    n['jira_key'] = key
    return n


def test_node_field_diff_and_adf():
    old = {'summary': 'a', 'description': 'x', 'priority': 'Medium'}
    new = {'summary': 'b', 'description': 'x', 'priority': 'High'}
    diff = _node_field_diff(old, new)
    assert diff == {'summary': ('a', 'b'), 'priority': ('Medium', 'High')}
    adf = {'type': 'doc', 'content': [
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'line one'}]},
        {'type': 'paragraph', 'content': [{'type': 'text', 'text': 'line two'}]}]}
    assert _adf_to_text(adf) == 'line one\nline two'
    assert _adf_to_text('plain') == 'plain'


def test_changed_jira_fields_only_sends_diff():
    n = existing('old title', 'E-1')
    n['orig_ticket'] = dict(n['ticket'])
    n['ticket'] = {**n['ticket'], 'summary': 'new title'}
    fields = _changed_jira_fields(n, 'T')
    assert fields == {'summary': 'new title'}


def test_push_applies_queued_field_updates():
    n = existing('old', 'E-1')
    n['orig_ticket'] = dict(n['ticket'])
    n['ticket'] = {**n['ticket'], 'summary': 'renamed'}
    plan = plan_with([n])
    jira = FakeJira()
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean
    assert jira.updated == [('E-1', {'summary': 'renamed'})]
    assert 'orig_ticket' not in n  # queue drained
    assert 'updated 1 existing' in msg


def test_push_applies_queued_unlinks():
    a, b = existing('a', 'E-1'), existing('b', 'E-2')
    plan = plan_with([a, b])
    by_id = {n['node_id']: n for n in plan['nodes']}
    # A pushed link with a known id, deleted from the canvas:
    edge = {'from': a['node_id'], 'to': b['node_id'], 'category': 'link',
            'rel': 'Blocks', 'pushed': True, 'link_id': '99'}
    _queue_unlink(plan, edge, by_id)
    # A pushed subtask hierarchy edge:
    hier = {'from': a['node_id'], 'to': b['node_id'], 'category': 'hierarchy',
            'rel': 'subtask', 'pushed': True}
    _queue_unlink(plan, hier, by_id)

    jira = FakeJira()
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean
    assert jira.deleted_links == ['99']
    assert ('E-2', {'parent': None}) in jira.updated
    assert 'pending_unlinks' not in plan  # drained and removed
    assert 'removed 2 relationship(s)' in msg


def test_unlink_without_id_resolves_via_lookup():
    a, b = existing('a', 'E-1'), existing('b', 'E-2')
    plan = plan_with([a, b])
    by_id = {n['node_id']: n for n in plan['nodes']}
    _queue_unlink(plan, {'from': a['node_id'], 'to': b['node_id'],
                         'category': 'link', 'rel': 'Blocks', 'pushed': True}, by_id)
    jira = FakeJira()
    jira.issuelinks_by_key['E-1'] = [
        {'id': '123', 'type': {'name': 'Blocks'}, 'outwardIssue': {'key': 'E-2'}},
        {'id': '124', 'type': {'name': 'Relates'}, 'outwardIssue': {'key': 'E-9'}}]
    _, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert clean
    assert jira.issue_lookups == ['E-1']
    assert jira.deleted_links == ['123']


def test_failed_unlink_keeps_queue_and_plan_dirty():
    a, b = existing('a', 'E-1'), existing('b', 'E-2')
    plan = plan_with([a, b])
    by_id = {n['node_id']: n for n in plan['nodes']}
    _queue_unlink(plan, {'from': a['node_id'], 'to': b['node_id'], 'category': 'link',
                         'rel': 'Blocks', 'pushed': True, 'link_id': '7'}, by_id)
    jira = FakeJira()
    jira.fail_unlinks = True
    msg, clean = _push_plan_to_jira(jira, CONFIG, plan)
    assert not clean
    assert len(plan['pending_unlinks']) == 1  # retained for retry
    assert _worth_saving(plan)


def test_touches_existing_gates_the_diff_preview():
    plain = plan_with([task_node('a')])
    by_id = {n['node_id']: n for n in plain['nodes']}
    assert not _touches_existing(plain, by_id)

    with_unlink = plan_with([task_node('a')])
    with_unlink['pending_unlinks'] = [{'rel': 'Blocks'}]
    assert _touches_existing(with_unlink, by_id)

    parent, draft = existing('real', 'E-1'), task_node('new child')
    edges: list = []
    _set_parent_edge(edges, parent, draft)
    attached = plan_with([parent, draft], edges)
    by_id2 = {n['node_id']: n for n in attached['nodes']}
    assert _touches_existing(attached, by_id2)


def test_node_size_defaults_and_resize():
    n = task_node('a')
    assert _node_size(n) == (150, 50)
    n['w'], n['h'] = 220, 90
    assert _node_size(n) == (220, 90)


def test_edge_segment_respects_per_node_size():
    a, b = task_node('a'), task_node('b')
    a['x'], a['y'] = 0, 0
    b['x'], b['y'] = 300, 0
    b['w'], b['h'] = 200, 100   # wider/taller target
    ca, tip = _edge_segment(a, b)
    assert ca == (75, 25)                     # a's center (default size)
    assert tip[0] == 300                      # stops at b's left border (x=300)
    # And hit-testing follows the resized geometry:
    by_id = {a['node_id']: a, b['node_id']: b}
    edges = [{'from': a['node_id'], 'to': b['node_id'],
              'category': 'link', 'rel': 'Blocks'}]
    assert _edge_at_point((200, (25 + tip[1]) / 2 + 1), by_id, edges) == 0


def test_push_change_summary_groups_per_ticket():
    epic = existing('Payments epic', 'E-1')
    epic['orig_ticket'] = dict(epic['ticket'])
    epic['ticket'] = {**epic['ticket'], 'summary': 'Payments epic v2',
                      'sprint': 'Sprint 9'}
    blocker = task_node('SomeBlocker')
    edges: list = []
    edges.append({'from': epic['node_id'], 'to': blocker['node_id'],
                  'category': 'link', 'rel': 'Blocks', 'reverse': False})
    plan = plan_with([epic, blocker], edges)
    plan['pending_unlinks'] = [{'rel': 'Relates', 'from_key': 'E-1',
                                'to_key': 'X-9', 'hier': False}]
    by_id = {n['node_id']: n for n in plan['nodes']}

    text = _push_change_summary(plan, by_id)
    lines = text.split('\n')
    # Ticket titles flush left, changes tabbed beneath them.
    assert 'E-1' in lines and 'SomeBlocker' in lines
    # Field changes use the form labels (summary → Summary, sprint → Sprint).
    assert any(ln.startswith("\tSummary: 'Payments epic'") for ln in lines)
    assert any(ln.startswith("\tSprint: '' → 'Sprint 9'") for ln in lines)
    # New node gets a Create line, with its relationship grouped under it.
    sb = lines.index('SomeBlocker')
    assert lines[sb + 1] == '\tCreate (Task)'
    assert lines[sb + 2].startswith('\tRelationship:') and 'Blocks' in lines[sb + 2]
    # The queued removal groups under the existing ticket.
    assert '\tRemove relationship: Relates → X-9' in lines


def test_hydrated_link_edges_carry_link_id():
    blocks = {'name': 'Blocks', 'inward': 'is blocked by', 'outward': 'blocks'}
    root = _issue('E-1', 'Epic', 'Root',
                  issuelinks=[{'id': '55', 'type': blocks,
                               'outwardIssue': {'key': 'C-1'}}])
    child = _issue('C-1', 'Story', 'Child', epic='E-1')
    jira = FakeJiraHydrate({'E-1': root, 'C-1': child}, {'E-1': ['C-1']})
    plan = _plan_from_jira(jira, CONFIG, 'E-1')
    links = [e for e in plan['edges'] if e.get('category') == 'link']
    assert links[0]['link_id'] == '55'


# ── Audit-refactor fixes ──────────────────────────────────────────────────────

def test_set_parent_edge_returns_dropped():
    p1, p2, c = task_node('p1'), task_node('p2'), task_node('c')
    edges: list = []
    assert _set_parent_edge(edges, p1, c) == []
    dropped = _set_parent_edge(edges, p2, c)
    assert len(dropped) == 1 and dropped[0]['from'] == p1['node_id']
    assert len([e for e in edges if e.get('category') == 'hierarchy']) == 1


def test_renest_unlink_queueing_rules():
    epic1, epic2 = epic_node(summary='E one'), epic_node(summary='E two')
    for n, k in ((epic1, 'E-1'), (epic2, 'E-2')):
        n['kind'], n['jira_key'] = 'existing', k
    tsk = existing('a task', 'T-1')
    child = existing('the child', 'C-1')
    edges: list = []
    _set_parent_edge(edges, epic1, child)
    edges[-1]['pushed'] = True
    plan = plan_with([epic1, epic2, tsk, child], edges)
    by_id = {n['node_id']: n for n in plan['nodes']}

    # Same-rel re-parent (epic-child → epic-child): plain overwrite, no unlink.
    _apply_relationship(edges, epic2, child,
                        {'category': 'hierarchy', 'dir': 'child'}, plan, by_id)
    assert not plan.get('pending_unlinks')

    # Cross-type re-nest (epic-child → subtask): old epic-link must be cleared.
    edges[-1]['pushed'] = True
    _apply_relationship(edges, tsk, child,
                        {'category': 'hierarchy', 'dir': 'child'}, plan, by_id)
    assert len(plan['pending_unlinks']) == 1
    u = plan['pending_unlinks'][0]
    assert u['hier'] and u['rel'] == 'epic-child' and u['to_key'] == 'C-1'


def test_incomplete_nodes_lists_missing_fields():
    bad = story_node('no points')
    bad['ticket']['story_points'] = ''
    fine = existing('fine', 'E-1')
    plan = plan_with([bad, fine])
    msgs = _incomplete_nodes(plan)
    assert len(msgs) == 1 and 'story_points' in msgs[0]
    assert _incomplete_nodes(plan_with([fine])) == []


def test_view_node_transform():
    n = task_node('v')
    n['x'], n['y'], n['w'], n['h'] = 100, 50, 200, 80
    assert _view_node(n, 1.0) == {**n}  # identity at z=1, no pan
    v = _view_node(n, 0.5, (10, 20))
    assert (v['x'], v['y'], v['w'], v['h']) == (60.0, 45.0, 100.0, 40.0)
    assert (n['x'], n['w']) == (100, 200)  # world coords never mutated
