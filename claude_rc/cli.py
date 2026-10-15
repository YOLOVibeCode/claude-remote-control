"""claude-rc: pin, supervise, and report the Remote Control hosts running in tmux.

    claude-rc adopt [--quiet]          record live tmux hosts in the manifest (never overwrites)
    claude-rc pin NAME DIR UUID -- ARGS   record a new session (called by the claude() wrapper)
    claude-rc has NAME                 exit 0 if NAME is pinned
    claude-rc keep|auto NAME...        keep: always running, never parked; auto: parked when idle
    claude-rc up [NAME...]             start pinned hosts that have no tmux session (parked ones only if named)
    claude-rc park NAME...             stop a host on purpose (/exit); it stays parked
    claude-rc unpark NAME...|--all     bring parked hosts back on the same conversation
    claude-rc check [--json]           state of every pinned host; exit 1 if any needs attention
    claude-rc plan [--at HOUR] [--json] what a watch run would do with the policy on; changes nothing
    claude-rc watch                    check, restart/relink/park what is safe, alert on changes
    claude-rc report                   email the daily all-clear
    claude-rc notify-test alerts|report

Files live in $CLAUDE_RC_HOME (default ~/.config/claude-rc): sessions.json (manifest),
state.json, health.json (delivery streaks), config.json (providers), secrets.env (0600), watch.log.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import socket
import sys
import time
import traceback
from dataclasses import replace
from typing import Callable, Dict, Iterator, List, Optional, Sequence, Set, TextIO, Tuple

from .core import (
    DOOR_KEY,
    ESCALATE_AFTER,
    NEEDS_YOU,
    ChannelHealth,
    DoorConfig,
    DoorObservation,
    Policy,
    RESTART_GRACE_S,
    Entry,
    History,
    Observation,
    Record,
    Relink,
    ReportRow,
    Restart,
    StartDoor,
    State,
    Stop,
    Throttle,
    adopt,
    classify,
    coalesce_queue,
    diff_alerts,
    diff_changes,
    door_argv,
    input_is_empty,
    format_report,
    is_door_command,
    is_night,
    plan,
    plan_door,
    restart_argv,
    stop_blocker,
    track_delivery,
)
from .ports import DeliveryError, EmailSender, TextSender

DEFAULT_FLAGS = ("--dangerously-skip-permissions",)   # how `cc` starts sessions
QUEUE_LIMIT = 20
LOG_MAX_BYTES = 1_000_000   # watch.log / launchd.log roll over here, keeping one old copy (.1)
POLL_S = 5
STOP_GRACE_S = 30   # a parked host gets this long to exit after /exit before its tmux session is closed

def rotate(path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Move a log past the cap to <path>.1 (replacing the previous one)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s >= 86400:
        return f"{s // 86400}d{(s % 86400) // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 60}m"


# claude flags that take a value: kept with their value when recording a session's flags.
VALUE_FLAGS = {"--model", "--permission-mode", "--add-dir", "--agent", "--settings", "--mcp-config",
               "--fallback-model", "--append-system-prompt", "--allowedTools", "--disallowedTools", "--effort"}
# Flags that identify a conversation or its remote name: the manifest records those separately.
IDENTITY_FLAGS = {"--remote-control", "--rc", "--session-id", "--resume", "-r", "--name", "-n"}

Channel = Callable[[], Optional[Tuple[object, str]]]


def flags_to_record(args: Sequence[str]) -> Tuple[str, ...]:
    """The flags a restart should reuse: options and their values; no prompts, no identity flags."""
    out: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        takes_value = nxt is not None and not nxt.startswith("-")
        if a in IDENTITY_FLAGS:
            i += 2 if takes_value else 1
            continue
        if a.startswith("-"):
            out.append(a)
            if a in VALUE_FLAGS and takes_value:
                out.append(nxt)
                i += 1
        i += 1
    return tuple(out)


class App:
    def __init__(self, home: str, registry, mux, manifest, state, clock, claude_bin: str,
                 text: Channel, mail: Channel, out: TextIO = sys.stdout, sleep: Callable[[float], None] = time.sleep,
                 trust=None, heartbeat: Optional[Callable[[], object]] = None, hostname: str = "",
                 health=None, local=None, policy: Optional[Policy] = None, qos=None,
                 local_hour: Optional[Callable[[float], int]] = None, config_warnings: Sequence[str] = (),
                 door: Optional[DoorConfig] = None, procs=None, door_name: str = ""):
        from .adapters.files import JsonHealthStore
        from .adapters.qos import NoThrottle
        from .notify.local import NoNotifier

        self.home, self.registry, self.mux, self.manifest, self.state = home, registry, mux, manifest, state
        self.trust, self.heartbeat, self.hostname = trust, heartbeat, hostname
        self.clock, self.claude_bin, self.text, self.mail, self.out, self.sleep = clock, claude_bin, text, mail, out, sleep
        self.health = health if health is not None else JsonHealthStore(os.path.join(home, "health.json"))
        self.local = local if local is not None else NoNotifier()
        self._health: Dict[str, ChannelHealth] = {}
        self.qos = qos if qos is not None else NoThrottle()
        policy = policy or Policy()
        # Without a way to change priority there is nothing to throttle: never plan it.
        self.policy = replace(policy, throttle_at_night=policy.throttle_at_night and bool(getattr(self.qos, "enabled", False)))
        self.local_hour = local_hour or (lambda t: time.localtime(t).tm_hour)
        self.config_warnings = list(config_warnings)
        self.door = door or DoorConfig()
        self.door_name = door_name or (hostname or "mac").lower()
        if procs is None:
            from .adapters.procs import PsProcesses
            procs = PsProcesses()
        self.procs = procs

    # --- helpers ----------------------------------------------------------------------------
    def log(self, line: str) -> None:
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        rotate(os.path.join(self.home, "watch.log"))
        fd = os.open(os.path.join(self.home, "watch.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.clock.now()))} {line}\n")

    @contextlib.contextmanager
    def locked(self, wait_s: float = 0) -> Iterator[bool]:
        """The watch lock. Commands that change sessions wait for a running watch; watch itself never waits."""
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.home, "watch.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
        with os.fdopen(fd, "w") as f:
            waited = 0.0
            while True:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if waited >= wait_s:
                        yield False
                        return
                    self.sleep(0.5)
                    waited += 0.5
            try:
                yield True
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

    def _records_by_name(self) -> Dict[str, Record]:
        """One record per tmux session, preferring a live process over a stale file."""
        out: Dict[str, Record] = {}
        for r in self.registry.live_sessions():
            if not r.tmux_session:
                continue
            cur = out.get(r.tmux_session)
            if cur is None or (r.alive and not cur.alive):
                out[r.tmux_session] = r
        return out

    def _session_names(self) -> Tuple[bool, Optional[Set[str]]]:
        """(answered, names): every tmux session in one call. (False, None) when tmux hung;
        (True, None) when the multiplexer cannot list, so callers ask per session."""
        ls = getattr(self.mux, "list_sessions", None)
        if ls is None:
            return True, None
        names = ls()
        return names is not None, names

    def _observe(self, entries: List[Entry], histories: Dict[str, History],
                 names: Optional[Set[str]] = None) -> List[Tuple[Observation, Optional[Record]]]:
        recs = self._records_by_name()
        now = self.clock.now()
        if names is None:
            names = self._session_names()[1]
        rows = []
        for e in entries:
            r = recs.get(e.name)
            has = (e.name in names) if names is not None else self.mux.has_session(e.name)
            state = classify(e, has, r, histories.get(e.name, History()), now)
            trusted = self.trust.trusted(e.dir) if self.trust is not None else True
            live = r if r is not None and r.alive else None
            rows.append((Observation(e, state, live.status if live else None, trusted,
                                     live.pid if live else None, live.status_since if live else None), r))
        return rows

    # --- parking ------------------------------------------------------------------------------
    def _revert_stop(self, s: Stop, blocker: str, conditions: Dict[str, str], histories: Dict[str, History]) -> None:
        """A stop that could not happen: the host keeps running, as it was."""
        h = histories.get(s.name, History())
        conditions[s.name] = s.fallback
        histories[s.name] = replace(h, parked_at=None, parked_reason=None, last_state=s.observed, note=blocker)
        if h.note != blocker:
            self.log(f"park skipped for {s.name}: {blocker}")

    def _run_stops(self, stops: List[Stop], conditions: Dict[str, str], histories: Dict[str, History]) -> None:
        """Type /exit into each host that is safe to stop, wait for it to exit, and close the tmux
        session only if it is still idle. Never signals a process directly."""
        if not stops:
            return
        by_pid = {r.pid: r for r in self.registry.live_sessions()}
        typed: Dict[str, Stop] = {}
        for s in stops:
            pane = self.mux.pane_info(s.name)
            text = self.mux.capture(s.name, 40, ansi=True) if pane is not None else ""
            blocker = stop_blocker(by_pid.get(s.pid), pane, text, s.pid, s.status_since)
            if blocker == "gone":
                continue  # already exited: parked
            if blocker is None:
                try:
                    self.mux.send_keys(s.name, "/exit")
                    typed[s.name] = s
                    continue
                except RuntimeError as e:
                    blocker = "tmux send-keys failed"
                    self.log(f"park {s.name}: {e}")
            self._revert_stop(s, blocker, conditions, histories)
        if not typed:
            return
        pending = {n: s.pid for n, s in typed.items()}
        for _ in range(int(STOP_GRACE_S // POLL_S) + 1):
            alive = {r.pid for r in self.registry.live_sessions() if r.alive}
            for n, pid in list(pending.items()):
                if pid not in alive:
                    del pending[n]
            if not pending:
                break
            self.sleep(POLL_S)
        for n, s in typed.items():
            if n not in pending:
                if self.mux.has_session(n):
                    self.mux.kill_session(n)  # claude exited but left the session behind
                continue
            fresh = {r.pid: r for r in self.registry.live_sessions()}.get(s.pid)
            if fresh is not None and fresh.alive and fresh.status == "idle":
                tail = self.mux.capture(n, 20).strip().splitlines()[-6:]
                self.mux.kill_session(n)
                self.log(f"park: /exit did not end {n} in {STOP_GRACE_S}s; closed its tmux session; pane: " + " | ".join(tail))
            else:
                self._revert_stop(s, "became busy", conditions, histories)

    # --- commands ---------------------------------------------------------------------------
    def adopt(self, quiet: bool = False) -> int:
        entries = self.manifest.load()
        new, notes = adopt(entries, self.registry.live_sessions(), DEFAULT_FLAGS, frozenset({self.door.session}))
        if new != entries:
            self.manifest.save(new)
        for n in notes:
            self.log(f"adopt: {n}")
            if not quiet:
                print(n, file=self.out)
        return 0

    def pin(self, name: str, directory: str, conversation: str, args: Sequence[str]) -> int:
        entries = self.manifest.load()
        if any(e.name == name for e in entries):
            print(f"{name} is already pinned; not overwritten", file=self.out)
            return 2
        entries.append(Entry(name, directory, conversation, flags_to_record(args)))
        self.manifest.save(entries)
        self.log(f"pin: {name} {conversation[:8]}")
        return 0

    def set_tier(self, names: Sequence[str], tier: str) -> int:
        """`claude-rc keep NAME` (always running, never parked) or `claude-rc auto NAME`."""
        entries = self.manifest.load()
        known = {e.name for e in entries}
        unknown = [n for n in names if n not in known]
        if unknown:
            print(f"not pinned: {', '.join(unknown)}", file=self.out)
            return 2
        self.manifest.save([replace(e, tier=tier) if e.name in names else e for e in entries])
        for n in names:
            print(f"{n}: {tier}", file=self.out)
            self.log(f"tier: {n} -> {tier}")
        return 0

    def has(self, name: str) -> int:
        return 0 if any(e.name == name for e in self.manifest.load()) else 1

    def up(self, names: Sequence[str] = (), wait_s: float = 120.0) -> int:
        """Start pinned hosts that have no tmux session. Parked hosts start only when named."""
        with self.locked(wait_s) as got:
            if not got:
                print("busy: a watch run holds the lock; try again in a minute", file=self.out)
                return 1
            conditions, histories, queued = self.state.load()
            now = self.clock.now()
            open_in = {r.conversation: r.tmux_session for r in self.registry.live_sessions()
                       if r.alive and r.tmux_session}
            skipped: List[str] = []
            for e in self.manifest.load():
                if not e.enabled or (names and e.name not in names):
                    continue
                h = histories.get(e.name, History())
                if h.parked_at is not None and not names:
                    skipped.append(e.name)
                    continue
                if self.mux.has_session(e.name):
                    if h.parked_at is not None:
                        histories[e.name] = replace(h, parked_at=None, parked_reason=None)
                    continue
                other = open_in.get(e.conversation)
                if other and other != e.name:
                    print(f"{e.name}: its conversation is already open in tmux session {other}", file=self.out)
                    continue
                try:
                    self.mux.new_session(e.name, e.dir, restart_argv(e, self.claude_bin))
                except RuntimeError as err:
                    print(f"{e.name}: {err}", file=self.out)
                    continue
                histories[e.name] = replace(h, restarts=h.restarts + (now,), last_state=State.STARTING,
                                            idle_unlinked_runs=0, relinked_at=None, parked_at=None,
                                            parked_reason=None, note=None)
                print(f"started {e.name} on {e.conversation[:8]}", file=self.out)
                self.log(f"up: started {e.name}")
            if skipped:
                more = "..." if len(skipped) > 5 else ""
                print(f"skipped {len(skipped)} parked ({', '.join(skipped[:5])}{more}); "
                      f"start one with: claude-rc unpark NAME", file=self.out)
            self.state.save(conditions, histories, queued)
        return 0

    def unpark(self, names: Sequence[str] = (), all_: bool = False) -> int:
        """Bring parked hosts back on the same conversation."""
        if all_:
            _, histories, _ = self.state.load()
            pinned = {e.name for e in self.manifest.load()}
            names = [n for n, h in histories.items() if h.parked_at is not None and n in pinned]
            if not names:
                print("nothing parked", file=self.out)
                return 0
        if not names:
            print("name a session, or use --all", file=self.out)
            return 2
        return self.up(names)

    def park(self, names: Sequence[str]) -> int:
        """Stop hosts on purpose; they stay parked until `claude-rc unpark`."""
        entries = {e.name: e for e in self.manifest.load()}
        unknown = [n for n in names if n not in entries]
        if unknown:
            print(f"not pinned: {', '.join(unknown)}", file=self.out)
            return 2
        with self.locked(60) as got:
            if not got:
                print("busy: a watch run holds the lock; try again in a minute", file=self.out)
                return 1
            conditions, histories, queued = self.state.load()
            now = self.clock.now()
            stops: List[Stop] = []
            for o, _ in self._observe([entries[n] for n in names], histories):
                n = o.entry.name
                h = histories.get(n, History())
                if o.state is State.PARKED:
                    print(f"{n}: already parked", file=self.out)
                    continue
                if o.state in (State.OK, State.UNLINKED) and o.pid is not None:
                    stops.append(Stop(n, o.pid, "manual", o.status_since, o.state, conditions.get(n, o.state.value)))
                elif o.state not in (State.DEAD, State.NOT_RUNNING, State.RESTART_FAILED):
                    print(f"{n}: {o.state.value}; not parked", file=self.out)
                    continue
                conditions[n] = "PARKED"
                histories[n] = replace(h, parked_at=now, parked_reason="manual", last_state=State.PARKED)
            self._run_stops(stops, conditions, histories)
            self.state.save(conditions, histories, queued)
        bad = 0
        for n in names:
            if conditions.get(n) == "PARKED":
                print(f"parked {n}", file=self.out)
                self.log(f"park: {n} (manual)")
            else:
                bad += 1
                print(f"{n}: not parked ({histories.get(n, History()).note or conditions.get(n)})", file=self.out)
        return 1 if bad else 0

    def check(self, as_json: bool = False) -> int:
        _, histories, _ = self.state.load()
        self._health = self.health.load()
        _, names = self._session_names()
        rows = self._observe(self._without_door(self.manifest.load()), histories, names)
        door_condition = self._door_plan(names, histories, self.clock.now(), False)[1]
        data = [{"name": o.entry.name, "state": o.state.value, "conversation": o.entry.conversation,
                 "linked": bool(r and r.linked), "status": o.status, "version": r.version if r else None,
                 "trusted": o.trusted, "tier": o.entry.tier,
                 "parked_reason": histories.get(o.entry.name, History()).parked_reason if o.state is State.PARKED else None}
                for o, r in rows]
        if door_condition is not None:
            door_state = "OK" if door_condition == "OK" else ("DEAD" if door_condition in ("RESTARTED", "GAVE_UP") else door_condition)
            data.append({"name": DOOR_KEY, "kind": "door", "state": door_state, "session": self.door.session,
                         "dir": self.door.dir, "permission_mode": self.door.permission_mode})
        if as_json:
            json.dump(data, self.out, indent=2)
            self.out.write("\n")
        else:
            night = self.policy.enabled and is_night(self.policy, self.local_hour(self.clock.now()))
            print(self._profile_line(night, sum(1 for o, _ in rows if o.state in (State.OK, State.UNLINKED))), file=self.out)
            if door_condition is not None:
                print(f"door: {data[-1]['state']} (tmux session {self.door.session}, {self.door.permission_mode})", file=self.out)
            for w in self._problems():
                print(f"attention: {w}", file=self.out)
            for d in data:
                if d.get("kind") == "door":
                    continue
                print(f"{d['state']:<19} {d['name']:<24} {d['conversation'][:8]}  "
                      f"{'linked' if d['linked'] else 'no-link'}  {d['status'] or '-'}"
                      f"{'  keep' if d['tier'] == 'keep' else ''}"
                      f"{'  (' + d['parked_reason'] + ')' if d['parked_reason'] else ''}"
                      f"{'' if d['trusted'] else '  (folder not trusted: restart would stop at the prompt)'}", file=self.out)
        fine = {State.OK, State.DISABLED, State.STARTING, State.PARKED}
        door_ok = door_condition in (None, "OK", "STARTING")
        return 0 if door_ok and all(o.state in fine for o, _ in rows) else 1

    def watch(self) -> int:
        with self.locked() as got:
            if not got:
                return 0
            try:
                return self._watch()
            except Exception as e:  # a crashed run saves nothing and beats nothing: say so
                self.log(f"watch crashed: {type(e).__name__}: {e}")
                traceback.print_exc(file=sys.stderr)
                return 1

    def _track(self, channel: str, error: Optional[str], after: int = ESCALATE_AFTER) -> Optional[str]:
        """Record one delivery attempt; escalate a streak once, note a recovery once."""
        cur = self._health.get(channel, ChannelHealth())
        new, event = track_delivery(cur, error, self.clock.now(), after)
        self._health[channel] = new
        if event == "escalate":
            since = time.strftime("%Y-%m-%d %H:%M", time.localtime(new.since or self.clock.now()))
            msg = f"{channel} failing since {since} ({new.failures} runs): {new.last_error}"
            shown = self.local.notify("claude-rc", msg)
            self.log(f"escalated: {msg}" + ("" if shown else " (no local notification available)"))
        elif event == "recovered":
            verb = "answering" if channel == "tmux" else "delivered"
            self.log(f"{channel} {verb} again after {cur.failures} failed runs")
        return event

    def _problems(self) -> List[str]:
        out = []
        for channel, h in sorted(self._health.items()):
            if h.failures:
                since = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.since or self.clock.now()))
                verb = "not answering" if channel == "tmux" else "not delivered"
                out.append(f"{channel} {verb} since {since} ({h.failures} runs): {h.last_error}")
        return out + [f"config: {w}" for w in self.config_warnings]

    def _relink_blocker(self, name: str) -> Optional[str]:
        """Why typing /remote-control into this pane is not safe right now, or None."""
        pane_info = getattr(self.mux, "pane_info", None)
        pane = pane_info(name) if pane_info else None
        if pane is not None and pane.in_mode:
            return "pane in copy mode"          # keys would go to tmux's copy mode, not to claude
        if not input_is_empty(self.mux.capture(name, 40, ansi=True)):
            return "text in its input box"      # Enter would submit the user's draft
        return None

    def _watch(self) -> int:
        # launchd reopens launchd.log at every run, so rolling it between runs is safe.
        rotate(os.path.join(self.home, "launchd.log"))
        prev, histories, queued = self.state.load()
        self._health = self.health.load()
        entries = self._without_door(self.manifest.load())
        answered, names = self._session_names()
        if not answered:  # tmux hung: act on nothing, keep what we knew, still tell the outside we ran
            if self._track("tmux", "tmux did not answer list-sessions") == "failing":
                self.log("tmux did not answer; run skipped, state kept")
            self.state.save(prev, histories, queued)
            self._beat(prev)
            self.health.save(self._health)
            return 0
        self._track("tmux", None)
        now = self.clock.now()
        night = self.policy.enabled and is_night(self.policy, self.local_hour(now))
        rows = self._observe(entries, histories, names)
        loaded = histories
        d = plan([o for o, _ in rows], histories, now, self.claude_bin, self.policy, night)
        conditions, histories = dict(d.conditions), dict(d.histories)
        # plan() only returns hosts; the door's own history (restarts, throttled pid) comes from the file.
        door_actions, door_condition, door_history = self._door_plan(names, loaded, now, night)
        if door_condition is not None:
            conditions[DOOR_KEY], histories[DOOR_KEY] = door_condition, door_history

        # Stop first: free memory before anything starts.
        self._run_stops([a for a in d.actions if isinstance(a, Stop)], conditions, histories)

        restarted: List[str] = []
        for a in d.actions:
            if isinstance(a, Restart):
                try:
                    self.mux.new_session(a.name, a.dir, a.argv)
                    restarted.append(a.name)
                except RuntimeError as e:
                    conditions[a.name] = "RESTART_FAILED"
                    self.log(f"restart {a.name} failed: {e}")
            elif isinstance(a, Relink):
                h = histories[a.name]
                blocker = self._relink_blocker(a.name)
                if blocker is None:
                    try:
                        self.mux.send_keys(a.name, "/remote-control")
                        self.log(f"relink: typed /remote-control into {a.name}")
                    except RuntimeError as e:
                        blocker = "tmux send-keys failed"
                        self.log(f"relink {a.name} failed: {e}")
                if blocker is not None:
                    # Try again next run; log the reason once per streak, not every 10 minutes.
                    histories[a.name] = replace(h, relinked_at=None, note=blocker)
                    if h.note != blocker:
                        self.log(f"relink skipped for {a.name}: {blocker}")

        if restarted:
            self._await_restarts(restarted, entries, conditions, histories)
        for a in door_actions:
            if isinstance(a, StartDoor):
                self._start_door(a, conditions, histories)
        self._run_throttles([a for a in tuple(d.actions) + door_actions if isinstance(a, Throttle)], histories)
        for n, c in conditions.items():
            if c == "PARKED" and prev.get(n) != "PARKED":
                h = histories.get(n, History())
                idle = f", idle {_ago(now - h.last_active)}" if h.last_active is not None else ", last use unknown"
                self.log(f"parked {n} ({h.parked_reason}{idle})")

        change = diff_changes(prev, conditions)
        if change:
            self.log(change)
        msg = diff_alerts(prev, conditions)
        if msg:
            queued = queued + [msg]
        queued, dropped = coalesce_queue(queued, QUEUE_LIMIT)
        if dropped:
            self.log("alerts folded into a summary (queue full): " + " / ".join(dropped)[:1000])
        queued = self._deliver(queued)
        self.state.save(conditions, histories, queued)
        self._beat(conditions)
        self.health.save(self._health)
        return 0

    # --- the door -----------------------------------------------------------------------------
    def _without_door(self, entries: List[Entry]) -> List[Entry]:
        """A manifest entry named like the door's session would fight the door: ignore it, say so once."""
        kept = [e for e in entries if e.name != self.door.session]
        if len(kept) != len(entries) and not any("named like the door" in w for w in self.config_warnings):
            self.config_warnings.append(f"manifest entry '{self.door.session}' is named like the door's tmux "
                                        f"session; ignored (rename or remove it)")
        return kept

    def _door_pid(self) -> Optional[int]:
        pane = self.mux.pane_info(self.door.session)
        if pane is None or pane.dead:
            return None
        return pane.pid if is_door_command(self.procs.command(pane.pid)) else None

    def _observe_door(self, names: Optional[Set[str]]) -> DoorObservation:
        d = self.door
        exists = (d.session in names) if names is not None else self.mux.has_session(d.session)
        trusted = self.trust.trusted(d.dir) if self.trust is not None else True
        return DoorObservation(exists, self._door_pid() if exists else None, trusted)

    def _door_plan(self, names: Optional[Set[str]], histories: Dict[str, History], now: float, night: bool):
        if not self.door.enabled:
            return (), None, History()
        background = self.policy.enabled and night and self.policy.throttle_at_night
        return plan_door(self.door, self._observe_door(names), histories.get(DOOR_KEY, History()), now,
                         door_argv(self.door, self.claude_bin, self.door_name), background)

    def _start_door(self, a: StartDoor, conditions: Dict[str, str], histories: Dict[str, History]) -> None:
        try:
            self.mux.new_session(a.session, a.dir, a.argv)
        except RuntimeError as e:
            conditions[DOOR_KEY] = "RESTART_FAILED"
            histories[DOOR_KEY] = replace(histories[DOOR_KEY], last_state=State.RESTART_FAILED)
            self.log(f"door start failed: {e}")
            return
        for _ in range(int(RESTART_GRACE_S // POLL_S) + 1):
            if self._door_pid() is not None:
                return
            self.sleep(POLL_S)
        conditions[DOOR_KEY] = "RESTART_FAILED"
        histories[DOOR_KEY] = replace(histories[DOOR_KEY], last_state=State.RESTART_FAILED)
        pane = self.mux.capture(a.session, 20).strip().splitlines()[-8:]
        self.log("door did not start; pane: " + " | ".join(pane))

    def _run_throttles(self, throttles: List[Throttle], histories: Dict[str, History]) -> None:
        """Apply background/foreground priority, only to the pid that is still the session's own process."""
        moved: Dict[bool, List[str]] = {True: [], False: []}
        for a in throttles:
            if a.name == DOOR_KEY:
                ok = self._door_pid() == a.pid
                pane = None
                done = ok and self.qos.background(a.pid, a.on)
                if done and not a.on:
                    for child in self.procs.descendants(a.pid):  # sessions the door started at night
                        self.qos.background(child, False)
            else:
                pane = self.mux.pane_info(a.name)
                done = pane is not None and pane.pid == a.pid and self.qos.background(a.pid, a.on)
            h = histories.get(a.name)
            if done:
                moved[a.on].append("door" if a.name == DOOR_KEY else a.name)
            elif h is not None:
                # Not applied: remember the truth so the next run tries again (or forgets a gone pid).
                gone = pane is None or pane.pid != a.pid if a.name != DOOR_KEY else self._door_pid() != a.pid
                histories[a.name] = replace(h, throttled_pid=None if a.on or gone else h.throttled_pid)
        if moved[True]:
            self.log("background priority: " + ", ".join(moved[True]))
        if moved[False]:
            self.log("normal priority: " + ", ".join(moved[False]))

    def _profile_line(self, night: bool, live: int) -> str:
        if not self.policy.enabled:
            return "policy: off (every pinned host is kept running)"
        p = self.policy
        limit = f"park after {p.night_idle_minutes} min idle" if night else f"park after {p.idle_park_hours:g} h idle"
        return (f"policy: on, {'night' if night else 'day'} profile ({limit}); live {live}/{p.max_live}; "
                f"night {p.night_start_hour:02d}:00-{p.night_end_hour:02d}:00")

    def preview(self, at_hour: Optional[int] = None, as_json: bool = False) -> int:
        """`claude-rc plan`: what a watch run would do with the policy on, without doing any of it."""
        _, histories, _ = self.state.load()
        answered, names = self._session_names()
        if not answered:
            print("tmux did not answer", file=self.out)
            return 1
        now = self.clock.now()
        hour = self.local_hour(now) if at_hour is None else at_hour
        pol = replace(self.policy, enabled=True)
        night = is_night(pol, hour)
        rows = self._observe(self._without_door(self.manifest.load()), histories, names)
        d = plan([o for o, _ in rows], histories, now, self.claude_bin, pol, night)
        dc = replace(self.door, enabled=True)  # show what the door would do even while it is off
        door_cond = plan_door(dc, self._observe_door(names), histories.get(DOOR_KEY, History()), now,
                              door_argv(dc, self.claude_bin, self.door_name), night and pol.throttle_at_night)[1]
        by_pid = {r.pid: r for r in self.registry.live_sessions()}
        acts: Dict[str, List[object]] = {}
        for a in d.actions:
            acts.setdefault(getattr(a, "name", ""), []).append(a)
        out = []
        for o, _ in rows:
            n = o.entry.name
            would, blocked = "-", ""
            for a in acts.get(n, []):
                if isinstance(a, Stop):
                    would = f"park:{a.reason}"
                    pane = self.mux.pane_info(n)
                    text = self.mux.capture(n, 40, ansi=True) if pane is not None else ""
                    blocked = stop_blocker(by_pid.get(a.pid), pane, text, a.pid, a.status_since) or ""
                elif isinstance(a, Restart):
                    would = "restart"
                elif isinstance(a, Relink):
                    would = "relink"
                elif isinstance(a, Throttle) and would == "-":
                    would = "background" if a.on else "normal priority"
            if would == "-" and d.conditions.get(n) == "PARKED" and o.state is not State.PARKED:
                would = f"park:{d.histories[n].parked_reason}"
            elif would == "-" and o.state in (State.OK, State.UNLINKED):
                would = "keep"
            idle = (_ago(now - o.status_since) if o.status == "idle" and o.status_since is not None else "")
            out.append({"name": n, "state": o.state.value, "tier": o.entry.tier, "status": o.status or "",
                        "idle": idle, "would": would, "blocked": blocked})
        live_now = sum(1 for o, _ in rows if o.state in (State.OK, State.UNLINKED))
        parks = sum(1 for r in out if r["would"].startswith("park:") and r["state"] in ("OK", "UNLINKED") and not r["blocked"])
        starts = sum(1 for r in out if r["would"] == "restart")
        if as_json:
            json.dump({"profile": "night" if night else "day", "hour": hour, "policy_enabled": self.policy.enabled,
                       "door": {"enabled": self.door.enabled, "would": door_cond},
                       "live_now": live_now, "live_after": live_now - parks + starts, "rows": out,
                       "warnings": self.config_warnings}, self.out, indent=2)
            self.out.write("\n")
            return 0
        print(f"{'night' if night else 'day'} profile at {hour:02d}:00"
              + ("" if self.policy.enabled else "  (policy is OFF in config.json: showing what enabling it would do)"),
              file=self.out)
        print(f"live hosts: {live_now} now -> {live_now - parks + starts} after", file=self.out)
        door_note = {"OK": "running", "RESTARTED": "would be started", "NOT_RUNNING": "its tmux session runs something else"}
        print(f"door ({'on' if self.door.enabled else 'off in config.json'}): "
              f"{door_note.get(door_cond, door_cond)}: {' '.join(door_argv(self.door, 'claude', self.door_name)[1:])}",
              file=self.out)
        print(f"{'NAME':<24} {'STATE':<15} {'TIER':<5} {'STATUS':<8} {'IDLE':<8} {'WOULD':<16} BLOCKED", file=self.out)
        for r in out:
            print(f"{r['name']:<24} {r['state']:<15} {r['tier']:<5} {r['status']:<8} {r['idle']:<8} {r['would']:<16} {r['blocked']}",
                  file=self.out)
        for w in self.config_warnings:
            print(f"config: {w}", file=self.out)
        return 0

    def _beat(self, conditions: Dict[str, str]) -> None:
        """Tell the outside dead-man switch this run happened. Never fatal."""
        sender = self.heartbeat() if self.heartbeat else None
        if sender is None:
            return
        hosts = {n: c for n, c in conditions.items() if not n.startswith("@")}
        payload = {"source": "claude-rc", "host": self.hostname,
                   "ok": sum(1 for c in hosts.values() if c == "OK"),
                   "total": len(hosts),
                   "needs_you": sum(1 for c in conditions.values() if c in NEEDS_YOU)}
        parked = sum(1 for c in hosts.values() if c == "PARKED")
        if self.policy.enabled or parked:
            payload["parked"] = parked
        if self.door.enabled:
            payload["door"] = conditions.get(DOOR_KEY, "unknown")
        failing = sorted(c for c, h in self._health.items() if h.failures)
        if failing:
            payload["failing"] = failing
        try:
            sender.beat(payload)  # type: ignore[attr-defined]
        except DeliveryError as e:
            if self._track("heartbeat", str(e)) == "failing":
                self.log(f"heartbeat not delivered ({e})")
            return
        self._track("heartbeat", None)

    def _await_restarts(self, names: List[str], entries: List[Entry], conditions: Dict[str, str],
                        histories: Dict[str, History]) -> None:
        """Give restarted hosts the grace period to register on their conversation."""
        want = {e.name: e.conversation for e in entries if e.name in names}
        pending = set(names)
        for _ in range(int(RESTART_GRACE_S // POLL_S) + 1):
            recs = self._records_by_name()
            for n in list(pending):
                r = recs.get(n)
                if r and r.alive and r.conversation == want[n]:
                    pending.discard(n)
            if not pending:
                return
            self.sleep(POLL_S)
        for n in pending:
            conditions[n] = "RESTART_FAILED"
            histories[n] = replace(histories[n], last_state=State.RESTART_FAILED)
            pane = self.mux.capture(n, 20).strip().splitlines()[-8:]
            self.log(f"restart {n} did not register on {want[n][:8]}; pane: " + " | ".join(pane))

    def _deliver(self, queued: List[str]) -> List[str]:
        if not queued:
            return []
        channel = self.text()
        if channel is None:
            self.log("alerts not configured; not sent: " + " / ".join(queued))
            return []
        sender, to = channel
        for i, msg in enumerate(queued):
            try:
                sender.send_text(to, msg)  # type: ignore[attr-defined]
            except DeliveryError as e:
                if self._track("alerts", str(e)) == "failing":
                    self.log(f"alert not delivered ({e}); will retry")
                return queued[i:]
        self._track("alerts", None)
        return []

    def report(self) -> int:
        _, histories, _ = self.state.load()
        self._health = self.health.load()
        now = self.clock.now()
        rows = []
        door_condition = self._door_plan(self._session_names()[1], histories, now, False)[1]
        if door_condition is not None:
            h = histories.get(DOOR_KEY, History())
            rows.append(ReportRow("door", "OK" if door_condition == "OK" else door_condition, "", door_condition == "OK",
                                  self.door.permission_mode, sum(1 for t in h.restarts if now - t < 86400), self.door.dir))
        for o, r in self._observe(self._without_door(self.manifest.load()), histories):
            h = histories.get(o.entry.name, History())
            rows.append(ReportRow(o.entry.name, o.state.value, o.entry.conversation, bool(r and r.linked),
                                  r.version if r else "-", sum(1 for t in h.restarts if now - t < 86400), o.entry.dir,
                                  o.trusted))
        subject, html = format_report(rows, self._problems())
        channel = self.mail()
        if channel is None:
            print("report: no email provider configured", file=self.out)
            return 2
        sender, to = channel
        try:
            sender.send_email(to, subject, html)  # type: ignore[attr-defined]
        except DeliveryError as e:
            self.log(f"report not delivered ({e})")
            print(f"report not delivered: {e}", file=self.out)
            self._track("report", str(e), after=1)  # one report a day: say so on the first miss
            self.health.save(self._health)
            return 1
        self.log(f"report sent: {subject}")
        self._track("report", None)
        self.health.save(self._health)
        return 0

    def notify_test(self, which: str) -> int:
        channel = self.text() if which == "alerts" else self.mail()
        if channel is None:
            print(f"{which}: no provider configured", file=self.out)
            return 2
        sender, to = channel
        try:
            if which == "alerts":
                sender.send_text(to, "claude-rc: test alert")  # type: ignore[attr-defined]
            else:
                sender.send_email(to, "claude-rc: test report", "<p>claude-rc test report</p>")  # type: ignore[attr-defined]
        except DeliveryError as e:
            print(f"{which}: not delivered: {e}", file=self.out)
            return 1
        print(f"{which}: sent to {to}", file=self.out)
        return 0


# --- real wiring ------------------------------------------------------------------------------

def _load_secrets(path: str) -> Dict[str, str]:
    env: Dict[str, str] = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def _claude_bin() -> str:
    explicit = os.environ.get("CLAUDE_RC_CLAUDE")
    if explicit:
        return explicit
    local = os.path.expanduser("~/.local/bin/claude")
    return local if os.path.exists(local) else (shutil.which("claude") or "claude")


def build_app(home: Optional[str] = None) -> App:
    from .adapters.files import JsonManifestStore, JsonStateStore, read_json
    from .adapters.registry import FileSessionRegistry
    from .adapters.tmux import TmuxMultiplexer
    from .adapters.trust import ClaudeJsonTrust
    from .notify import ConfigError, build_email_sender, build_heartbeat_sender, build_text_sender

    home = home or os.environ.get("CLAUDE_RC_HOME") or os.path.expanduser("~/.config/claude-rc")
    try:
        config = read_json(os.path.join(home, "config.json"), {})
    except ValueError as e:  # a typo in config.json must not stop the watchdog
        print(f"claude-rc: config.json is not valid JSON ({e}); using defaults", file=sys.stderr)
        config = {}
    from .core import parse_door, parse_policy
    policy, warnings = parse_policy(config.get("policy"))
    door, door_warnings = parse_door(config.get("door"))
    warnings += door_warnings
    door = replace(door, dir=os.path.expanduser(door.dir))
    host = socket.gethostname().split(".")[0]
    env = dict(os.environ)
    env.update(_load_secrets(os.path.join(home, "secrets.env")))

    def channel(key: str, build) -> Channel:
        def make():
            cfg = config.get(key)
            if not cfg:
                return None
            try:
                return build(cfg, env), cfg["to"]
            except (ConfigError, KeyError) as e:
                print(f"claude-rc: {key} config: {e}", file=sys.stderr)
                return None
        return make

    def heartbeat_channel():
        cfg = config.get("heartbeat")
        if not cfg:
            return None
        try:
            return build_heartbeat_sender(cfg, env)
        except ConfigError as e:
            print(f"claude-rc: heartbeat config: {e}", file=sys.stderr)
            return None

    class _Clock:
        def now(self) -> float:
            return time.time()

    registry_dir = os.environ.get("CLAUDE_RC_REGISTRY") or os.path.expanduser("~/.claude/sessions")
    from .adapters.qos import TaskPolicy
    from .notify.local import MacNotification

    mux = TmuxMultiplexer()
    app = App(local=MacNotification(), policy=policy, qos=TaskPolicy(), config_warnings=warnings, door=door,
              door_name=door.name or host.lower(), home=home, registry=FileSessionRegistry(registry_dir), mux=mux,
               manifest=JsonManifestStore(os.path.join(home, "sessions.json")),
               state=JsonStateStore(os.path.join(home, "state.json")), clock=_Clock(), claude_bin=_claude_bin(),
               text=channel("alerts", build_text_sender), mail=channel("report", build_email_sender),
               trust=ClaudeJsonTrust(), heartbeat=heartbeat_channel,
               hostname=socket.gethostname().split(".")[0])
    mux.on_error = app.log  # a hung or missing tmux is logged, never fatal
    return app


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="claude-rc", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("adopt"); a.add_argument("--quiet", action="store_true")  # noqa: E702
    pn = sub.add_parser("pin"); pn.add_argument("name"); pn.add_argument("dir"); pn.add_argument("conversation")  # noqa: E702
    pn.add_argument("args", nargs=argparse.REMAINDER)
    h = sub.add_parser("has"); h.add_argument("name")  # noqa: E702
    k = sub.add_parser("keep"); k.add_argument("names", nargs="+")  # noqa: E702
    au = sub.add_parser("auto"); au.add_argument("names", nargs="+")  # noqa: E702
    u = sub.add_parser("up"); u.add_argument("names", nargs="*")  # noqa: E702
    pk = sub.add_parser("park"); pk.add_argument("names", nargs="+")  # noqa: E702
    upk = sub.add_parser("unpark"); upk.add_argument("names", nargs="*"); upk.add_argument("--all", action="store_true")  # noqa: E702
    c = sub.add_parser("check"); c.add_argument("--json", action="store_true")  # noqa: E702
    pl = sub.add_parser("plan"); pl.add_argument("--at", type=int, metavar="HOUR"); pl.add_argument("--json", action="store_true")  # noqa: E702
    pl.add_argument("--dry-run", action="store_true", help="accepted for clarity; plan never changes anything")
    sub.add_parser("watch")
    sub.add_parser("report")
    t = sub.add_parser("notify-test"); t.add_argument("which", choices=["alerts", "report"])  # noqa: E702
    ns = p.parse_args(argv)

    app = build_app()
    if ns.cmd == "adopt":
        return app.adopt(ns.quiet)
    if ns.cmd == "pin":
        rest = ns.args[1:] if ns.args[:1] == ["--"] else ns.args
        return app.pin(ns.name, ns.dir, ns.conversation, rest)
    if ns.cmd == "has":
        return app.has(ns.name)
    if ns.cmd in ("keep", "auto"):
        return app.set_tier(ns.names, ns.cmd)
    if ns.cmd == "up":
        return app.up(ns.names)
    if ns.cmd == "park":
        return app.park(ns.names)
    if ns.cmd == "unpark":
        return app.unpark(ns.names, ns.all)
    if ns.cmd == "check":
        return app.check(ns.json)
    if ns.cmd == "plan":
        return app.preview(ns.at, ns.json)
    if ns.cmd == "watch":
        return app.watch()
    if ns.cmd == "report":
        return app.report()
    return app.notify_test(ns.which)


if __name__ == "__main__":
    sys.exit(main())
