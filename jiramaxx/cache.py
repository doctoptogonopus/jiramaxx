from __future__ import annotations
from pathlib import Path
import yaml
from .models import Ticket, ticket_from_dict


class Cache:
    def __init__(self, directory: str):
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True)

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
