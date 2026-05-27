from __future__ import annotations
import requests
from requests.auth import HTTPBasicAuth


class JiraClient:
    def __init__(self, base_url: str, user_email: str, api_token: str,
                 token_type: str = 'classic', cloud_id: str = ''):
        self.auth = HTTPBasicAuth(user_email, api_token.strip())
        self.headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if token_type == 'scoped' and cloud_id:
            self.base = f'https://api.atlassian.com/ex/jira/{cloud_id.strip()}'
        else:
            self.base = base_url.rstrip('/')

    @staticmethod
    def discover_cloud_id(site_url: str) -> str:
        r = requests.get(site_url.rstrip('/') + '/_edge/tenant_info')
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
        r = requests.get(f"{self.base}{path}", auth=self.auth, headers=self.headers, params=params)
        self._raise(r)
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        r = requests.post(f"{self.base}{path}", auth=self.auth, headers=self.headers, json=body)
        self._raise(r)
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
            'jql': f'project="{project_key}" AND sprint is not EMPTY ORDER BY updated DESC',
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
                                    'state': s.get('state', '')})
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
