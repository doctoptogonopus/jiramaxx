from __future__ import annotations
from datetime import datetime
from pathlib import Path
import uuid
import yaml
from .models import Ticket, ticket_from_dict


def new_plan(name: str, epic_key: str | None = None) -> dict:
    """A fresh, empty initiative-planner graph (nodes + relationship edges)."""
    return {
        'plan_id': uuid.uuid4().hex[:8],
        'name': name or 'Untitled plan',
        'epic_key': epic_key,
        'created_at': datetime.now().isoformat(),
        'nodes': [],
        'edges': [],
    }


class Cache:
    def __init__(self, directory: str):
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        # Active-sprint ticket snapshot lives in a sibling folder next to drafts
        # (i.e. directly under the data folder, alongside transcripts) — also keeps
        # it clear of the draft glob (*.yaml in self.dir, non-recursive).
        self.active_tickets_dir = self.dir.parent / 'active_tickets'
        # Initiative-planner graphs live in their own sibling folder.
        self.plans_dir = self.dir.parent / 'plans'

    def _path(self, ticket_id: str) -> Path:
        return self.dir / f"{ticket_id}.yaml"

    def save(self, ticket: Ticket):
        with open(self._path(ticket.ticket_id), 'w', encoding='utf-8') as f:
            yaml.dump(ticket.to_dict(), f, default_flow_style=False, allow_unicode=True)

    def delete(self, ticket_id: str):
        p = self._path(ticket_id)
        if p.exists():
            p.unlink()

    def load_all(self) -> list[Ticket]:
        tickets = []
        for path in self.dir.glob('*.yaml'):
            try:
                with open(path, encoding='utf-8') as f:
                    data = yaml.safe_load(f)
                if data:
                    tickets.append(ticket_from_dict(data))
            except Exception:
                pass
        return sorted(tickets, key=lambda t: t.created_at)

    def drafts(self) -> list[Ticket]:
        return [t for t in self.load_all() if not t.submitted]

    def submitted(self) -> list[Ticket]:
        return [t for t in self.load_all() if t.submitted]

    # ── Active-sprint ticket snapshot ─────────────────────────────────────────
    # Persisted as readable YAML so the Manage window opens instantly from disk and
    # only hits the network when the user presses Update. A single file, overwritten
    # on each refresh — nothing accumulates.

    def _active_tickets_path(self) -> Path:
        return self.active_tickets_dir / 'mine.yaml'

    def save_sprint_issues(self, issues: list[dict]) -> None:
        self.active_tickets_dir.mkdir(parents=True, exist_ok=True)
        with open(self._active_tickets_path(), 'w', encoding='utf-8') as f:
            yaml.dump({'fetched_at': datetime.now().isoformat(), 'issues': issues},
                      f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def load_sprint_issues(self) -> dict | None:
        """Return {'fetched_at', 'issues'} from the last snapshot, or None."""
        path = self._active_tickets_path()
        if not path.exists():
            return None
        try:
            with open(path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data and isinstance(data.get('issues'), list):
                return data
        except Exception:
            pass
        return None

    # ── Issue-link types snapshot ─────────────────────────────────────────────
    # Link types essentially never change, so the planner fetches them at most
    # once per installation and reads this file thereafter. Deleting the file is
    # the manual refresh path.

    def _link_types_path(self) -> Path:
        return self.dir.parent / 'link_types.yaml'

    def save_link_types(self, types: list) -> None:
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        with open(self._link_types_path(), 'w', encoding='utf-8') as f:
            yaml.dump({'fetched_at': datetime.now().isoformat(), 'types': types},
                      f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def load_link_types(self) -> list | None:
        path = self._link_types_path()
        if not path.exists():
            return None
        try:
            with open(path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data and isinstance(data.get('types'), list):
                return data['types']
        except Exception:
            pass
        return None

    # ── Initiative-planner graphs ─────────────────────────────────────────────
    # One YAML file per plan, holding nodes (embedded draft tickets + canvas
    # positions) and relationship edges. Self-contained, separate from drafts.

    def _plan_path(self, plan_id: str) -> Path:
        return self.plans_dir / f"{plan_id}.yaml"

    def save_plan(self, plan: dict) -> None:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        with open(self._plan_path(plan['plan_id']), 'w', encoding='utf-8') as f:
            yaml.dump(plan, f, default_flow_style=False, sort_keys=False,
                      allow_unicode=True)

    def load_plan(self, plan_id: str) -> dict | None:
        path = self._plan_path(plan_id)
        if not path.exists():
            return None
        try:
            with open(path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
            if data and data.get('plan_id'):
                return data
        except Exception:
            pass
        return None

    def list_plans(self) -> list[dict]:
        """All plans, newest first."""
        plans: list[dict] = []
        if self.plans_dir.exists():
            for path in self.plans_dir.glob('*.yaml'):
                try:
                    with open(path, encoding='utf-8') as f:
                        data = yaml.safe_load(f)
                    if data and data.get('plan_id'):
                        plans.append(data)
                except Exception:
                    pass
        return sorted(plans, key=lambda p: p.get('created_at', ''), reverse=True)

    def delete_plan(self, plan_id: str) -> None:
        path = self._plan_path(plan_id)
        if path.exists():
            path.unlink()
