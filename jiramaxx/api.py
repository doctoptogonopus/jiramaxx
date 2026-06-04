from __future__ import annotations
import os
import requests
from requests.auth import HTTPBasicAuth

# The standard proxy env vars requests honors. We set every case variant so the
# value takes effect regardless of how a downstream lib reads it.
_PROXY_ENV_VARS = ('HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy')


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

    def _post(self, path: str, body: dict) -> dict:
        r = self.session.post(f"{self.base}{path}", json=body)
        self._raise(r)
        # Some endpoints (e.g. POST .../transitions) return 204 No Content on
        # success, so there's no JSON body to parse.
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

    def add_comment(self, issue_key: str, text: str) -> dict:
        return self._post(f'/rest/api/3/issue/{issue_key}/comment', {
            'body': {
                'type': 'doc', 'version': 1,
                'content': [{'type': 'paragraph', 'content': [{'type': 'text', 'text': text}]}],
            }
        })

    def get_transitions(self, issue_key: str) -> list[dict]:
        return self._get(f'/rest/api/3/issue/{issue_key}/transitions').get('transitions', [])

    def transition_issue(self, issue_key: str, transition_id: str):
        self._post(f'/rest/api/3/issue/{issue_key}/transitions', {'transition': {'id': transition_id}})

    def get_active_sprint_issues(self, board_id: int, project_key: str) -> list[dict]:
        data = self._get('/rest/api/3/search/jql', {
            'jql': f'project="{project_key}" AND sprint not in closedSprints() ORDER BY updated DESC',
            'maxResults': 50,
            'fields': 'summary,status,assignee,issuetype,priority',
        })
        return data.get('issues', [])

    def get_sprints(self, project_key: str, sprint_cf: str = 'customfield_10020') -> list[dict]:
        """Extract sprint metadata from issue fields — no Agile API scope required."""
        data = self._get('/rest/api/3/search/jql', {
            'jql': f'project="{project_key}" AND sprint not in closedSprints() ORDER BY updated DESC',
            'maxResults': 100,
            'fields': sprint_cf,
        })
        seen: set[int] = set()
        sprints: list[dict] = []
        for issue in data.get('issues', []):
            for s in (issue.get('fields', {}).get(sprint_cf) or []):
                if isinstance(s, dict) and s.get('id') not in seen:
                    seen.add(s['id'])
                    sprints.append({'id': s['id'], 'name': s.get('name', ''),
                                    'state': s.get('state', '').lower()})
        # active first, then future, drop closed
        order = {'active': 0, 'future': 1}
        return [s for s in sorted(sprints, key=lambda x: order.get(x['state'], 99))
                if s['state'] in ('active', 'future')]

    def get_myself(self) -> dict:
        return self._get('/rest/api/3/myself')

    def get_project(self, project_key: str) -> dict:
        return self._get(f'/rest/api/3/project/{project_key}')

    def check_create_permission(self, project_key: str) -> bool:
        data = self._get('/rest/api/3/mypermissions',
                         {'projectKey': project_key, 'permissions': 'CREATE_ISSUES'})
        return data.get('permissions', {}).get('CREATE_ISSUES', {}).get('havePermission', False)
