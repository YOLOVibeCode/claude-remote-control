"""claude-rc: pin, supervise, and report the Remote Control hosts running in tmux.

    claude-rc adopt [--quiet]          record live tmux hosts in the manifest (never overwrites)
    claude-rc pin NAME DIR UUID -- ARGS   record a new session (called by the claude() wrapper)
    claude-rc has NAME                 exit 0 if NAME is pinned
    claude-rc keep|auto NAME...        keep: always running, never parked; auto: parked when idle
    claude-rc up [NAME...]             start pinned hosts that have no tmux session
    claude-rc check [--json]           state of every pinned host; exit 1 if any needs attention
    claude-rc watch                    check, restart/relink what is safe, alert on changes
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
from typing import Callable, Dict, Iterator, List, Optional, Sequence, TextIO, Tuple

from .core import (
    ESCALATE_AFTER,
    NEEDS_YOU,
    ChannelHealth,
    RESTART_GRACE_S,
    Entry,
    History,
    Observation,
    Record,
    Relink,
    ReportRow,
    Restart,
    State,
    adopt,
    classify,
    coalesce_queue,
    diff_alerts,
    diff_changes,
    input_is_empty,
    format_report,
    plan,
    track_delivery,
)
from .ports import DeliveryError, EmailSender, TextSender

DEFAULT_FLAGS = ("--dangerously-skip-permissions",)   # how `cc` starts sessions
QUEUE_LIMIT = 20
LOG_MAX_BYTES = 1_000_000   # watch.log / launchd.log roll over here, keeping one old copy (.1)
POLL_S = 5

def rotate(path: str, max_bytes: int = LOG_MAX_BYTES) -> None:
    """Move a log past the cap to <path>.1 (replacing the previous one)."""
    try:
        if os.path.getsize(path) > max_bytes:
            os.replace(path, path + ".1")
    except FileNotFoundError:
        pass


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
                 health=None, local=None):
        from .adapters.files import JsonHealthStore
        from .notify.local import NoNotifier

        self.home, self.registry, self.mux, self.manifest, self.state = home, registry, mux, manifest, state
        self.trust, self.heartbeat, self.hostname = trust, heartbeat, hostname
        self.clock, self.claude_bin, self.text, self.mail, self.out, self.sleep = clock, claude_bin, text, mail, out, sleep
        self.health = health if health is not None else JsonHealthStore(os.path.join(home, "health.json"))
        self.local = local if local is not None else NoNotifier()
        self._health: Dict[str, ChannelHealth] = {}

    # --- helpers ----------------------------------------------------------------------------
    def log(self, line: str) -> None:
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        rotate(os.path.join(self.home, "watch.log"))
        fd = os.open(os.path.join(self.home, "watch.log"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(self.clock.now()))} {line}\n")

    @contextlib.contextmanager
    def locked(self) -> Iterator[bool]:
        os.makedirs(self.home, mode=0o700, exist_ok=True)
        with open(os.path.join(self.home, "watch.lock"), "w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                yield False
                return
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

    def _observe(self, entries: List[Entry], histories: Dict[str, History]) -> List[Tuple[Observation, Optional[Record]]]:
        recs = self._records_by_name()
        now = self.clock.now()
        rows = []
        for e in entries:
            r = recs.get(e.name)
            state = classify(e, self.mux.has_session(e.name), r, histories.get(e.name, History()), now)
            trusted = self.trust.trusted(e.dir) if self.trust is not None else True
            rows.append((Observation(e, state, r.status if r and r.alive else None, trusted), r))
        return rows

    # --- commands ---------------------------------------------------------------------------
    def adopt(self, quiet: bool = False) -> int:
        entries = self.manifest.load()
        new, notes = adopt(entries, self.registry.live_sessions(), DEFAULT_FLAGS)
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

    def up(self, names: Sequence[str] = ()) -> int:
        conditions, histories, queued = self.state.load()
        now = self.clock.now()
        for e in self.manifest.load():
            if not e.enabled or (names and e.name not in names) or self.mux.has_session(e.name):
                continue
            argv = [self.claude_bin, "--remote-control", e.name, "--resume", e.conversation, *e.flags]
            self.mux.new_session(e.name, e.dir, argv)
            h = histories.get(e.name, History())
            histories[e.name] = replace(h, restarts=h.restarts + (now,), last_state=State.STARTING,
                                        idle_unlinked_runs=0, relinked_at=None)
            print(f"started {e.name} on {e.conversation[:8]}", file=self.out)
            self.log(f"up: started {e.name}")
        self.state.save(conditions, histories, queued)
        return 0

    def check(self, as_json: bool = False) -> int:
        _, histories, _ = self.state.load()
        rows = self._observe(self.manifest.load(), histories)
        data = [{"name": o.entry.name, "state": o.state.value, "conversation": o.entry.conversation,
                 "linked": bool(r and r.linked), "status": o.status, "version": r.version if r else None,
                 "trusted": o.trusted}
                for o, r in rows]
        if as_json:
            json.dump(data, self.out, indent=2)
            self.out.write("\n")
        else:
            for d in data:
                print(f"{d['state']:<19} {d['name']:<24} {d['conversation'][:8]}  "
                      f"{'linked' if d['linked'] else 'no-link'}  {d['status'] or '-'}"
                      f"{'' if d['trusted'] else '  (folder not trusted: restart would stop at the prompt)'}", file=self.out)
        fine = {State.OK, State.DISABLED, State.STARTING}
        return 0 if all(o.state in fine for o, _ in rows) else 1

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
            self.log(f"{channel} delivered again after {cur.failures} failed runs")
        return event

    def _problems(self) -> List[str]:
        out = []
        for channel, h in sorted(self._health.items()):
            if h.failures:
                since = time.strftime("%Y-%m-%d %H:%M", time.localtime(h.since or self.clock.now()))
                out.append(f"{channel} not delivered since {since} ({h.failures} runs): {h.last_error}")
        return out

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
        entries = self.manifest.load()
        rows = self._observe(entries, histories)
        now = self.clock.now()
        d = plan([o for o, _ in rows], histories, now, self.claude_bin)
        conditions, histories = dict(d.conditions), dict(d.histories)

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

    def _beat(self, conditions: Dict[str, str]) -> None:
        """Tell the outside dead-man switch this run happened. Never fatal."""
        sender = self.heartbeat() if self.heartbeat else None
        if sender is None:
            return
        payload = {"source": "claude-rc", "host": self.hostname,
                   "ok": sum(1 for c in conditions.values() if c == "OK"),
                   "total": len(conditions),
                   "needs_you": sum(1 for c in conditions.values() if c in NEEDS_YOU)}
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
        for o, r in self._observe(self.manifest.load(), histories):
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
    config = read_json(os.path.join(home, "config.json"), {})
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
    from .notify.local import MacNotification

    mux = TmuxMultiplexer()
    app = App(local=MacNotification(), home=home, registry=FileSessionRegistry(registry_dir), mux=mux,
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
    c = sub.add_parser("check"); c.add_argument("--json", action="store_true")  # noqa: E702
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
    if ns.cmd == "check":
        return app.check(ns.json)
    if ns.cmd == "watch":
        return app.watch()
    if ns.cmd == "report":
        return app.report()
    return app.notify_test(ns.which)


if __name__ == "__main__":
    sys.exit(main())
