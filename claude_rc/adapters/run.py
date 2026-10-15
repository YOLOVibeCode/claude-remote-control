"""Run a command and return (exit code, output). Never raises: a hung or missing program is a result.

A watch run that raises saves no state, sends no heartbeat and delivers no alerts, so every
external call goes through here. 124 means it timed out, 127 means it could not start.
"""
from __future__ import annotations

import subprocess
from typing import List, Tuple

TIMED_OUT = 124
NOT_STARTED = 127


def run_command(argv: List[str], timeout: float = 10) -> Tuple[int, str]:
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return TIMED_OUT, f"timed out after {timeout:g}s: {' '.join(argv[:2])}"
    except OSError as e:
        return NOT_STARTED, f"could not run {argv[0]}: {e.strerror or e}"
    return p.returncode, p.stdout
