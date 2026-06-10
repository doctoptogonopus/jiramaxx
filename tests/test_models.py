from jiramaxx.models import (Bug, Epic, Story, Task, init_jira_config,
                             init_ticket_config, ticket_from_dict)


def test_basic_payload_shape():
    t = Task(summary='Do it', description='Steps')
    p = t.to_jira_payload('ENG')
    f = p['fields']
    assert f['project'] == {'key': 'ENG'}
    assert f['summary'] == 'Do it'
    assert f['issuetype'] == {'name': 'Task'}
    assert f['description']['content'][0]['content'][0]['text'] == 'Steps'


def test_parent_and_labels_and_priority():
    t = Task(summary='s', description='d', labels=' a, b ,,c ', priority='High')
    t.parent = ' ENG-7 '
    f = t.to_jira_payload('ENG')['fields']
    assert f['parent'] == {'key': 'ENG-7'}
    assert f['labels'] == ['a', 'b', 'c']
    assert f['priority'] == {'name': 'High'}


def test_assignee_me_resolves_via_config():
    init_jira_config({'my_account_id': 'abc123'})
    t = Task(summary='s', description='d', assignee='me')
    assert t.to_jira_payload('ENG')['fields']['assignee'] == {'accountId': 'abc123'}
    # Without config, the literal value is passed through.
    init_jira_config({})
    t2 = Task(summary='s', description='d', assignee='someid')
    assert t2.to_jira_payload('ENG')['fields']['assignee'] == {'accountId': 'someid'}


def test_story_points_and_epic_link_custom_fields():
    init_jira_config({'custom_fields': {'story_points': 'customfield_1',
                                        'epic_link': 'customfield_2'}})
    t = Story(summary='s', description='d', story_points='5',
              epic_link='https://x.atlassian.net/browse/ENG-9/')
    f = t.to_jira_payload('ENG')['fields']
    assert f['customfield_1'] == 5
    assert f['customfield_2'] == 'ENG-9'  # URL reduced to the key


def test_sprint_maps_to_cached_id():
    init_jira_config({'sprint_cache': [{'id': 42, 'name': 'Sprint 9', 'state': 'active'}]})
    t = Story(summary='s', description='d', story_points='3', sprint='Sprint 9')
    assert t.to_jira_payload('ENG')['fields']['customfield_10020'] == 42
    t.sprint = '(Backlog)'
    assert 'customfield_10020' not in t.to_jira_payload('ENG')['fields']


def test_epic_name_field():
    t = Epic(summary='s', description='d', epic_name='Big Thing')
    assert t.to_jira_payload('ENG')['fields']['customfield_10011'] == 'Big Thing'


def test_round_trip_from_dict():
    t = Bug(summary='s', description='d', severity='High')
    again = ticket_from_dict(dict(t.to_dict()))
    assert isinstance(again, Bug)
    assert again.to_dict() == t.to_dict()


def test_from_dict_ignores_unknown_keys_and_defaults_to_task():
    t = ticket_from_dict({'ticket_type': 'Nope', 'summary': 's', 'bogus': 1})
    assert isinstance(t, Task)
    assert t.summary == 's'


def test_is_valid_reports_missing_required():
    t = Story(summary='s')  # description + story_points missing
    ok, missing = t.is_valid()
    assert not ok
    assert set(missing) == {'description', 'story_points'}


def test_validate_fields_int_check():
    t = Story(summary='s', description='d', story_points='five')
    ok, errors = t.validate_fields()
    assert not ok and 'story_points' in errors[0]
    t.story_points = '5'
    assert t.validate_fields() == (True, [])


def test_config_overrides_required_fields():
    init_ticket_config({'Task': {'required': ['summary'], 'optional': []}})
    t = Task(summary='only this')
    assert t.is_valid() == (True, [])
