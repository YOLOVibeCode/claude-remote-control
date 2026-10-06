"""Decision logic for supervising Remote Control hosts. Pure: plain data in, plain data out.

Nothing here touches tmux, claude, files, the clock, or the network; adapters do that and
pass the results in. That keeps every rule testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

# A restarted host gets this long to register on its conversation before it counts as failed.
RESTART_GRACE_S = 90


class State(str, Enum):
    OK = "OK"
    DEAD = "DEAD"                              # no tmux session: safe to restart
    NOT_RUNNING = "NOT_RUNNING"                # tmux session without a live claude: report only
    WRONG_CONVERSATION = "WRONG_CONVERSATION"  # alive on another conversation: report only
    UNLINKED = "UNLINKED"                      # right conversation, Remote Control off
    STARTING = "STARTING"                      # inside the grace period after a restart
    RESTART_FAILED = "RESTART_FAILED"          # restarted, never came up on its conversation
    DISABLED = "DISABLED"                      # `enabled: false` in the manifest


@dataclass(frozen=True)
class Entry:
    """One manifest line: the tmux session name pinned to a folder and a conversation."""
    name: str
    dir: str
    conversation: str
    flags: Tuple[str, ...] = ()
    enabled: bool = True


@dataclass(frozen=True)
class Record:
    """What ~/.claude/sessions/<pid>.json says about one running claude process."""
    pid: int
    conversation: str
    cwd: str
    tmux_session: Optional[str]
    status: str          # idle | busy | shell
    linked: bool         # bridgeSessionId present
    version: str
    alive: bool


@dataclass(frozen=True)
class History:
    """What earlier watch runs remember about one entry."""
    restarts: Tuple[float, ...] = ()
    last_state: Optional[State] = None
    idle_unlinked_runs: int = 0
    relinked_at: Optional[float] = None


def classify(entry: Entry, has_session: bool, record: Optional[Record], history: History, now: float) -> State:
    if not entry.enabled:
        return State.DISABLED
    live = record if record is not None and record.alive else None
    if live is not None:
        if live.conversation != entry.conversation:
            return State.WRONG_CONVERSATION
        return State.OK if live.linked else State.UNLINKED
    last_restart = history.restarts[-1] if history.restarts else None
    if last_restart is not None and now - last_restart < RESTART_GRACE_S:
        return State.STARTING
    if history.last_state in (State.STARTING, State.RESTART_FAILED):
        return State.RESTART_FAILED
    return State.NOT_RUNNING if has_session else State.DEAD


def restart_argv(entry: Entry, claude_bin: str) -> List[str]:
    """The command a restarted host runs: same name, same conversation, same flags."""
    return [claude_bin, "--remote-control", entry.name, "--resume", entry.conversation, *entry.flags]
