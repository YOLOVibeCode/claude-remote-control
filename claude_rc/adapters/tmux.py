"""Multiplexer over the tmux CLI. Session targets use `=name` so `Ava` never matches `Ava-2`."""
from __future__ import annotations

import os
from typing import Callable, List, Optional, Sequence, Set, Tuple

from ..core import PaneInfo
from .run import NOT_STARTED, TIMED_OUT, run_command

Runner = Callable[[List[str]], Tuple[int, str]]

__all__ = ["TmuxMultiplexer", "run_command"]


class TmuxMultiplexer:
    def __init__(self, run: Runner = run_command, path: Optional[str] = None, tmux: str = "tmux",
                 socket: Optional[str] = None, on_error: Optional[Callable[[str], None]] = None):
        self._run = run
        self._path = path if path is not None else os.environ.get("PATH", "")
        self._tmux = tmux
        self._sock = ["-L", socket] if socket else []
        self.on_error = on_error

    def _cmd(self, *args: str) -> List[str]:
        return [self._tmux, *self._sock, *args]

    def _call(self, *args: str) -> Tuple[int, str]:
        rc, out = self._run(self._cmd(*args))
        if rc in (TIMED_OUT, NOT_STARTED) and self.on_error is not None:
            self.on_error(f"tmux {args[0]}: {out.strip()[:200]}")
        return rc, out

    def has_session(self, name: str) -> bool:
        # A hung tmux counts as "present": never restart a session we could not see.
        return self._call("has-session", "-t", f"={name}")[0] in (0, TIMED_OUT)

    def list_sessions(self) -> Optional[Set[str]]:
        """Every session name, in one call. None when tmux did not answer; empty when no server runs."""
        rc, out = self._call("list-sessions", "-F", "#{session_name}")
        if rc in (TIMED_OUT, NOT_STARTED):
            return None
        if rc != 0:
            return set()  # "no server running"
        return {line for line in out.splitlines() if line}

    def pane_info(self, name: str) -> Optional[PaneInfo]:
        rc, out = self._call("display-message", "-p", "-t", f"={name}:",
                             "#{pane_pid}|#{pane_in_mode}|#{pane_dead}|#{session_windows}|#{window_panes}")
        if rc != 0:
            return None
        try:
            pid, in_mode, dead, windows, panes = out.strip().split("|")
            return PaneInfo(int(pid), in_mode == "1", dead == "1", int(windows), int(panes))
        except ValueError:
            return None

    def new_session(self, name: str, directory: str, argv: Sequence[str]) -> None:
        rc, out = self._call("new-session", "-d", "-s", name, "-c", directory, "-e", f"PATH={self._path}", *argv)
        if rc != 0:
            raise RuntimeError(f"tmux new-session {name} failed: {out.strip()[:200]}")

    def send_keys(self, name: str, text: str) -> None:
        # Two calls on purpose: text then Enter as one burst can read as a paste (newline, not submit).
        rc, out = self._call("send-keys", "-t", f"={name}:", "-l", text)
        if rc != 0:
            raise RuntimeError(f"tmux send-keys {name} failed: {out.strip()[:200]}")  # never press Enter blind
        rc, out = self._call("send-keys", "-t", f"={name}:", "Enter")
        if rc != 0:
            raise RuntimeError(f"tmux send-keys {name} Enter failed: {out.strip()[:200]}")

    def capture(self, name: str, lines: int, ansi: bool = False) -> str:
        style = ("-e",) if ansi else ()
        return self._call("capture-pane", "-p", *style, "-t", f"={name}:", "-S", f"-{lines}")[1]

    def kill_session(self, name: str) -> bool:
        return self._call("kill-session", "-t", f"={name}")[0] == 0
