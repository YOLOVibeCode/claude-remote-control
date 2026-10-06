"""SessionRegistry over ~/.claude/sessions/<pid>.json, the file Claude Code keeps per running process."""
from __future__ import annotations

import glob
import json
import os
from typing import Callable, List

from ..core import Record


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class FileSessionRegistry:
    def __init__(self, directory: str, pid_alive: Callable[[int], bool] = pid_alive):
        self.directory = directory
        self._alive = pid_alive

    def live_sessions(self) -> List[Record]:
        out: List[Record] = []
        for path in glob.glob(os.path.join(self.directory, "*.json")):
            try:
                with open(path) as f:
                    d = json.load(f)
                pid = int(d["pid"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            pane = d.get("tmux") or ""
            out.append(Record(
                pid=pid,
                conversation=str(d.get("sessionId") or ""),
                cwd=str(d.get("cwd") or ""),
                tmux_session=pane.split(":", 1)[0] or None,
                status=str(d.get("status") or ""),
                linked=bool(d.get("bridgeSessionId")),
                version=str(d.get("version") or ""),
                alive=self._alive(pid),
            ))
        return out
