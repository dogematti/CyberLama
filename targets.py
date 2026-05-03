"""
CyberLama target registry. Persists pentest targets (IPs/domains) with notes
and tags so the operator can reference them by short name across sessions.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class Target:
    name: str
    host: str
    notes: str = ""
    tags: list[str] = field(default_factory=list)
    added_at: str = ""


class TargetsRegistry:
    """JSON-backed registry of named pentest targets."""

    def __init__(self, path: Path):
        self.path = path
        self._items: dict[str, Target] = {}
        if path.exists():
            self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(
                f"[targets] warning: failed to load {self.path} ({e}); starting fresh.",
                file=sys.stderr,
            )
            return
        if not isinstance(raw, list):
            print(
                f"[targets] warning: {self.path} not a list; starting fresh.",
                file=sys.stderr,
            )
            return
        for entry in raw:
            if not isinstance(entry, dict) or "name" not in entry or "host" not in entry:
                continue
            t = Target(
                name=str(entry["name"]),
                host=str(entry["host"]),
                notes=str(entry.get("notes", "")),
                tags=[str(x) for x in entry.get("tags", []) if isinstance(x, (str, int))],
                added_at=str(entry.get("added_at", "")),
            )
            self._items[t.name] = t

    def add(
        self,
        name: str,
        host: str,
        notes: str = "",
        tags: list[str] | None = None,
    ) -> Target:
        """Add or replace a target by name; persists immediately."""
        t = Target(
            name=name,
            host=host,
            notes=notes,
            tags=list(tags) if tags else [],
            added_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._items[name] = t
        self.save()
        return t

    def remove(self, name: str) -> bool:
        if name not in self._items:
            return False
        del self._items[name]
        self.save()
        return True

    def get(self, name: str) -> Target | None:
        return self._items.get(name)

    def list(self) -> list[Target]:
        return sorted(self._items.values(), key=lambda t: t.name)

    def save(self) -> None:
        """Atomically write JSON via .tmp + rename."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        data = [asdict(t) for t in self.list()]
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.path)


def format_for_context(registry: TargetsRegistry) -> str:
    """Render registry as system-prompt-friendly bullet lines."""
    targets = registry.list()
    if not targets:
        return ""
    lines = []
    for t in targets:
        tag_part = f" [tags: {', '.join(t.tags)}]" if t.tags else ""
        note_part = f" — {t.notes}" if t.notes else ""
        lines.append(f"- {t.name} ({t.host}){tag_part}{note_part}")
    return "\n".join(lines)
