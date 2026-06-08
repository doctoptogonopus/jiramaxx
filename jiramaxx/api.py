from __future__ import annotations
import os
import re
import requests
from requests.auth import HTTPBasicAuth

# The standard proxy env vars requests honors. We set every case variant so the
# value takes effect regardless of how a downstream lib reads it.
_PROXY_ENV_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')


def _jql_str(value: str) -> str:
    """Escape a value for use inside a double-quoted JQL string literal, so a
    project key or status name containing a backslash or quote can't break (or
    inject into) the query. Backslash first, then the quote."""
    return str(value).replace('\\', '\\\\').replace('"', '\\"')


def apply_proxy_env(network: dict | None) -> None:
    """Export a configured ``network.proxy`` to the standard proxy env vars.

    This mirrors setting HTTP(S)_PROXY in your shell: requests (and anything else
    in-process) then routes through it automatically. You don't need to embed
    credentials in the URL — if your corporate proxy requires auth, set the env
    vars yourself with the credentials. A blank field is left untouched, so an
    existing shell-set HTTP(S)_PROXY keeps working.
    """
    proxy = ((network or {}).get('proxy') or '').strip()
    if not proxy:
        return
    for var in _PROXY_ENV_VARS:
        os.environ[var] = proxy


def _network_kwargs(network: dict | None) -> dict:
    """Translate a config ``network`` section into requests kwargs.

    ``ca_bundle`` (a PEM path) maps to ``verify``. The proxy is *not* returned
    here — call :func:`apply_proxy_env` so it flows through requests' env-var
    support instead. Anything blank is omitted so requests falls back to its
    defaults (the OS trust store via truststore, and HTTP(S)_PROXY env vars).
    """
    kwargs: dict = {}
    net = network or {}
    ca = (net.get('ca_bundle') or '').strip()
    if ca:
        kwargs['verify'] = ca
    return kwargs


class JiraClient:
    def __init__(self, base_url: str, user_email: str, api_token: str,
                 token_type: str = 'classic', cloud_id: str = '',
                 verify=None, proxies: dict | None = None):
        if token_type == 'scoped' and cloud_id:
            self.base = f'https://api.atlassian.com/ex/jira/{cloud_id.strip()}'
        else:
            self.base = base_url.rstrip('/')
        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user_email, api_token.strip())
        self.session.headers.update(
            {'Accept': 'application/json', 'Content-Type': 'application/json'})
        # Only override requests' defaults when explicitly configured; otherwise
        # leave them so truststore (OS certs) and HTTP(S)_PROXY env vars apply.
        if verify is not None:
            self.session.verify = verify
        if proxies:
            self.session.proxies.update(proxies)

    @classmethod
    def from_config(cls, config: dict) -> 'JiraClient':
        """Build a client from a full config dict (jira + network sections)."""
        jcfg = config.get('jira', {})
        return cls(
            jcfg.get('base_url', ''),
            jcfg.get('user_email', ''),
            jcfg.get('api_token', ''),
            token_type=jcfg.get('token_type', 'classic'),
            cloud_id=jcfg.get('cloud_id', ''),
            **_network_kwargs(config.get('network', {})),
        )

    @staticmethod
    def discover_cloud_id(site_url: str, verify=None, proxies: dict | None = None) -> str:
        r = requests.get(site_url.rstrip('/') + '/_edge/tenant_info',
                         verify=True if verify is None else verify, proxies=proxies)
        r.raise_for_status()
        return r.json()['cloudId']

    @staticmethod
    def _raise(r: requests.Response):
        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise requests.exceptions.HTTPError(
                f"{r.status_code} {r.reason}\n{detail}", response=r
            )

    def _get(self, path: str, params: dict | None = None) -> dict:
        r = self.session.get(f"{self.base}{path}", params=params)
        self._raise(r)
        return r.json()

    def _search_all(self, jql: str, fields: str, page_size: int = 100,
                    max_total: int = 2000) -> list[dict]:
        """Run a JQL search across all pages. The enhanced `/search/jql` endpoint
        returns a ``nextPageToken`` while more results remain; follow it until it's
        gone (or a safety cap is hit) so large sprints aren't silently truncated."""
        issues: list[dict] = []
        token: str | None = None
        while True:
            params = {'jql': jql, 'maxResults': page_size, 'fields': fields}
            if token:
                params['nextPageToken'] = token
            data = self._get('/rest/api/3/search/jql', params)
            batch = data.get('issues', [])
            issues.extend(batch)
            token = data.get('nextPageToken')
            if not token or not batch or len(issues) >= max_total:
                break
        return issues

    def _post(self, path: str, body: dict) -> dict:
        r = self.session.post(f"{self.base}{path}", json=body)
        self._raise(r)
        # Some endpoints (e.g. POST .../transitions) return 204 No Content on
        # success, so there's no JSON body to parse.
        if not r.content:
            return {}
        return r.json()

    def _put(self, path: str, body: dict) -> dict:
        r = self.session.put(f"{self.base}{path}", json=body)
        self._raise(r)
        if not r.content:
            return {}
        return r.json()

    def create_issue(self, payload: dict) -> dict:
        try:
            return self._post('/rest/api/3/issue', payload)
        except Exception as exc:
            import json
            raise type(exc)(
                f"{exc}\n\n--- Payload sent ---\n{json.dumps(payload, indent=2)}"
            ) from None

    def update_issue(self, issue_key: str, fields: dict) -> dict:
        """Update fields on an existing issue (PUT). Used by the planner to set an
        existing ticket's parent / epic link when it's nested under a plan node."""
        return self._put(f'/rest/api/3/issue/{issue_key}', {'fields': fields})

    def search_issues(self, project_key: str, text: str, max_total: int = 50) -> list[dict]:
        """Live search for existing issues. A key-looking term (e.g. ``PAY-12``)
        matches by key; anything else does a summary text search within the project.
        Returns raw issue dicts (key + summary/issuetype/status fields)."""
        text = (text or '').strip()
        if re.match(r'^[A-Za-z][A-Za-z0-9]*-\d+$', text):
            jql = f'key = "{_jql_str(text)}"'
        else:
            jql = (f'project = "{_jql_str(project_key)}" '
                   f'AND summary ~ "{_jql_str(text)}"')
        return self._search_all(jql + ' ORDER BY updated DESC',
                                'summary,issuetype,status', max_total=max_total)

    def add_comment(self, issue_key: str, text: str) -> dict:
        return self._post(f'/rest/api/3/issue/{issue_key}/comment', {
            'body': {
                'type': 'doc', 'version': 1,
                'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}],
            }
        })

    def get_issue_link_types(self) -> list[dict]:
        """Available issue-link types, e.g. {'name':'Blocks','inward':'is blocked by',
        'outward':'blocks'}. Used by the planner's relationship picker."""
        return self._get('/rest/api/3/issueLinkType').get('issueLinkTypes', [])

    def create_issue_link(self, inward_key: str, outward_key: str, link_type_name: str):
        """Create an issue link of ``link_type_name`` where ``outward_key`` is the
        outward issue and ``inward_key`` is the inward issue (Jira: the outward issue
        '<outward phrase>' the inward issue)."""
        self._post('/rest/api/3/issueLink', {
            'type': {'name': link_type_name},
            'inwardIssue': {'key': inward_key},
            'outwardIssue': {'key': outward_key},
        })

    def get_transitions(self, issue_key: str) -> list[dict]:
        return self._get(f'/rest/api/3/issue/{issue_key}/transitions').get('transitions', [])

    def transition_issue(self, issue_key: str, transition_id: str):
        self._post(f'/rest/api/3/issue/{issue_key}/transitions', {'transition': {'id': transition_id}})

    def get_sprint_issues(self, project_key: str, *, mine: bool = True,
                          status: str | None = None,
                          epic_link_cf: str | None = None) -> list[dict]:
        """Issues in the *active* sprint of ``project_key``.

        ``sprint in openSprints()`` restricts to the active sprint (excludes both
        closed and not-yet-started future sprints). ``mine`` adds an
        ``assignee = currentUser()`` clause; ``status`` filters to a single status
        (used by Release mode). Extra fields (duedate, parent, epic link) are
        requested so the UI can sort and group without follow-up calls.
        """
        clauses = [f'project="{_jql_str(project_key)}"', 'sprint in openSprints()']
        if mine:
            clauses.append('assignee = currentUser()')
        if status:
            clauses.append(f'status = "{_jql_str(status)}"')
        jql = ' AND '.join(clauses) + ' ORDER BY updated DESC'
        fields = ['summary', 'status', 'assignee', 'issuetype', 'priority',
                  'duedate', 'parent']
        if epic_link_cf:
            fields.append(epic_link_cf)
        return self._search_all(jql, ','.join(fields))

    def get_sprints(self, project_key: str, sprint_cf: str = 'customfield_10020') -> list[dict]:
        """Extract sprint metadata from issue fields — no Agile API scope required."""
        jql = (f'project="{_jql_str(project_key)}" '
               'AND sprint not in closedSprints() ORDER BY updated DESC')
        issues = self._search_all(jql, sprint_cf)
        seen: set[int] = set()
        sprints: list[dict] = []
        for issue in issues:
            for s in (issue.get('fields', {}).get(sprint_cf) or []):
                if isinstance(s, dict) and s.get('id') not in seen:
                    seen.add(s['id'])
                    sprints.append({'id': s['id'], 'name': s.get('name', ''),
                                    'state': s.get('state', '').lower()})
        # active first, then future, drop closed
        order = {'active': 0, 'future': 1}
        return [s for s in sorted(sprints, key=lambda x: order.get(x['state'], 99))
                if s['state'] in ('active', 'future')]

    def get_project_statuses(self, project_key: str) -> set[str]:
        """Lowercased set of every status name available across the project's
        issue-type workflows. Used to validate the configured release statuses."""
        data = self._get(f'/rest/api/3/project/{project_key}/statuses')
        names: set[str] = set()
        for itype in (data if isinstance(data, list) else []):
            for st in itype.get('statuses', []):
                name = st.get('name')
                if name:
                    names.add(name.lower())
        return names

    def get_myself(self) -> dict:
        return self._get('/rest/api/3/myself')

    def get_project(self, project_key: str) -> dict:
        return self._get(f'/rest/api/3/project/{project_key}')

    def check_create_permission(self, project_key: str) -> bool:
        data = self._get('/rest/api/3/mypermissions',
                         {'projectKey': project_key, 'permissions': 'CREATE_ISSUES'})
        return data.get('permissions', {}).get('CREATE_ISSUES', {}).get('havePermission', False)
