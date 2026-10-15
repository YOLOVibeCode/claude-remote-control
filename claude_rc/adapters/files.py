"""ManifestStore and StateStore as private JSON files, written atomically."""
from __future__ import annotations

import json
import os
import tempfile
from typing import Dict, List, Tuple

from ..core import TIERS, ChannelHealth, Entry, History, State


def write_private(path: str, data: object) -> None:
    os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def read_json(path: str, default):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


class JsonManifestStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> List[Entry]:
        data = read_json(self.path, {"sessions": []})
        return [
            Entry(name=s["name"], dir=s["dir"], conversation=s["conversation"],
                  flags=tuple(s.get("flags", ())), enabled=bool(s.get("enabled", True)),
                  tier=s.get("tier") if s.get("tier") in TIERS else "auto")
            for s in data.get("sessions", [])
        ]

    def save(self, entries: List[Entry]) -> None:
        write_private(self.path, {"sessions": [
            {"name": e.name, "dir": e.dir, "conversation": e.conversation, "flags": list(e.flags), "enabled": e.enabled,
             "tier": e.tier}
            for e in entries
        ]})


class JsonStateStore:
    def __init__(self, path: str):
        self.path = path

    def load(self) -> Tuple[Dict[str, str], Dict[str, History], List[str]]:
        data = read_json(self.path, {})
        histories = {
            name: History(
                restarts=tuple(h.get("restarts", ())),
                last_state=State(h["last_state"]) if h.get("last_state") else None,
                idle_unlinked_runs=int(h.get("idle_unlinked_runs", 0)),
                relinked_at=h.get("relinked_at"),
                note=h.get("note"),
                parked_at=h.get("parked_at"),
                parked_reason=h.get("parked_reason"),
                last_active=h.get("last_active"),
                throttled_pid=h.get("throttled_pid"),
            )
            for name, h in data.get("histories", {}).items()
        }
        return dict(data.get("conditions", {})), histories, list(data.get("queued", []))

    def save(self, conditions: Dict[str, str], histories: Dict[str, History], queued: List[str]) -> None:
        write_private(self.path, {
            "conditions": conditions,
            "histories": {
                name: {"restarts": list(h.restarts), "last_state": h.last_state.value if h.last_state else None,
                       "idle_unlinked_runs": h.idle_unlinked_runs, "relinked_at": h.relinked_at, "note": h.note,
                       "parked_at": h.parked_at, "parked_reason": h.parked_reason, "last_active": h.last_active,
                       "throttled_pid": h.throttled_pid}
                for name, h in histories.items()
            },
            "queued": queued,
        })


class JsonHealthStore:
    """Per-channel delivery health (health.json): consecutive failures and whether it was escalated."""

    def __init__(self, path: str):
        self.path = path

    def load(self) -> Dict[str, ChannelHealth]:
        data = read_json(self.path, {})
        out: Dict[str, ChannelHealth] = {}
        for name, h in (data.items() if isinstance(data, dict) else []):
            if isinstance(h, dict):
                out[name] = ChannelHealth(int(h.get("failures", 0)), h.get("since"), str(h.get("last_error", "")),
                                          bool(h.get("escalated", False)))
        return out

    def save(self, health: Dict[str, ChannelHealth]) -> None:
        write_private(self.path, {name: {"failures": h.failures, "since": h.since, "last_error": h.last_error,
                                         "escalated": h.escalated} for name, h in health.items()})
