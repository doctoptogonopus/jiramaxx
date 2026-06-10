import sys
import types

import pytest
import requests

from jiramaxx.api import (JiraClient, KEYRING_SENTINEL, _jql_str, resolve_token,
                          store_token)


class FakeResponse:
    def __init__(self, data=None, ok=True, status_code=200, reason='OK'):
        self._data = data if data is not None else {}
        self.ok = ok
        self.status_code = status_code
        self.reason = reason
        self.content = b'x'
        self.text = 'err-body'

    def json(self):
        return self._data


class FakeSession:
    """Records every call; pops canned responses in order."""

    def __init__(self, responses=None):
        self.calls = []
        self._responses = list(responses or [])

    def _next(self):
        return self._responses.pop(0) if self._responses else FakeResponse()

    def get(self, url, params=None, **kw):
        self.calls.append(('GET', url, params, kw))
        return self._next()

    def post(self, url, json=None, **kw):
        self.calls.append(('POST', url, json, kw))
        return self._next()

    def put(self, url, json=None, **kw):
        self.calls.append(('PUT', url, json, kw))
        return self._next()


def make_client(responses=None) -> tuple[JiraClient, FakeSession]:
    c = JiraClient('https://x.atlassian.net', 'me@x.com', 'tok')
    fake = FakeSession(responses)
    c.session = fake
    return c, fake


def test_jql_str_escapes_backslash_then_quote():
    assert _jql_str('a"b\\c') == 'a\\"b\\\\c'
    assert _jql_str('plain') == 'plain'


def test_search_issues_key_vs_text():
    c, s = make_client([FakeResponse({'issues': []}), FakeResponse({'issues': []})])
    c.search_issues('PROJ', 'PAY-12')
    c.search_issues('PROJ', 'fix "login"')
    jql_key = s.calls[0][2]['jql']
    jql_text = s.calls[1][2]['jql']
    assert jql_key.startswith('key = "PAY-12"')
    assert 'project = "PROJ"' in jql_text and 'summary ~ "fix \\"login\\""' in jql_text


def test_search_all_follows_next_page_token():
    pages = [FakeResponse({'issues': [{'key': f'A-{i}'} for i in range(2)],
                           'nextPageToken': 'tok1'}),
             FakeResponse({'issues': [{'key': 'A-2'}]})]
    c, s = make_client(pages)
    out = c._search_all('jql', 'summary')
    assert len(out) == 3
    assert s.calls[1][2]['nextPageToken'] == 'tok1'


def test_search_all_respects_max_total():
    pages = [FakeResponse({'issues': [{'key': 'A'}] * 5, 'nextPageToken': 't'})]
    c, s = make_client(pages)
    out = c._search_all('jql', 'summary', max_total=3)
    assert len(out) == 5 and len(s.calls) == 1  # stops after the page that crossed the cap


def test_every_request_has_a_timeout():
    c, s = make_client([FakeResponse(), FakeResponse(), FakeResponse()])
    c._get('/x')
    c._post('/x', {})
    c._put('/x', {})
    assert all('timeout' in kw and kw['timeout'] for *_, kw in s.calls)


def test_get_children_jql_uses_cf_syntax():
    c, s = make_client([FakeResponse({'issues': []}), FakeResponse({'issues': []})])
    c.get_children('E-1', 'customfield_10014')
    c.get_children('E-1', None)
    with_cf = s.calls[0][2]['jql']
    without = s.calls[1][2]['jql']
    assert 'parent = "E-1"' in with_cf and 'cf[10014] = "E-1"' in with_cf
    assert 'cf[' not in without


def test_create_issue_error_keeps_type_and_appends_payload():
    c, _ = make_client([FakeResponse({'errors': {'summary': 'bad'}},
                                     ok=False, status_code=400, reason='Bad Request')])
    with pytest.raises(requests.exceptions.HTTPError) as ei:
        c.create_issue({'fields': {'summary': 'X'}})
    assert 'Payload sent' in str(ei.value)
    assert '400' in str(ei.value)


def _fake_keyring(store: dict):
    mod = types.ModuleType('keyring')
    mod.set_password = lambda svc, user, tok: store.__setitem__((svc, user), tok)
    mod.get_password = lambda svc, user: store.get((svc, user))
    return mod


def test_token_keyring_round_trip(monkeypatch):
    store: dict = {}
    monkeypatch.setitem(sys.modules, 'keyring', _fake_keyring(store))
    assert store_token('me@x.com', 's3cret')
    assert store[('jiramaxx', 'me@x.com')] == 's3cret'
    cfg = {'api_token': KEYRING_SENTINEL, 'user_email': 'me@x.com'}
    assert resolve_token(cfg) == 's3cret'


def test_resolve_token_passthrough_and_fallbacks(monkeypatch):
    assert resolve_token({'api_token': ' plain '}) == 'plain'
    assert resolve_token({}) == ''

    broken = types.ModuleType('keyring')

    def _boom(*a):
        raise RuntimeError('locked down')

    broken.get_password = _boom
    broken.set_password = _boom
    monkeypatch.setitem(sys.modules, 'keyring', broken)
    assert resolve_token({'api_token': KEYRING_SENTINEL}) == ''
    assert store_token('u', 't') is False
