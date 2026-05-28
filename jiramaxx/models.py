from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime
from typing import Optional
import uuid


def _validate_int(val) -> str | None:
    try:
        int(val)
        return None
    except (ValueError, TypeError):
        return f"must be a whole number, got '{val}'"


# ── Config registry ───────────────────────────────────────────────────────────
# Populated at startup (and on config save) from config.yaml → ticket_types.
# While empty each class falls back to its _default_required / _default_optional.

_TICKET_CONFIG: dict[str, dict] = {}
_JIRA_CONFIG: dict = {}


def init_ticket_config(ticket_types_config: dict):
    global _TICKET_CONFIG
    _TICKET_CONFIG = ticket_types_config or {}


def init_jira_config(jira_config: dict):
    global _JIRA_CONFIG
    _JIRA_CONFIG = jira_config or {}
    sprints = _JIRA_CONFIG.get('sprint_cache') or []
    FIELD_META['sprint']['options'] = ['(Backlog)'] + [s['name'] for s in sprints]


# ── Field metadata ────────────────────────────────────────────────────────────
# Single source of truth for widget type, label, and widget-specific options.
# Add a new field here to make it available for any ticket type via config GUI.

FIELD_META: dict[str, dict] = {
    'summary':            {'type': 'text',      'label': 'Summary'},
    'description':        {'type': 'multiline', 'label': 'Description'},
    'story_points':       {'type': 'spinner',   'label': 'Story Points',
                           'values': [0, 1, 2, 3, 5, 8, 13, 21]},
    'assignee':           {'type': 'text',      'label': 'Assignee (ID or "me")'},
    'labels':             {'type': 'text',      'label': 'Labels (comma-separated)'},
    'sprint':             {'type': 'dropdown',  'label': 'Sprint', 'options': ['(Backlog)']},
    'epic_link':          {'type': 'text',      'label': 'Epic Link'},
    'epic_name':          {'type': 'text',      'label': 'Epic Name'},
    'severity':           {'type': 'dropdown',  'label': 'Severity',
                           'options': ['Critical', 'High', 'Medium', 'Low']},
    'steps_to_reproduce': {'type': 'multiline', 'label': 'Steps to Reproduce'},
    'priority':           {'type': 'dropdown',  'label': 'Priority',
                           'options': ['Highest', 'High', 'Medium', 'Low', 'Lowest']},
}


# ── Base ticket ───────────────────────────────────────────────────────────────
# All form-facing fields live here so any ticket type can use any field via
# config without needing a new dataclass attribute.

@dataclass
class Ticket(ABC):
    # Form fields
    summary: str = ''
    description: str = ''
    assignee: str = ''
    labels: str = ''
    priority: str = 'Medium'
    story_points: str = ''
    sprint: str = ''
    epic_link: str = ''
    epic_name: str = ''
    severity: str = 'Medium'
    steps_to_reproduce: str = ''
    # Metadata — never shown in forms
    ticket_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    jira_key: Optional[str] = None
    submitted: bool = False

    @property
    @abstractmethod
    def ticket_type(self) -> str: ...

    # Subclasses override these two to set hardcoded fallback field lists.
    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description']

    @property
    def _default_optional(self) -> list[str]:
        return ['priority']

    # These read from config first; fall back to the hardcoded defaults above.
    @property
    def required_fields(self) -> list[str]:
        return _TICKET_CONFIG.get(self.ticket_type, {}).get('required', self._default_required)

    @property
    def optional_fields(self) -> list[str]:
        return _TICKET_CONFIG.get(self.ticket_type, {}).get('optional', self._default_optional)

    @property
    def field_validators(self) -> dict[str, object]:
        """Map field_name → callable(value) → error str | None. Override per subclass."""
        return {}

    def all_form_fields(self) -> list[str]:
        seen = set(self.required_fields)
        return self.required_fields + [f for f in self.optional_fields if f not in seen]

    def is_valid(self) -> tuple[bool, list[str]]:
        missing = [f for f in self.required_fields if not str(getattr(self, f, '') or '').strip()]
        return not missing, missing

    def validate_fields(self) -> tuple[bool, list[str]]:
        errors = []
        for field_name, validator in self.field_validators.items():
            val = getattr(self, field_name, '')
            if val == '' or val is None:
                continue
            msg = validator(val)
            if msg:
                errors.append(f"{field_name}: {msg}")
        return not errors, errors

    def to_dict(self) -> dict:
        d = {f.name: getattr(self, f.name) for f in dc_fields(self)}
        d['ticket_type'] = self.ticket_type
        return d

    def apply_form_values(self, values: dict):
        for f in dc_fields(self):
            key = f'-FIELD-{f.name.upper()}-'
            if key in values:
                setattr(self, f.name, values[key])

    def to_jira_payload(self, project_key: str) -> dict:
        payload = {
            'fields': {
                'project': {'key': project_key},
                'summary': self.summary,
                'description': {
                    'type': 'doc', 'version': 1,
                    'content': [{'type': 'paragraph',
                                 'content': [{'type': 'text', 'text': self.description or ''}]}],
                },
                'issuetype': {'name': self.ticket_type},
            }
        }
        if self.priority:
            payload['fields']['priority'] = {'name': self.priority}
        if self.assignee:
            account_id = self.assignee.strip()
            if account_id.lower() == 'me':
                account_id = _JIRA_CONFIG.get('my_account_id', '') or account_id
            if account_id:
                payload['fields']['assignee'] = {'accountId': account_id}
        if self.labels:
            payload['fields']['labels'] = [l.strip() for l in self.labels.split(',') if l.strip()]
        cf = _JIRA_CONFIG.get('custom_fields', {})
        if self.story_points:
            sp_field = cf.get('story_points') or 'customfield_10016'
            try:
                payload['fields'][sp_field] = int(self.story_points)
            except (ValueError, TypeError):
                pass
        if self.epic_link:
            el_field = cf.get('epic_link') or 'customfield_10014'
            link = self.epic_link.strip().rstrip('/')
            if '/' in link:
                link = link.split('/')[-1]
            payload['fields'][el_field] = link
        if self.sprint and self.sprint != '(Backlog)':
            sp_cf = cf.get('sprint') or 'customfield_10020'
            cache = _JIRA_CONFIG.get('sprint_cache') or []
            sprint_id = next((s['id'] for s in cache if s['name'] == self.sprint), None)
            if sprint_id:
                payload['fields'][sp_cf] = sprint_id
        return payload


# ── Concrete types ────────────────────────────────────────────────────────────
# Each subclass only needs to declare ticket_type + default field lists.
# No dataclass fields needed — all fields are inherited from Ticket.

@dataclass
class Story(Ticket):
    @property
    def ticket_type(self) -> str:
        return 'Story'

    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description', 'story_points']

    @property
    def _default_optional(self) -> list[str]:
        return ['assignee', 'labels', 'sprint', 'epic_link', 'priority']

    @property
    def field_validators(self) -> dict:
        return {'story_points': _validate_int}


@dataclass
class Bug(Ticket):
    @property
    def ticket_type(self) -> str:
        return 'Bug'

    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description', 'severity', 'steps_to_reproduce']

    @property
    def _default_optional(self) -> list[str]:
        return ['assignee', 'labels', 'priority']


@dataclass
class Task(Ticket):
    @property
    def ticket_type(self) -> str:
        return 'Task'

    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description']

    @property
    def _default_optional(self) -> list[str]:
        return ['assignee', 'story_points', 'labels', 'priority']

    @property
    def field_validators(self) -> dict:
        return {'story_points': _validate_int}


@dataclass
class Epic(Ticket):
    @property
    def ticket_type(self) -> str:
        return 'Epic'

    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description', 'epic_name']

    @property
    def _default_optional(self) -> list[str]:
        return ['labels', 'priority']

    def to_jira_payload(self, project_key: str) -> dict:
        p = super().to_jira_payload(project_key)
        if self.epic_name:
            en_field = _JIRA_CONFIG.get('custom_fields', {}).get('epic_name') or 'customfield_10011'
            p['fields'][en_field] = self.epic_name
        return p


@dataclass
class Initiative(Ticket):
    @property
    def ticket_type(self) -> str:
        return 'Initiative'

    @property
    def _default_required(self) -> list[str]:
        return ['summary', 'description']

    @property
    def _default_optional(self) -> list[str]:
        return ['labels', 'priority']


TICKET_CLASSES: dict[str, type[Ticket]] = {
    'Story': Story,
    'Bug': Bug,
    'Task': Task,
    'Epic': Epic,
    'Initiative': Initiative,
}


def ticket_from_dict(data: dict) -> Ticket:
    ticket_type = data.pop('ticket_type', 'Task')
    cls = TICKET_CLASSES.get(ticket_type, Task)
    valid = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in valid})
