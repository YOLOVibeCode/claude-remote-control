"""Which folders Claude Code trusts, from ~/.claude.json (projects.<dir>.hasTrustDialogAccepted).

Only that one flag is read. If the file cannot be read, everything counts as trusted so the
watchdog still restarts (and reports RESTART_FAILED if a prompt does appear). The file is large
and asked about once per session, so it is parsed once and re-read only when it changes.
"""
from __future__ import annotations

import json
import os
from typing import Optional, Tuple


class ClaudeJsonTrust:
    def __init__(self, path: str = os.path.expanduser("~/.claude.json")):
        self.path = path
        self._stamp: Optional[Tuple[int, int]] = None
        self._projects: Optional[dict] = None

    def _load(self) -> Optional[dict]:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
            if stamp != self._stamp:
                with open(self.path) as f:
                    projects = json.load(f).get("projects", {})
                self._projects = projects if isinstance(projects, dict) else {}
                self._stamp = stamp
        except (OSError, ValueError):
            return None
        return self._projects

    def trusted(self, directory: str) -> bool:
        projects = self._load()
        if projects is None:
            return True
        entry = projects.get(directory)
        return bool(isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"))
