"""Decision logic for supervising Remote Control hosts. Pure: plain data in, plain data out.

Nothing here touches tmux, claude, files, the clock, or the network; adapters do that and
pass the results in. That keeps every rule testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Tuple

# A restarted host gets this long to register on its conversation before it counts as failed.
RESTART_GRACE_S = 90
# A failed restart is tried again after this long.
RETRY_FAILED_AFTER_S = 3600


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
    # A restart that never came up stays failed for an hour, then gets another try
    # (plan() still caps restarts per hour), so a fixed cause heals without a human.
    if history.last_state in (State.STARTING, State.RESTART_FAILED) and last_restart is not None \
            and now - last_restart < RETRY_FAILED_AFTER_S:
        return State.RESTART_FAILED
    return State.NOT_RUNNING if has_session else State.DEAD


def restart_argv(entry: Entry, claude_bin: str) -> List[str]:
    """The command a restarted host runs: same name, same conversation, same flags."""
    return [claude_bin, "--remote-control", entry.name, "--resume", entry.conversation, *entry.flags]


# --- what a watch run does -------------------------------------------------------------

MAX_RESTARTS_PER_HOUR = 3
RELINK_AFTER_IDLE_RUNS = 2

# Conditions a person has to look at; everything else is fine or being handled.
NEEDS_YOU = frozenset({"NOT_RUNNING", "WRONG_CONVERSATION", "RESTART_FAILED", "GAVE_UP", "UNLINKED_STUCK", "UNTRUSTED"})


@dataclass(frozen=True)
class Observation:
    entry: Entry
    state: State
    status: Optional[str]  # registry status of the live process (idle | busy | shell), if any
    trusted: bool = True   # Claude Code trusts entry.dir; if not, a restart stops at the trust prompt


@dataclass(frozen=True)
class Restart:
    name: str
    dir: str
    argv: Tuple[str, ...]


@dataclass(frozen=True)
class Relink:
    """Type /remote-control into an idle host whose link dropped."""
    name: str


@dataclass(frozen=True)
class Decision:
    actions: Tuple[object, ...]
    conditions: Dict[str, str]
    histories: Dict[str, History]


def plan(observations: List[Observation], histories: Dict[str, History], now: float, claude_bin: str) -> Decision:
    actions: List[object] = []
    conditions: Dict[str, str] = {}
    out: Dict[str, History] = {}
    for o in observations:
        name = o.entry.name
        h = histories.get(name, History())
        recent = tuple(t for t in h.restarts if now - t < 3600)
        idle_runs, relinked_at, last = h.idle_unlinked_runs, h.relinked_at, o.state
        condition = o.state.value

        if o.state is State.DEAD and not o.trusted:
            condition = "UNTRUSTED"
        elif o.state is State.DEAD:
            if len(recent) < MAX_RESTARTS_PER_HOUR:
                actions.append(Restart(name, o.entry.dir, tuple(restart_argv(o.entry, claude_bin))))
                recent = recent + (now,)
                condition, last = "RESTARTED", State.STARTING
            else:
                condition = "GAVE_UP"
        elif o.state is State.UNLINKED:
            idle_runs = idle_runs + 1 if o.status == "idle" else 0
            if relinked_at is not None:
                condition = "UNLINKED_STUCK"
            elif idle_runs >= RELINK_AFTER_IDLE_RUNS:
                actions.append(Relink(name))
                relinked_at = now
        if o.state is not State.UNLINKED:
            idle_runs, relinked_at = 0, None

        conditions[name] = condition
        out[name] = History(restarts=recent, last_state=last, idle_unlinked_runs=idle_runs, relinked_at=relinked_at)
    return Decision(tuple(actions), conditions, out)


# --- what reaches the phone and the inbox --------------------------------------------

SMS_LIMIT = 300

REASONS = {
    "NOT_RUNNING": "claude not running in its tmux session",
    "WRONG_CONVERSATION": "wrong conversation",
    "RESTART_FAILED": "restart failed",
    "GAVE_UP": "gave up after 3 restarts this hour",
    "UNLINKED_STUCK": "Remote Control still off after relink",
    "UNTRUSTED": "folder not trusted: open claude there once and accept",
}


def diff_alerts(prev: Dict[str, str], now: Dict[str, str]) -> Optional[str]:
    """One message per change: restarts, new problems, recoveries. None when nothing changed."""
    restarted = [n for n, c in now.items() if c == "RESTARTED" and prev.get(n) != "RESTARTED"]
    trouble = [f"{n} ({REASONS[c]})" for n, c in now.items() if c in NEEDS_YOU and prev.get(n) != c]
    back = [n for n, c in now.items() if c == "OK" and (prev.get(n) in NEEDS_YOU or prev.get(n) in ("RESTARTED", "STARTING"))]
    groups = [("restarted", restarted), ("needs you", trouble), ("back", back)]
    if not any(items for _, items in groups):
        return None
    return _fit("claude-rc: ", groups, SMS_LIMIT)


def _fit(prefix: str, groups: List[Tuple[str, List[str]]], limit: int) -> str:
    total = sum(len(items) for _, items in groups)
    shown: List[Tuple[str, List[str]]] = []
    used = 0

    def render(gs: List[Tuple[str, List[str]]], left: int) -> str:
        body = "; ".join(f"{label} {', '.join(items)}" for label, items in gs if items)
        return prefix + body + (f" +{left} more" if left else "")

    for label, items in groups:
        kept: List[str] = []
        for item in items:
            trial = shown + [(label, kept + [item])]
            if len(render(trial, total - used - 1)) > limit:
                return render(shown + [(label, kept)], total - used)
            kept.append(item)
            used += 1
        shown.append((label, kept))
    return render(shown, 0)


@dataclass(frozen=True)
class ReportRow:
    name: str
    condition: str
    conversation: str
    linked: bool
    version: str
    restarts_24h: int
    dir: str
    trusted: bool = True


def format_report(rows: List[ReportRow]) -> Tuple[str, str]:
    """Daily all-clear email: (subject, html)."""
    from html import escape

    ok = sum(1 for r in rows if r.condition == "OK")
    need = sum(1 for r in rows if r.condition in NEEDS_YOU)
    subject = f"claude-rc: {ok}/{len(rows)} OK" + (f", {need} need you" if need else "")
    cells = "".join(
        "<tr>"
        f"<td>{escape(r.name)}</td><td>{escape(r.condition)}</td><td><code>{escape(r.conversation[:8])}</code></td>"
        f"<td>{'yes' if r.linked else 'no'}</td><td>{escape(r.version)}</td><td>{r.restarts_24h}</td>"
        f"<td>{escape(r.dir)}{'' if r.trusted else ' <b>(not trusted: a restart would stop at the trust prompt)</b>'}</td>"
        "</tr>"
        for r in sorted(rows, key=lambda r: (r.condition not in NEEDS_YOU, r.condition != "OK", r.name))
    )
    html = (
        f"<p>{escape(subject)}</p>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>session</th><th>state</th><th>conversation</th><th>linked</th><th>CLI</th><th>restarts 24h</th><th>folder</th></tr>"
        f"{cells}</table>"
    )
    return subject, html


# --- seeding the manifest ----------------------------------------------------------------

def adopt(entries: List[Entry], records: List[Record], default_flags: Tuple[str, ...]) -> Tuple[List[Entry], List[str]]:
    """Add live tmux hosts the manifest does not know yet. Never rewrites an existing entry;
    notes when a host runs a different conversation than the one pinned."""
    by_name = {e.name: e for e in entries}
    out = list(entries)
    notes: List[str] = []
    for r in records:
        if not r.alive or not r.tmux_session:
            continue
        known = by_name.get(r.tmux_session)
        if known is None:
            e = Entry(r.tmux_session, r.cwd, r.conversation, tuple(default_flags))
            out.append(e)
            by_name[e.name] = e
            notes.append(f"added {e.name}")
        elif known.conversation != r.conversation:
            notes.append(f"{known.name} runs {r.conversation[:8]}, manifest pins {known.conversation[:8]}")
    return out, notes
