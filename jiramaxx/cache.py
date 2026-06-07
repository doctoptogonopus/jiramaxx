from __future__ import annotations
from datetime import datetime
from pathlib import Path
import yaml
from .models import Ticket, ticket_from_dict


class Cache:
    def __init__(self, directory: str):
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)
        # Sprint snapshots live in a subfolder so the draft glob (*.yaml in self.dir,
        # non-recursive) never mistakes them for drafts.
        self.sprint_dir = self.dir / 'sprints'

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

    # ── Sprint snapshot cache ────────────────────────────────────────────────
    # Persisted as readable YAML so the Manage window opens instantly from disk
    # and only hits the network when the user presses Update. ``mine`` keeps the
    # assigned-to-me list separate from the all-users Release-mode list.

    def _sprint_path(self, mine: bool) -> Path:
        return self.sprint_dir / (f"sprint_{'mine' if mine else 'all'}.yaml")

    def save_sprint_issues(self, issues: list[dict], *, mine: bool) -> None:
        self.sprint_dir.mkdir(parents=True, exist_ok=True)
        with open(self._sprint_path(mine), 'w') as f:
            yaml.dump({'fetched_at': datetime.now().isoformat(), 'issues': issues},
                      f, default_flow_style=False, sort_keys=False)

    def load_sprint_issues(self, *, mine: bool) -> dict | None:
        """Return {'fetched_at', 'issues'} from the last snapshot, or None."""
        path = self._sprint_path(mine)
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
