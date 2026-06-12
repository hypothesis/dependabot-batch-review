"""
Append-only JSONL audit trail for sweeps.

Each consequential step of a sweep — the classified plan, every merge, every
health verdict, every rollback — is appended as one JSON object per line, so a
TUI session (or any live run) leaves a durable record of what the automation
did and why, independent of Slack or the terminal scrollback.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    """Append-only JSON-lines log; one ``record`` call per event."""

    def __init__(self, path: Path) -> None:
        self._path = path
        if path.parent != Path(""):
            path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def record(self, event: str, **fields: object) -> None:
        entry: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
        }
        entry.update(fields)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")


def default_audit_path(now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return Path(f"sweep-audit-{stamp}.jsonl")
