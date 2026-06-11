import yaml

from jiramaxx import main as m


def _point_config_at(monkeypatch, path):
    monkeypatch.setattr(m, 'CONFIG_PATH', path)


def test_missing_file_writes_and_returns_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'config.yaml'
    _point_config_at(monkeypatch, p)
    cfg = m.load_config()
    assert p.exists()
    assert cfg['jira']['project_key'] == 'ENG'


def test_empty_file_merges_to_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'config.yaml'
    p.write_text('', encoding='utf-8')
    _point_config_at(monkeypatch, p)
    cfg = m.load_config()
    assert cfg['hotkeys']['create_ticket'] == 'ctrl+alt+j'
    assert cfg['ui']['theme'] == 'DarkBlue3'


def test_partial_file_keeps_user_values_and_fills_gaps(tmp_path, monkeypatch):
    p = tmp_path / 'config.yaml'
    p.write_text(yaml.dump({'jira': {'project_key': 'ZZZ'},
                            'hotkeys': {'create_ticket': 'ctrl+alt+x'}}),
                 encoding='utf-8')
    _point_config_at(monkeypatch, p)
    cfg = m.load_config()
    assert cfg['jira']['project_key'] == 'ZZZ'          # user value wins
    assert cfg['jira']['base_url']                      # default filled in
    assert cfg['hotkeys']['create_ticket'] == 'ctrl+alt+x'
    assert cfg['hotkeys']['manage_tickets'] == 'ctrl+alt+m'  # sibling default kept


def test_garbage_yaml_merges_to_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'config.yaml'
    p.write_text('- just\n- a\n- list\n', encoding='utf-8')
    _point_config_at(monkeypatch, p)
    cfg = m.load_config()
    assert cfg['jira']['project_key'] == 'ENG'


def test_merge_never_aliases_defaults(tmp_path, monkeypatch):
    p = tmp_path / 'config.yaml'
    p.write_text('', encoding='utf-8')
    _point_config_at(monkeypatch, p)
    cfg = m.load_config()
    cfg['jira']['project_key'] = 'MUTATED'
    assert m.DEFAULT_CONFIG['jira']['project_key'] == 'ENG'


def test_is_configured():
    assert not m.is_configured({})
    assert not m.is_configured({'jira': {'api_token': '   '}})
    assert m.is_configured({'jira': {'api_token': 'abc'}})
