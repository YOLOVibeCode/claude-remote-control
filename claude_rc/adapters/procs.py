"""Process facts the door needs: is a pid the Remote Control server, and which processes it started.

The command line is only compared, never logged: it is how the door is recognised, and pids
can be reused.
"""
from __future__ import annotations

from typing import List

from .registry import pid_alive
from .run import run_command


class PsProcesses:
    def __init__(self, run=run_command, max_descendants: int = 64):
        self._run, self._max = run, max_descendants

    def alive(self, pid: int) -> bool:
        return pid_alive(pid)

    def command(self, pid: int) -> str:
        rc, out = self._run(["ps", "-o", "command=", "-p", str(pid)])
        return out.strip() if rc == 0 else ""

    def descendants(self, pid: int) -> List[int]:
        out: List[int] = []
        queue = [pid]
        while queue and len(out) < self._max:
            rc, text = self._run(["pgrep", "-P", str(queue.pop(0))])
            if rc != 0:
                continue
            for line in text.split():
                if line.isdigit() and int(line) not in out:
                    out.append(int(line))
                    queue.append(int(line))
        return out[:self._max]
