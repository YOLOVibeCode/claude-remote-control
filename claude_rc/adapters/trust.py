"""Which folders Claude Code trusts, from ~/.claude.json (projects.<dir>.hasTrustDialogAccepted).

Only that one flag is read. If the file cannot be read, everything counts as trusted so the
watchdog still restarts (and reports RESTART_FAILED if a prompt does appear).
"""
from __future__ import annotations

import json
import os


class ClaudeJsonTrust:
    def __init__(self, path: str = os.path.expanduser("~/.claude.json")):
        self.path = path

    def trusted(self, directory: str) -> bool:
        try:
            with open(self.path) as f:
                projects = json.load(f).get("projects", {})
        except (OSError, ValueError):
            return True
        entry = projects.get(directory)
        return bool(isinstance(entry, dict) and entry.get("hasTrustDialogAccepted"))
