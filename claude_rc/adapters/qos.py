"""Process priority on macOS: `taskpolicy -b -p PID` moves a running process to background
(efficiency cores, throttled disk), `-B` moves it back. Elsewhere this does nothing."""
from __future__ import annotations

import os
import sys
from typing import Optional

from .run import run_command


class TaskPolicy:
    def __init__(self, run=run_command, binary: str = "/usr/sbin/taskpolicy", enabled: Optional[bool] = None):
        self._run, self._bin = run, binary
        self.enabled = (sys.platform == "darwin" and os.path.exists(binary)) if enabled is None else enabled

    def background(self, pid: int, on: bool) -> bool:
        if not self.enabled:
            return False
        rc, _ = self._run([self._bin, "-b" if on else "-B", "-p", str(pid)])
        return rc == 0


class NoThrottle:
    enabled = False

    def background(self, pid: int, on: bool) -> bool:
        return False
