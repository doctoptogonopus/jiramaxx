from __future__ import annotations
from datetime import datetime
from pathlib import Path
import yaml
from .models import Ticket, ticket_from_dict


class Cache:
    def __init__(self, directory: str):
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        # Active-sprint ticket snapshot lives in a sibling folder next to drafts
        # (i.e. directly under the data folder, alongside transcripts) — also keeps
        # it clear of the draft glob (*.yaml in self.dir, non-recursive).
        self.active_tickets_dir = self.dir.parent / 'active_tickets'

    def _path(self, ticket_id: str) -> Path:
        return self.dir / f"{ticket_id}.yaml"

    def save(self, ticket: Ticket):
        with open(self._path(ticket.ticket_id), 'w') as f:
            yaml.dump(ticket.to_dict(), f, default_flow_style=False)

    def delete(self, ticket_id: str):
        p = self._path(ticket_id)
        if p.exists():
            p.unlink()

    def load_all(self) -> list[Ticket]:
        tickets = []
        for path in self.dir.glob('*.yaml'):
            try:
                with open(path) as f:
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
        with open(self._active_tickets_path(), 'w') as f:
            yaml.dump({'fetched_at': datetime.now().isoformat(), 'issues': issues},
                      f, default_flow_style=False, sort_keys=False)

    def load_sprint_issues(self) -> dict | None:
        """Return {'fetched_at', 'issues'} from the last snapshot, or None."""
        path = self._active_tickets_path()
        if not path.exists():
            return None
        try:
            with open(path) as f:
                data = yaml.safe_load(f)
            if data and isinstance(data.get('issues'), list):
                return data
        except Exception:
            pass
        return None
