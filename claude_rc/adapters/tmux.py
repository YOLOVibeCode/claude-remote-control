"""Multiplexer over the tmux CLI. Session targets use `=name` so `Ava` never matches `Ava-2`."""
from __future__ import annotations

import os
import subprocess
from typing import Callable, List, Optional, Sequence, Tuple

Runner = Callable[[List[str]], Tuple[int, str]]


def run_command(argv: List[str]) -> Tuple[int, str]:
    p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=30)
    return p.returncode, p.stdout


class TmuxMultiplexer:
    def __init__(self, run: Runner = run_command, path: Optional[str] = None, tmux: str = "tmux", socket: Optional[str] = None):
        self._run = run
        self._path = path if path is not None else os.environ.get("PATH", "")
        self._tmux = tmux
        self._sock = ["-L", socket] if socket else []

    def _cmd(self, *args: str) -> List[str]:
        return [self._tmux, *self._sock, *args]

    def has_session(self, name: str) -> bool:
        return self._run(self._cmd("has-session", "-t", f"={name}"))[0] == 0

    def new_session(self, name: str, directory: str, argv: Sequence[str]) -> None:
        rc, out = self._run(self._cmd("new-session", "-d", "-s", name, "-c", directory, "-e", f"PATH={self._path}", *argv))
        if rc != 0:
            raise RuntimeError(f"tmux new-session {name} failed: {out.strip()[:200]}")

    def send_keys(self, name: str, text: str) -> None:
        self._run(self._cmd("send-keys", "-t", f"={name}:", "-l", text))
        self._run(self._cmd("send-keys", "-t", f"={name}:", "Enter"))

    def capture(self, name: str, lines: int) -> str:
        return self._run(self._cmd("capture-pane", "-p", "-t", f"={name}:", "-S", f"-{lines}"))[1]
