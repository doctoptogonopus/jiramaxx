from jiramaxx.cache import Cache, new_plan
from jiramaxx.models import Story, Task


def make_cache(tmp_path) -> Cache:
    return Cache(str(tmp_path / 'data' / 'drafts'))


def test_draft_round_trip_with_unicode(tmp_path):
    c = make_cache(tmp_path)
    t = Story(summary='Café — résumé …', description='d',
              story_points='3')
    c.save(t)
    loaded = c.load_all()
    assert len(loaded) == 1
    assert isinstance(loaded[0], Story)
    assert loaded[0].summary == t.summary


def test_drafts_vs_submitted_split(tmp_path):
    c = make_cache(tmp_path)
    a, b = Task(summary='a', description='d'), Task(summary='b', description='d')
    b.submitted = True
    c.save(a)
    c.save(b)
    assert [t.summary for t in c.drafts()] == ['a']
    assert [t.summary for t in c.submitted()] == ['b']


def test_corrupt_draft_is_skipped(tmp_path):
    c = make_cache(tmp_path)
    c.save(Task(summary='ok', description='d'))
    (c.dir / 'broken.yaml').write_text('{:::not yaml', encoding='utf-8')
    assert [t.summary for t in c.load_all()] == ['ok']


def test_delete_draft(tmp_path):
    c = make_cache(tmp_path)
    t = Task(summary='x', description='d')
    c.save(t)
    c.delete(t.ticket_id)
    c.delete('nonexistent')  # no-op, no raise
    assert c.load_all() == []


def test_plan_round_trip_and_delete(tmp_path):
    c = make_cache(tmp_path)
    p = new_plan('My initiative')
    p['nodes'].append({'node_id': 'n1', 'x': 1, 'y': 2, 'kind': 'draft',
                       'jira_key': None,
                       'ticket': Task(summary='s', description='d').to_dict()})
    c.save_plan(p)
    assert c.plans_dir == c.dir.parent / 'plans'  # sibling, never mixed with drafts
    assert c.load_plan(p['plan_id'])['name'] == 'My initiative'
    assert [q['plan_id'] for q in c.list_plans()] == [p['plan_id']]
    c.delete_plan(p['plan_id'])
    assert c.load_plan(p['plan_id']) is None
    assert c.list_plans() == []


def test_load_plan_tolerates_garbage(tmp_path):
    c = make_cache(tmp_path)
    c.plans_dir.mkdir(parents=True, exist_ok=True)
    (c.plans_dir / 'junk.yaml').write_text('[]', encoding='utf-8')
    assert c.load_plan('junk') is None
    assert c.list_plans() == []


def test_sprint_snapshot_round_trip(tmp_path):
    c = make_cache(tmp_path)
    assert c.load_sprint_issues() is None
    issues = [{'key': 'A-1', 'fields': {'summary': 'café'}}]
    c.save_sprint_issues(issues)
    snap = c.load_sprint_issues()
    assert snap['issues'] == issues and snap['fetched_at']
