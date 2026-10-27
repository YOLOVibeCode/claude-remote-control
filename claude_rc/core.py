"""Decision logic for supervising Remote Control hosts. Pure: plain data in, plain data out.

Nothing here touches tmux, claude, files, the clock, or the network; adapters do that and
pass the results in. That keeps every rule testable in isolation.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

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
    PARKED = "PARKED"                          # stopped on purpose (policy or `claude-rc park`): never restarted


@dataclass(frozen=True)
class Entry:
    """One manifest line: the tmux session name pinned to a folder and a conversation."""
    name: str
    dir: str
    conversation: str
    flags: Tuple[str, ...] = ()
    enabled: bool = True
    tier: str = "auto"   # "keep": always running, never parked; "auto": parked when idle (policy)


TIERS = ("auto", "keep")


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
    status_since: Optional[float] = None  # statusUpdatedAt in seconds: idle since, when status is idle
    waiting_for: str = ""                 # why a "waiting" session waits (input needed, dialog open, ...)


@dataclass(frozen=True)
class History:
    """What earlier watch runs remember about one entry."""
    restarts: Tuple[float, ...] = ()
    last_state: Optional[State] = None
    idle_unlinked_runs: int = 0
    relinked_at: Optional[float] = None    # the last time /remote-control was typed in this unlinked streak
    relinks: int = 0                       # how many times it was typed in this streak
    note: Optional[str] = None  # the last "skipped" reason logged, so a blocked action is logged once per streak
    parked_at: Optional[float] = None       # stopped on purpose; never restarted while set
    parked_reason: Optional[str] = None     # idle | night | budget | manual
    last_active: Optional[float] = None     # last time the user did something there; survives the process
    throttled_pid: Optional[int] = None     # the pid `taskpolicy -b` was applied to


@dataclass(frozen=True)
class PaneInfo:
    """What tmux says about a session's only pane."""
    pid: int          # the pane's own process (claude itself for a supervised host)
    in_mode: bool     # copy mode: typed keys would go to tmux, not to claude
    dead: bool
    windows: int
    panes: int


def _after_restart(history: History, now: float) -> Optional[State]:
    """STARTING inside the grace period, RESTART_FAILED for an hour after one never came up."""
    last_restart = history.restarts[-1] if history.restarts else None
    if last_restart is not None and now - last_restart < RESTART_GRACE_S:
        return State.STARTING
    # A restart that never came up stays failed for an hour, then gets another try
    # (plan() still caps restarts per hour), so a fixed cause heals without a human.
    if history.last_state in (State.STARTING, State.RESTART_FAILED) and last_restart is not None \
            and now - last_restart < RETRY_FAILED_AFTER_S:
        return State.RESTART_FAILED
    return None


def classify(entry: Entry, has_session: bool, record: Optional[Record], history: History, now: float) -> State:
    if not entry.enabled:
        return State.DISABLED
    live = record if record is not None and record.alive else None
    if live is not None:
        if live.conversation != entry.conversation:
            return State.WRONG_CONVERSATION
        return State.OK if live.linked else State.UNLINKED
    # Parked on purpose: never DEAD (that would restart it) and never NOT_RUNNING (that would alert).
    if history.parked_at is not None:
        return State.PARKED
    after = _after_restart(history, now)
    if after is not None:
        return after
    return State.NOT_RUNNING if has_session else State.DEAD


def restart_argv(entry: Entry, claude_bin: str) -> List[str]:
    """The command a restarted host runs: same name, same conversation, same flags."""
    return [claude_bin, "--remote-control", entry.name, "--resume", entry.conversation, *entry.flags]


# --- when to keep hosts running ---------------------------------------------------------------

@dataclass(frozen=True)
class Policy:
    """How greedy the watchdog may be with this machine. Off unless config.json says otherwise."""
    enabled: bool = False
    night_start_hour: int = 0          # night = [start, end) in local hours; wraps past midnight if start > end
    night_end_hour: int = 6
    idle_park_hours: float = 8.0       # day: park a host idle this long
    max_live: int = 8                  # day and night: most hosts with a live claude (the door does not count)
    budget_min_idle_minutes: int = 60  # the budget never parks a host idle less than this
    night_idle_minutes: int = 30       # night: park a host idle this long
    throttle_at_night: bool = True     # night: hosts still running drop to background priority
    morning_revive: bool = False       # day: restart hosts parked for "night" (keep-tier always runs)


_BAD = object()


def _typed(value: object, kind: type) -> object:
    if kind is bool:
        return value if isinstance(value, bool) else _BAD
    if isinstance(value, bool):  # True is an int in Python; never accept it as a number
        return _BAD
    if kind is int:
        return value if isinstance(value, int) else _BAD
    if kind is float:
        return float(value) if isinstance(value, (int, float)) else _BAD
    if kind is str:
        return value if isinstance(value, str) else _BAD
    return _BAD


def _parse_section(raw: object, default: object, rules: Dict[str, Tuple[type, object]], label: str) -> Tuple[object, List[str]]:
    """Each bad value falls back to its own default with one warning; keys starting with _ are comments."""
    if raw is None:
        return default, []
    if not isinstance(raw, dict):
        return default, [f"{label}: expected an object; using defaults"]
    kw: Dict[str, object] = {}
    warnings: List[str] = []
    for key, value in raw.items():
        if str(key).startswith("_"):
            continue
        rule = rules.get(key)
        if rule is None:
            warnings.append(f"{label}.{key}: unknown setting, ignored")
            continue
        kind, valid = rule
        v = _typed(value, kind)  # type: ignore[arg-type]
        if v is _BAD or (valid is not None and not valid(v)):  # type: ignore[operator]
            warnings.append(f"{label}.{key}: {value!r} is not valid; using {getattr(default, key)!r}")
            continue
        kw[key] = v
    return replace(default, **kw), warnings  # type: ignore[type-var]


_POLICY_RULES: Dict[str, Tuple[type, object]] = {
    "enabled": (bool, None),
    "night_start_hour": (int, lambda v: 0 <= v <= 23),
    "night_end_hour": (int, lambda v: 0 <= v <= 23),
    "idle_park_hours": (float, lambda v: 0 < v <= 720),
    "max_live": (int, lambda v: v >= 0),
    "budget_min_idle_minutes": (int, lambda v: v >= 0),
    "night_idle_minutes": (int, lambda v: v > 0),
    "throttle_at_night": (bool, None),
    "morning_revive": (bool, None),
}


def parse_policy(raw: object) -> Tuple[Policy, List[str]]:
    policy, warnings = _parse_section(raw, Policy(), _POLICY_RULES, "policy")
    return policy, warnings  # type: ignore[return-value]


def is_night(p: Policy, hour: int) -> bool:
    if p.night_start_hour == p.night_end_hour:
        return False
    if p.night_start_hour < p.night_end_hour:
        return p.night_start_hour <= hour < p.night_end_hour
    return hour >= p.night_start_hour or hour < p.night_end_hour


# --- the door: one always-on Remote Control server the phone can always reach -----------------

DOOR_SESSION = "remote"   # the tmux session `ccserve` uses
DOOR_KEY = "@door"        # its key in conditions and histories; no tmux session name can start with @


@dataclass(frozen=True)
class DoorConfig:
    enabled: bool = False
    session: str = DOOR_SESSION
    name: str = ""                        # Remote Control environment name; "" = this Mac's short hostname
    dir: str = "~/Dev"
    spawn: str = "same-dir"               # same-dir | worktree | session
    capacity: int = 2                     # sessions the phone can start at once (not used with spawn=session)
    permission_mode: str = "acceptEdits"  # edits go through; commands ask on the phone


def _word(v: str) -> bool:
    import re
    return bool(re.fullmatch(r"[A-Za-z0-9_-]+", v))


_DOOR_RULES: Dict[str, Tuple[type, object]] = {
    "enabled": (bool, None),
    "session": (str, _word),
    "name": (str, lambda v: v == "" or _word(v)),
    "dir": (str, lambda v: v.strip() != ""),
    "spawn": (str, lambda v: v in ("same-dir", "worktree", "session")),
    "capacity": (int, lambda v: 1 <= v <= 32),
    "permission_mode": (str, lambda v: v.isalpha()),
}


def parse_door(raw: object) -> Tuple[DoorConfig, List[str]]:
    door, warnings = _parse_section(raw, DoorConfig(), _DOOR_RULES, "door")
    return door, warnings  # type: ignore[return-value]


def door_argv(d: DoorConfig, claude_bin: str, name: str) -> List[str]:
    argv = [claude_bin, "remote-control", "--name", name, "--spawn", d.spawn]
    if d.spawn != "session":
        argv += ["--capacity", str(d.capacity)]
    return argv + ["--permission-mode", d.permission_mode]


def is_door_command(cmd: str) -> bool:
    """True for `claude remote-control ...` (the server), not for `claude --remote-control NAME` (a host)."""
    parts = cmd.split()
    return len(parts) > 1 and parts[1] == "remote-control"


@dataclass(frozen=True)
class DoorObservation:
    exists: bool                  # its tmux session exists
    server_pid: Optional[int]     # the pane's process, when it is the Remote Control server
    trusted: bool = True


@dataclass(frozen=True)
class StartDoor:
    session: str
    dir: str
    argv: Tuple[str, ...]


def plan_door(d: DoorConfig, o: DoorObservation, h: History, now: float, argv: List[str],
              background: bool) -> Tuple[Tuple[object, ...], Optional[str], History]:
    """Keep the door up: start it when its session is gone (3 an hour), never touch a session that
    runs something else, and drop it to background priority at night."""
    if not d.enabled:
        return (), None, History()
    actions: List[object] = []
    recent = tuple(t for t in h.restarts if now - t < 3600)
    last, throttled = h.last_state, h.throttled_pid
    if o.server_pid is not None:
        condition, last = "OK", State.OK
        if throttled is not None and throttled != o.server_pid:
            throttled = None
        if background and throttled is None:
            actions.append(Throttle(DOOR_KEY, o.server_pid, True))
            throttled = o.server_pid
        elif not background and throttled is not None:
            actions.append(Throttle(DOOR_KEY, o.server_pid, False))
            throttled = None
    elif o.exists:
        condition, throttled = "NOT_RUNNING", None
    else:
        throttled = None
        after = _after_restart(h, now)
        if after is not None:
            condition = after.value
        elif not o.trusted:
            condition = "UNTRUSTED"
        elif len(recent) < MAX_RESTARTS_PER_HOUR:
            actions.append(StartDoor(d.session, d.dir, tuple(argv)))
            recent = recent + (now,)
            condition, last = "RESTARTED", State.STARTING
        else:
            condition = "GAVE_UP"
    return tuple(actions), condition, replace(h, restarts=recent, last_state=last, throttled_pid=throttled)


# --- what a watch run does -------------------------------------------------------------

MAX_RESTARTS_PER_HOUR = 3
RELINK_AFTER_IDLE_RUNS = 2
# An outage (a router's nightly pause, an ISP drop) can outlast the first relink, and Remote Control
# never reconnects by itself, so a stuck link is retyped hourly. Each try resets the idle clock, so stop.
RELINK_RETRY_S = 3600
MAX_RELINKS = 6


def relink_not_typed(h: History) -> History:
    """A planned relink was not typed (a draft, copy mode, tmux failed): undo it so the next run tries again."""
    if h.relinks <= 1:
        return replace(h, relinked_at=None, relinks=0)
    return replace(h, relinked_at=(h.relinked_at or 0) - RELINK_RETRY_S, relinks=h.relinks - 1)


# Conditions a person has to look at; everything else is fine or being handled.
NEEDS_YOU = frozenset({"NOT_RUNNING", "WRONG_CONVERSATION", "RESTART_FAILED", "GAVE_UP", "UNLINKED_STUCK", "UNTRUSTED"})


@dataclass(frozen=True)
class Observation:
    entry: Entry
    state: State
    status: Optional[str]  # registry status of the live process (idle | busy | shell), if any
    trusted: bool = True   # Claude Code trusts entry.dir; if not, a restart stops at the trust prompt
    pid: Optional[int] = None             # the live claude process, if any
    status_since: Optional[float] = None  # when its status last changed (idle since, when idle)


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
class Stop:
    """Park a live host: type /exit when it is safe, fall back to closing its tmux session.
    `observed` and `fallback` restore its state and condition if the stop is blocked."""
    name: str
    pid: int
    reason: str                     # idle | night | budget | manual
    status_since: Optional[float]   # if the session's status moved past this, it was used: do not stop
    observed: State
    fallback: str


@dataclass(frozen=True)
class Throttle:
    """Move a running host to background priority (night) or back (day). macOS taskpolicy."""
    name: str
    pid: int
    on: bool


ACTIVE = ("busy", "waiting")


@dataclass(frozen=True)
class Decision:
    actions: Tuple[object, ...]
    conditions: Dict[str, str]
    histories: Dict[str, History]


def _last_active(observations: List[Observation], histories: Dict[str, History], now: float) -> Dict[str, Optional[float]]:
    """When the user last did something in each host: now if it is working, else when it went idle.
    Kept in History so a host that is down still ranks by when it was last used."""
    out: Dict[str, Optional[float]] = {}
    for o in observations:
        la = histories.get(o.entry.name, History()).last_active
        if o.state in (State.OK, State.UNLINKED):
            if o.status in ACTIVE:
                la = now
            elif o.status == "idle" and o.status_since is not None:
                la = o.status_since if la is None else max(la, o.status_since)
        out[o.entry.name] = la
    return out


def park_reasons(observations: List[Observation], histories: Dict[str, History], last_active: Dict[str, Optional[float]],
                 now: float, policy: Policy, night: bool) -> Dict[str, Optional[str]]:
    """Which hosts should not be running, and why ("idle", "night", "budget"); None means it may run.

    One rule for parking and for restarting, day and night: keep-tier, busy and waiting hosts always
    run; the rest run if they were used recently (within idle_park_hours by day, night_idle_minutes at
    night), most recent first, up to max_live; the budget never cuts a host used in the last hour."""
    out: Dict[str, Optional[str]] = {o.entry.name: None for o in observations}
    if not policy.enabled:
        return out
    limit = policy.night_idle_minutes * 60 if night else policy.idle_park_hours * 3600
    floor = policy.budget_min_idle_minutes * 60
    forced = 0
    ranked: List[Tuple[float, str, bool]] = []
    for o in observations:
        name = o.entry.name
        h = histories.get(name, History())
        live = o.state in (State.OK, State.UNLINKED)
        revivable = (o.state is State.PARKED and policy.morning_revive and not night and h.parked_reason == "night")
        if o.state in (State.STARTING, State.WRONG_CONVERSATION):
            forced += 1  # a claude is running there: it counts against the budget
            continue
        if not (live or o.state is State.DEAD or revivable):
            continue     # DISABLED, NOT_RUNNING, RESTART_FAILED, PARKED: not this rule's business
        if o.entry.tier == "keep":
            if live or o.trusted:
                forced += 1
            continue
        if live and (o.status != "idle" or o.status_since is None):
            forced += 1  # working, waiting on you, or unknown: never parked
            continue
        la = last_active.get(name)
        age = None if la is None else max(0.0, now - la)
        if age is None or age >= limit:
            out[name] = "night" if night else "idle"
            continue
        if not live and not o.trusted:
            continue     # used recently but cannot start: reported UNTRUSTED as before
        ranked.append((age, name, live))
    slots = max(0, policy.max_live - forced)
    for i, (age, name, live) in enumerate(sorted(ranked)):
        if i < slots or (live and age < floor):
            continue
        out[name] = "budget"
    return out


def plan(observations: List[Observation], histories: Dict[str, History], now: float, claude_bin: str,
         policy: Optional[Policy] = None, night: bool = False) -> Decision:
    policy = policy or Policy()
    actions: List[object] = []
    conditions: Dict[str, str] = {}
    out: Dict[str, History] = {}
    last_active = _last_active(observations, histories, now)
    reasons = park_reasons(observations, histories, last_active, now, policy, night)
    night_idle_s = policy.night_idle_minutes * 60
    for o in observations:
        name = o.entry.name
        h = histories.get(name, History())
        recent = tuple(t for t in h.restarts if now - t < 3600)
        idle_runs, relinked_at, relinks, last = h.idle_unlinked_runs, h.relinked_at, h.relinks, o.state
        parked_at, parked_reason, throttled = h.parked_at, h.parked_reason, h.throttled_pid
        condition = o.state.value
        why = reasons.get(name)
        live = o.state in (State.OK, State.UNLINKED)
        emitted = False
        if live:
            parked_at, parked_reason = None, None   # running on its conversation again: not parked
        else:
            throttled = None                        # no process: nothing is throttled

        if live and why is not None and o.pid is not None:
            fallback = "UNLINKED_STUCK" if (o.state is State.UNLINKED and relinked_at is not None) else o.state.value
            actions.append(Stop(name, o.pid, why, o.status_since, o.state, fallback))
            emitted = True
            if night and policy.throttle_at_night and throttled != o.pid:
                actions.append(Throttle(name, o.pid, True))  # if the stop is blocked, it still sleeps lightly
                throttled = o.pid
            condition, last = "PARKED", State.PARKED
            parked_at, parked_reason = now, why
            idle_runs, relinked_at, relinks = 0, None, 0
        elif o.state is State.PARKED:
            condition = "PARKED"
            revive = (why is None and policy.enabled and policy.morning_revive and not night
                      and h.parked_reason == "night" and o.trusted)
            if revive and len(recent) < MAX_RESTARTS_PER_HOUR:
                actions.append(Restart(name, o.entry.dir, tuple(restart_argv(o.entry, claude_bin))))
                emitted = True
                recent = recent + (now,)
                condition, last = "RESTARTED", State.STARTING
                parked_at, parked_reason = None, None
        elif o.state is State.DEAD and why is not None:
            condition, last = "PARKED", State.PARKED
            parked_at, parked_reason = now, why
        elif o.state is State.DEAD and not o.trusted:
            condition = "UNTRUSTED"
        elif o.state is State.DEAD:
            if len(recent) < MAX_RESTARTS_PER_HOUR:
                actions.append(Restart(name, o.entry.dir, tuple(restart_argv(o.entry, claude_bin))))
                emitted = True
                recent = recent + (now,)
                condition, last = "RESTARTED", State.STARTING
            else:
                condition = "GAVE_UP"
        elif o.state is State.UNLINKED:
            idle_runs = idle_runs + 1 if o.status == "idle" else 0
            if relinked_at is not None:
                condition = "UNLINKED_STUCK"    # stays stuck while retrying: one alert, not one per try
            due = relinked_at is None or (relinks < MAX_RELINKS and now - relinked_at >= RELINK_RETRY_S)
            if due and idle_runs >= RELINK_AFTER_IDLE_RUNS:
                actions.append(Relink(name))
                emitted = True
                relinked_at, relinks = now, relinks + 1
        if o.state is not State.UNLINKED:
            idle_runs, relinked_at, relinks = 0, None, 0

        if live and why is None and o.pid is not None:
            want_bg = (policy.enabled and night and policy.throttle_at_night and o.status == "idle"
                       and o.status_since is not None and now - o.status_since >= night_idle_s)
            if throttled is not None and throttled != o.pid:
                throttled = None                    # a new process: the old one's setting went with it
            if want_bg and throttled is None:
                actions.append(Throttle(name, o.pid, True))
                throttled = o.pid
            elif not want_bg and throttled is not None:
                actions.append(Throttle(name, o.pid, False))
                throttled = None

        conditions[name] = condition
        out[name] = replace(h, restarts=recent, last_state=last, idle_unlinked_runs=idle_runs, relinked_at=relinked_at,
                            relinks=relinks, note=h.note if emitted else None, parked_at=parked_at,
                            parked_reason=parked_reason, last_active=last_active.get(name), throttled_pid=throttled)
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


DOOR_REASONS = {"NOT_RUNNING": "its tmux session runs something else"}


def _display(name: str) -> str:
    return "door" if name == DOOR_KEY else name


def _reason(name: str, condition: str) -> str:
    return DOOR_REASONS.get(condition, REASONS[condition]) if name == DOOR_KEY else REASONS[condition]


def _new_trouble(prev: Dict[str, str], now: Dict[str, str]) -> List[str]:
    return [f"{_display(n)} ({_reason(n, c)})" for n, c in now.items() if c in NEEDS_YOU and prev.get(n) != c]


def diff_changes(prev: Dict[str, str], now: Dict[str, str]) -> Optional[str]:
    """One line per run for watch.log: restarts, new problems, recoveries. None when nothing changed."""
    restarted = [_display(n) for n, c in now.items() if c == "RESTARTED" and prev.get(n) != "RESTARTED"]
    back = [_display(n) for n, c in now.items()
            if c == "OK" and (prev.get(n) in NEEDS_YOU or prev.get(n) in ("RESTARTED", "STARTING"))]
    groups = [("restarted", restarted), ("needs you", _new_trouble(prev, now)), ("back", back)]
    if not any(items for _, items in groups):
        return None
    return _fit("claude-rc: ", groups, SMS_LIMIT)


def diff_alerts(prev: Dict[str, str], now: Dict[str, str]) -> Optional[str]:
    """The text for the phone: only new problems that need you. Restarts that worked and recoveries
    are in watch.log (diff_changes) and the daily report. None when nothing needs you."""
    trouble = _new_trouble(prev, now)
    return _fit("claude-rc: ", [("needs you", trouble)], SMS_LIMIT) if trouble else None


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


# --- is the phone still hearing from us? ------------------------------------------------------

ESCALATE_AFTER = 3  # consecutive failed runs before a channel is escalated (once)


@dataclass(frozen=True)
class ChannelHealth:
    """Delivery record for one channel (alerts, heartbeat, report)."""
    failures: int = 0                # consecutive failed runs
    since: Optional[float] = None    # when the current failure streak started
    last_error: str = ""
    escalated: bool = False


def track_delivery(h: ChannelHealth, error: Optional[str], now: float,
                   after: int = ESCALATE_AFTER) -> Tuple[ChannelHealth, Optional[str]]:
    """Update a channel's health after one attempt. The event is "failing" (first failure),
    "escalate" (the streak reached `after`, reported once), "recovered", or None."""
    if error is None:
        return (ChannelHealth(), "recovered") if h.failures else (h, None)
    new = ChannelHealth(h.failures + 1, h.since if h.since is not None else now, error[:200], h.escalated)
    if new.failures >= after and not h.escalated:
        return replace(new, escalated=True), "escalate"
    return new, ("failing" if new.failures == 1 else None)


def coalesce_queue(queued: List[str], limit: int) -> Tuple[List[str], List[str]]:
    """Keep the newest alerts within `limit`, folding the rest into one summary line.
    Returns (kept, dropped) so the caller can log what was folded."""
    if len(queued) <= limit:
        return list(queued), []
    keep = max(0, limit - 1)
    dropped = list(queued[:len(queued) - keep])
    return [f"claude-rc: {len(dropped)} earlier alerts not delivered (see watch.log)"] + list(queued[len(queued) - keep:]), dropped


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


def format_report(rows: List[ReportRow], problems: Sequence[str] = ()) -> Tuple[str, str]:
    """Daily all-clear email: (subject, html). `problems` are things outside the sessions that
    need a look, such as an alert channel that keeps failing."""
    from html import escape

    ok = sum(1 for r in rows if r.condition == "OK")
    need = sum(1 for r in rows if r.condition in NEEDS_YOU)
    parked = sum(1 for r in rows if r.condition == "PARKED")
    active = sum(1 for r in rows if r.condition not in ("PARKED", "DISABLED"))
    subject = (f"claude-rc: {ok}/{active if parked else len(rows)} OK" + (f", {need} need you" if need else "")
               + (f", {parked} parked" if parked else "") + (", alerts failing" if problems else ""))
    attention = ("<p><b>Needs attention</b></p><ul>" + "".join(f"<li>{escape(p)}</li>" for p in problems) + "</ul>"
                 if problems else "")
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
        f"{attention}"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>session</th><th>state</th><th>conversation</th><th>linked</th><th>CLI</th><th>restarts 24h</th><th>folder</th></tr>"
        f"{cells}</table>"
    )
    return subject, html


# --- seeding the manifest ----------------------------------------------------------------

def adopt(entries: List[Entry], records: List[Record], default_flags: Tuple[str, ...],
          exclude: frozenset = frozenset({DOOR_SESSION})) -> Tuple[List[Entry], List[str]]:
    """Add live tmux hosts the manifest does not know yet. Never rewrites an existing entry;
    notes when a host runs a different conversation than the one pinned. Sessions the door
    spawns inherit its tmux session name; they are the door's, never adopted."""
    by_name = {e.name: e for e in entries}
    out = list(entries)
    notes: List[str] = []
    for r in records:
        if not r.alive or not r.tmux_session or r.tmux_session in exclude:
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


# --- is it safe to type into a session? ------------------------------------------------------

_DIM = "\x1b[2m"
_PROMPT = "❯"  # ❯


def input_is_empty(pane_ansi: str) -> bool:
    """True only when Claude Code's input box is empty (a dim suggestion counts as empty).

    `pane_ansi` is `tmux capture-pane -p -e`. The input box is the last line that starts with
    ❯ followed by a non-breaking space; text after it that is not dimmed is a draft. A menu
    cursor (`❯ No, exit`) or anything unrecognised is treated as not empty: never type blind.
    """
    import re

    lines = [l for l in pane_ansi.split("\n") if _PROMPT in l]
    if not lines:
        return False
    line = lines[-1]
    after = line.split(_PROMPT, 1)[1]
    if not after.startswith("\xa0"):
        return False  # a regular space: a selection menu, not the input box
    after = after[1:]
    # Everything from the first dim marker on is a suggestion/hint, not typed text.
    typed = after.split(_DIM, 1)[0]
    typed = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", typed)
    return typed.strip(" \xa0\t") == ""


def stop_blocker(fresh: Optional[Record], pane: Optional[PaneInfo], pane_ansi: str, pid: int,
                 status_since: Optional[float]) -> Optional[str]:
    """Why typing /exit into this host is not safe right now, or None. "gone" means it already
    exited, which counts as parked. Checked again right before typing: the phone may have just
    sent it something."""
    if fresh is None or not fresh.alive or fresh.pid != pid:
        return "gone"
    if fresh.status != "idle":
        return f"became {fresh.status or 'unknown'}"
    if status_since is not None and fresh.status_since is not None and fresh.status_since > status_since:
        return "became active"
    if pane is None:
        return "no pane"
    if pane.windows > 1 or pane.panes > 1:
        return "session has other windows or panes"
    if pane.in_mode:
        return "copy mode"
    if pane.pid != pid:
        return "claude is not the pane's own process"
    if not input_is_empty(pane_ansi):
        return "text in its input box"
    return None
