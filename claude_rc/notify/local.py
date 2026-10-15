"""A notification on this Mac: the fallback when the alert channel itself keeps failing.

The text goes to osascript as arguments, never inside the script, so nothing in it can be
read as AppleScript.
"""
from __future__ import annotations

import os
import sys
from typing import Optional

from ..adapters.run import run_command


class MacNotification:
    def __init__(self, run=run_command, enabled: Optional[bool] = None, osascript: str = "/usr/bin/osascript"):
        self._run, self._bin = run, osascript
        self.enabled = (sys.platform == "darwin" and os.path.exists(osascript)) if enabled is None else enabled

    def notify(self, title: str, body: str) -> bool:
        if not self.enabled:
            return False
        rc, _ = self._run([self._bin, "-e", "on run argv",
                           "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                           "-e", "end run", title, body[:250]])
        return rc == 0


class NoNotifier:
    def notify(self, title: str, body: str) -> bool:
        return False
