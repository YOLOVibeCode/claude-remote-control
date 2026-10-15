"""The commands, wired to fakes: a whole watch cycle without tmux, claude, or the network."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.adapters.files import JsonManifestStore, JsonStateStore  # noqa: E402
from claude_rc.cli import App, flags_to_record  # noqa: E402
from claude_rc.core import DoorConfig, Entry, PaneInfo, Policy, Record  # noqa: E402
from claude_rc.ports import DeliveryError  # noqa: E402

CONV = "11111111-1111-1111-1111-111111111111"
BIN = "/bin/claude"


class FakeProcs:
    def __init__(self):
        self.commands, self.children = {}, {}

    def alive(self, pid):
        return pid in self.commands

    def command(self, pid):
        return self.commands.get(pid, "")

    def descendants(self, pid):
        return list(self.children.get(pid, []))


class FakeRegistry:
    def __init__(self):
        self.records = []

    def live_sessions(self):
        return list(self.records)


class FakeMux:
    def __init__(self, registry, comes_up=True):
        self.sessions, self.calls, self.registry, self.comes_up = set(), [], registry, comes_up
        self.copy_mode, self.keys_fail, self.hung = set(), False, False
        self.pane_pids, self.procs = {}, FakeProcs()

    def has_session(self, name):
        return name in self.sessions

    def list_sessions(self):
        return None if self.hung else set(self.sessions)

    def pane_info(self, name):
        if name not in self.sessions:
            return None
        pid = self.pane_pids.get(name) or next((r.pid for r in self.registry.records if r.tmux_session == name and r.alive), 1)
        return PaneInfo(pid, name in self.copy_mode, False, 1, 1)

    def kill_session(self, name):
        self.calls.append(("kill", name))
        self.sessions.discard(name)
        return True

    door_pid = 777

    def new_session(self, name, directory, argv):
        self.calls.append(("new", name, directory, tuple(argv)))
        self.sessions.add(name)
        if not self.comes_up:
            return
        if "--resume" in argv:  # claude starts and registers on the resumed conversation
            conv = argv[argv.index("--resume") + 1]
            self.registry.records.append(Record(99, conv, directory, name, "idle", True, "2.1.291", True))
        else:                   # the door: the pane's process is the Remote Control server
            self.pane_pids[name] = self.door_pid
            self.procs.commands[self.door_pid] = " ".join(argv)

    exit_works = True

    def send_keys(self, name, text):
        if self.keys_fail:
            raise RuntimeError("tmux send-keys app failed: timed out")
        self.calls.append(("keys", name, text))
        if text == "/exit" and self.exit_works:  # claude exits and its tmux session closes
            self.registry.records = [replace(r, alive=False) if r.tmux_session == name else r for r in self.registry.records]
            self.sessions.discard(name)

    pane = "\x1b[39m❯\xa0\n"

    def capture(self, name, lines, ansi=False):
        return self.pane if ansi else "Workspace not trusted\n"


class FakeNotifier:
    def __init__(self):
        self.shown = []

    def notify(self, title, body):
        self.shown.append((title, body))
        return True


class FakeQos:
    enabled = True

    def __init__(self):
        self.calls = []

    def background(self, pid, on):
        self.calls.append((pid, on))
        return True


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def now(self):
        return self.t


class Capture:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def send_text(self, to, body):
        if self.fail:
            raise DeliveryError("stub", "rejected", 503)
        self.sent.append((to, body))

    def send_email(self, to, subject, html):
        if self.fail:
            raise DeliveryError("stub", "rejected", 503)
        self.sent.append((to, subject, html))


class AppTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.reg = FakeRegistry()
        self.mux = FakeMux(self.reg)
        self.clock = Clock()
        self.text, self.mail = Capture(), Capture()
        outer = self

        class Beats:
            sent, fail = [], False

            def beat(self, payload):
                if self.fail:
                    raise DeliveryError("stub", "unreachable")
                self.sent.append(payload)

        self.beats = Beats()
        self.beats.sent = []
        self.out = io.StringIO()
        self.manifest = JsonManifestStore(os.path.join(self.home, "sessions.json"))
        self.state = JsonStateStore(os.path.join(self.home, "state.json"))
        self.untrusted = set()
        outer = self

        class Trust:
            def trusted(self, d):
                return d not in outer.untrusted

        self.trust = Trust()
        self.notifier = FakeNotifier()

    def app(self, **kw):
        base = dict(home=self.home, registry=self.reg, mux=self.mux, manifest=self.manifest, state=self.state,
                    trust=self.trust,
                    clock=self.clock, claude_bin=BIN, text=lambda: (self.text, "+15125550100"),
                    mail=lambda: (self.mail, "me@example.com"), out=self.out, sleep=lambda s: None,
                    heartbeat=lambda: self.beats, hostname="mac", local=self.notifier, procs=self.mux.procs)
        base.update(kw)
        return App(**base)

    def log(self):
        try:
            with open(os.path.join(self.home, "watch.log")) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def pin_app(self):
        self.manifest.save([Entry("app", "/w/app", CONV, ("--dangerously-skip-permissions",))])

    # adopt / pin / has --------------------------------------------------------------------
    def test_adopt_records_live_tmux_hosts_once(self):
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.app().adopt()
        self.assertEqual([e.name for e in self.manifest.load()], ["app"])
        self.app().adopt()
        self.assertEqual(len(self.manifest.load()), 1)

    def test_pin_records_flags_and_refuses_to_overwrite(self):
        a = self.app()
        self.assertEqual(a.pin("app", "/w/app", CONV, ["--dangerously-skip-permissions", "--model", "opus", "fix the bug"]), 0)
        self.assertEqual(self.manifest.load()[0].flags, ("--dangerously-skip-permissions", "--model", "opus"))
        self.assertEqual(a.pin("app", "/w/app", "other", []), 2)
        self.assertEqual(self.manifest.load()[0].conversation, CONV)
        self.assertEqual(a.has("app"), 0)
        self.assertEqual(a.has("nope"), 1)

    def test_keep_and_auto_set_the_tier_and_an_unknown_name_exits_2(self):
        self.pin_app()
        self.assertEqual(self.app().set_tier(["app"], "keep"), 0)
        self.assertEqual(self.manifest.load()[0].tier, "keep")
        self.assertEqual(self.app().set_tier(["app"], "auto"), 0)
        self.assertEqual(self.manifest.load()[0].tier, "auto")
        self.assertEqual(self.app().set_tier(["nope"], "keep"), 2)

    # park / unpark / up ----------------------------------------------------------------------
    def live_app(self, status="idle", since=None, linked=True):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", status, linked, "2.1.300", True,
                                   status_since=since if since is not None else self.clock.t - 3600)]

    def test_park_types_exit_and_the_host_stays_parked(self):
        self.live_app()
        self.assertEqual(self.app().park(["app"]), 0)
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [("keys", "app", "/exit")])
        self.clock.t += 600
        self.app().watch()
        self.clock.t += 600
        self.app().watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"], [])   # never restarted
        self.assertEqual(self.text.sent, [])                                  # and nothing to text
        self.assertEqual(self.app().check(), 0)                               # parked is fine

    def test_park_is_blocked_by_a_draft_or_copy_mode_and_says_why(self):
        self.live_app()
        self.mux.pane = "\x1b[39m❯\xa0half-written thought\n"
        self.assertEqual(self.app().park(["app"]), 1)
        self.assertIn("not parked (text in its input box)", self.out.getvalue())
        self.mux.pane = "\x1b[39m❯\xa0\n"
        self.mux.copy_mode.add("app")
        self.assertEqual(self.app().park(["app"]), 1)
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [])

    def test_park_never_stops_a_busy_host(self):
        self.live_app(status="busy")
        self.assertEqual(self.app().park(["app"]), 1)
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [])

    def test_an_exit_that_hangs_closes_the_session_only_while_idle(self):
        self.live_app()
        self.mux.exit_works = False
        self.assertEqual(self.app().park(["app"]), 0)
        self.assertIn(("kill", "app"), self.mux.calls)
        self.assertIn("/exit did not end app", self.log())

    def test_an_exit_that_hangs_while_now_busy_is_not_killed(self):
        self.live_app()
        self.mux.exit_works = False
        outer = self

        class BusyAfterTyping(FakeRegistry):
            def live_sessions(self):
                typed = any(c[0] == "keys" for c in outer.mux.calls)
                return [replace(r, status="busy") if typed else r for r in outer.reg.records]

        self.assertEqual(self.app(registry=BusyAfterTyping()).park(["app"]), 1)
        self.assertNotIn(("kill", "app"), self.mux.calls)

    def test_up_skips_parked_unless_named_and_unpark_resumes_the_same_chat(self):
        self.live_app()
        self.app().park(["app"])
        self.app().up()
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"], [])
        self.assertIn("skipped 1 parked", self.out.getvalue())
        self.assertEqual(self.app().unpark(["app"]), 0)
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"][0],
                         ("new", "app", "/w/app", (BIN, "--remote-control", "app", "--resume", CONV, "--dangerously-skip-permissions")))
        _, histories, _ = self.state.load()
        self.assertIsNone(histories["app"].parked_at)

    def test_unpark_all_and_nothing_parked(self):
        self.pin_app()
        self.assertEqual(self.app().unpark(all_=True), 0)
        self.assertIn("nothing parked", self.out.getvalue())

    def test_unpark_refuses_when_the_conversation_is_open_elsewhere(self):
        self.live_app()
        self.app().park(["app"])
        self.reg.records = [Record(5, CONV, "/w/app", "app-2", "idle", True, "2.1.300", True)]  # ccc resumed it
        self.app().unpark(["app"])
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"], [])
        self.assertIn("already open in tmux session app-2", self.out.getvalue())

    def test_up_waits_for_the_lock_and_gives_up_cleanly(self):
        self.pin_app()
        a = self.app()
        with a.locked() as got:
            self.assertTrue(got)
            self.assertEqual(self.app().up(wait_s=1), 1)
        self.assertEqual(self.mux.calls, [])

    def test_the_lock_file_is_private(self):
        import stat
        with self.app().locked():
            pass
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.home, "watch.lock")).st_mode), 0o600)

    def test_park_a_dead_host_marks_it_parked(self):
        self.pin_app()
        self.assertEqual(self.app().park(["app"]), 0)
        self.app().watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"], [])

    # policy ---------------------------------------------------------------------------------
    def test_policy_off_never_parks_or_throttles(self):
        self.live_app(since=self.clock.t - 30 * 3600)
        qos = FakeQos()
        self.app(qos=qos, local_hour=lambda t: 1).watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [])
        self.assertEqual(qos.calls, [])

    def test_a_day_watch_parks_an_idle_host_and_it_stays_parked(self):
        self.live_app(since=self.clock.t - 9 * 3600)
        on = Policy(enabled=True)
        self.app(policy=on, local_hour=lambda t: 10).watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [("keys", "app", "/exit")])
        self.assertIn("parked app (idle, idle 9h00m)", self.log())
        for _ in range(2):
            self.clock.t += 600
            self.app(policy=on, local_hour=lambda t: 10).watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "new"], [])
        self.assertEqual(self.text.sent, [])

    def test_night_drops_a_keep_host_to_background_and_morning_restores_it(self):
        self.live_app(since=self.clock.t - 2 * 3600)
        self.app().set_tier(["app"], "keep")
        qos, on = FakeQos(), Policy(enabled=True)
        self.app(policy=on, qos=qos, local_hour=lambda t: 1).watch()
        self.clock.t += 600
        self.app(policy=on, qos=qos, local_hour=lambda t: 1).watch()
        self.clock.t += 600
        self.app(policy=on, qos=qos, local_hour=lambda t: 7).watch()
        self.assertEqual(qos.calls, [(1, True), (1, False)])
        self.assertIn("background priority: app", self.log())
        self.assertIn("normal priority: app", self.log())

    def test_an_unresponsive_tmux_skips_the_run_keeps_state_and_still_beats(self):
        self.pin_app()
        self.state.save({"app": "OK"}, {}, [])
        self.mux.hung = True
        self.assertEqual(self.app(policy=Policy(enabled=True)).watch(), 0)
        self.assertEqual(self.mux.calls, [])
        self.assertEqual(self.state.load()[0], {"app": "OK"})
        self.assertEqual(len(self.beats.sent), 1)
        self.assertIn("tmux did not answer; run skipped", self.log())

    def test_plan_is_read_only_and_shows_what_would_happen(self):
        self.live_app(since=self.clock.t - 9 * 3600)
        self.app().watch()
        path = os.path.join(self.home, "state.json")
        with open(path, "rb") as f:
            before = f.read()
        calls = list(self.mux.calls)
        self.assertEqual(self.app(local_hour=lambda t: 10).preview(), 0)
        out = self.out.getvalue()
        self.assertIn("policy is OFF in config.json", out)
        self.assertIn("park:idle", out)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(self.mux.calls, calls)

    def test_plan_at_a_night_hour_uses_the_night_profile(self):
        self.live_app(since=self.clock.t - 3600)
        self.app().preview(at_hour=1, as_json=True)
        data = json.loads(self.out.getvalue())
        self.assertEqual((data["profile"], data["rows"][0]["would"]), ("night", "park:night"))

    def test_plan_shows_why_a_park_would_be_blocked(self):
        self.live_app(since=self.clock.t - 9 * 3600)
        self.mux.copy_mode.add("app")
        self.app(local_hour=lambda t: 10).preview(as_json=True)
        self.assertEqual(json.loads(self.out.getvalue())["rows"][0]["blocked"], "copy mode")

    def test_check_shows_the_policy_line_and_config_warnings(self):
        self.live_app()
        self.app(policy=Policy(enabled=True), local_hour=lambda t: 10, config_warnings=["policy.x: unknown setting, ignored"]).check()
        out = self.out.getvalue()
        self.assertIn("policy: on, day profile", out)
        self.assertIn("attention: config: policy.x", out)

    # door -----------------------------------------------------------------------------------
    DOOR = DoorConfig(enabled=True, dir="/w/Dev")

    def test_watch_starts_the_door_when_enabled_and_down(self):
        self.app(door=self.DOOR).watch()
        self.assertEqual(self.mux.calls[0], ("new", "remote", "/w/Dev", (
            BIN, "remote-control", "--name", "mac", "--spawn", "same-dir", "--capacity", "2",
            "--permission-mode", "acceptEdits")))
        self.assertIn("restarted door", self.log())
        self.clock.t += 600
        self.app(door=self.DOOR).watch()
        self.assertEqual(len([c for c in self.mux.calls if c[0] == "new"]), 1)  # running: left alone

    def test_the_door_keeps_its_history_between_runs(self):
        self.app(door=self.DOOR).watch()
        self.clock.t += 600
        self.app(door=self.DOOR).watch()
        _, histories, _ = self.state.load()
        self.assertEqual(histories["@door"].restarts, (1_000_000.0,))

    def test_a_foreign_remote_session_is_reported_and_left_alone(self):
        self.mux.sessions.add("remote")
        self.mux.pane_pids["remote"] = 555
        self.mux.procs.commands[555] = "-zsh"
        self.app(door=self.DOOR).watch()
        self.assertEqual(self.mux.calls, [])
        self.assertIn("door (its tmux session runs something else)", self.text.sent[-1][1])

    def test_a_disabled_door_does_nothing(self):
        self.app().watch()
        self.assertEqual(self.mux.calls, [])
        self.assertNotIn("door", self.beats.sent[-1])

    def test_the_door_shows_in_check_json_and_the_heartbeat(self):
        self.app(door=self.DOOR).watch()
        self.assertEqual(self.beats.sent[-1]["door"], "RESTARTED")
        self.app(door=self.DOOR).check(as_json=True)
        door = [r for r in json.loads(self.out.getvalue()) if r.get("kind") == "door"][0]
        self.assertEqual((door["state"], door["permission_mode"]), ("OK", "acceptEdits"))

    def test_sessions_the_door_spawns_are_never_adopted(self):
        self.reg.records = [Record(5, CONV, "/w/Dev", "remote", "idle", True, "2.1.300", True)]
        self.app().adopt()
        self.assertEqual(self.manifest.load(), [])

    def test_a_manifest_entry_named_like_the_door_is_ignored_with_a_warning(self):
        self.manifest.save([Entry("remote", "/w/x", CONV)])
        self.app(door=self.DOOR).watch()
        self.assertEqual([c for c in self.mux.calls if c[1] == "remote" and "--resume" in c[3]], [])
        self.app(door=self.DOOR).check()
        self.assertIn("named like the door", self.out.getvalue())

    def test_night_moves_the_door_to_background_and_morning_restores_it_and_its_sessions(self):
        qos, on = FakeQos(), Policy(enabled=True)
        self.app(door=self.DOOR, policy=on, qos=qos, local_hour=lambda t: 1).watch()   # starts it
        self.clock.t += 600
        self.app(door=self.DOOR, policy=on, qos=qos, local_hour=lambda t: 1).watch()   # night: background
        self.mux.procs.children[777] = [778, 779]                                       # sessions started overnight
        self.clock.t += 600
        self.app(door=self.DOOR, policy=on, qos=qos, local_hour=lambda t: 7).watch()   # morning
        self.assertEqual(qos.calls, [(777, True), (777, False), (778, False), (779, False)])

    # watch ----------------------------------------------------------------------------------
    def test_watch_restarts_a_dead_host_and_logs_restart_then_return_without_texting(self):
        self.pin_app()
        self.app().watch()
        self.assertEqual(self.mux.calls[0], ("new", "app", "/w/app",
                                             (BIN, "--remote-control", "app", "--resume", CONV, "--dangerously-skip-permissions")))
        self.clock.t += 600
        self.app().watch()
        self.clock.t += 600
        self.app().watch()
        self.assertEqual(self.text.sent, [])  # it fixed itself: nothing for the phone
        with open(os.path.join(self.home, "watch.log")) as f:
            log = f.read()
        self.assertIn("claude-rc: restarted app", log)
        self.assertIn("claude-rc: back app", log)

    def test_watch_does_not_restart_into_an_untrusted_folder(self):
        self.pin_app()
        self.untrusted.add("/w/app")
        self.app().watch()
        self.assertEqual(self.mux.calls, [])
        self.assertIn("needs you app (folder not trusted", self.text.sent[-1][1])

    def test_check_json_shows_trust(self):
        self.pin_app()
        self.untrusted.add("/w/app")
        self.app().check(as_json=True)
        self.assertFalse(json.loads(self.out.getvalue())[0]["trusted"])

    def test_a_restart_that_never_registers_is_reported_with_the_pane(self):
        self.pin_app()
        self.mux.comes_up = False
        self.app().watch()
        self.assertIn("needs you app (restart failed)", self.text.sent[-1][1])
        with open(os.path.join(self.home, "watch.log")) as f:
            self.assertIn("Workspace not trusted", f.read())

    def test_unlinked_idle_host_is_relinked_once_then_reported(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", False, "2.1.291", True)]
        self.app().watch()
        self.assertFalse([c for c in self.mux.calls if c[0] == "keys"])
        self.clock.t += 600
        self.app().watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [("keys", "app", "/remote-control")])
        self.clock.t += 600
        self.app().watch()
        self.assertEqual(len([c for c in self.mux.calls if c[0] == "keys"]), 1)
        self.assertIn("needs you app (Remote Control still off after relink)", self.text.sent[-1][1])

    def test_relink_never_types_over_a_draft(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.mux.pane = "\x1b[39m❯\xa0half-written thought\n"
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", False, "2.1.291", True)]
        for _ in range(3):
            self.app().watch()
            self.clock.t += 600
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [])
        with open(os.path.join(self.home, "watch.log")) as f:
            self.assertIn("relink skipped for app: text in its input box", f.read())
        # Once the draft is gone, the relink happens.
        self.mux.pane = "\x1b[39m❯\xa0\n"
        self.app().watch()
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [("keys", "app", "/remote-control")])

    def test_relink_skip_is_logged_once_per_streak(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.mux.pane = "\x1b[39m❯\xa0half-written thought\n"
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", False, "2.1.291", True)]
        for _ in range(5):
            self.app().watch()
            self.clock.t += 600
        self.assertEqual(self.log().count("relink skipped for app: text in its input box"), 1)

    def test_relink_never_types_into_copy_mode(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.mux.copy_mode.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", False, "2.1.291", True)]
        for _ in range(3):
            self.app().watch()
            self.clock.t += 600
        self.assertEqual([c for c in self.mux.calls if c[0] == "keys"], [])
        self.assertIn("relink skipped for app: pane in copy mode", self.log())

    def test_a_send_keys_failure_during_relink_is_logged_and_the_run_completes(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.mux.keys_fail = True
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", False, "2.1.291", True)]
        for _ in range(2):
            self.assertEqual(self.app().watch(), 0)
            self.clock.t += 600
        self.assertIn("relink app failed", self.log())
        self.assertEqual(len(self.beats.sent), 2)  # both runs finished and beat

    def test_a_crashing_watch_run_is_logged_not_silent(self):
        self.pin_app()

        class Broken:
            def live_sessions(self):
                raise KeyError("registry went away")

        import contextlib
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.app(registry=Broken()).watch(), 1)
        self.assertIn("watch crashed: KeyError", self.log())

    def test_failed_delivery_is_queued_and_sent_next_run(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, "other-conv", "/w/app", "app", "idle", True, "2.1.291", True)]
        self.text.fail = True
        self.app().watch()
        self.assertEqual(self.text.sent, [])
        self.text.fail = False
        self.clock.t += 600
        self.app().watch()
        bodies = [b for _, b in self.text.sent]
        self.assertEqual(bodies, ["claude-rc: needs you app (wrong conversation)"])

    def test_alert_failures_escalate_once_after_three_runs_without_per_run_spam(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, "other-conv", "/w/app", "app", "idle", True, "2.1.291", True)]
        self.text.fail = True
        for _ in range(5):
            self.app().watch()
            self.clock.t += 600
        log = self.log()
        self.assertEqual(log.count("alert not delivered"), 1)
        self.assertEqual(log.count("escalated: alerts failing since"), 1)
        self.assertEqual(len(self.notifier.shown), 1)
        self.text.fail = False
        self.app().watch()
        self.assertIn("alerts delivered again after 5 failed runs", self.log())
        self.assertEqual([b for _, b in self.text.sent], ["claude-rc: needs you app (wrong conversation)"])

    def test_heartbeat_failures_escalate_once(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.beats.fail = True
        for _ in range(4):
            self.app().watch()
            self.clock.t += 600
        self.assertEqual(self.log().count("heartbeat not delivered"), 1)
        self.assertEqual(self.log().count("escalated: heartbeat failing"), 1)

    def test_a_full_queue_is_summarized_and_logged_not_dropped_silently(self):
        self.pin_app()
        self.state.save({}, {}, [f"claude-rc: old alert {i}" for i in range(25)])
        self.text.fail = True
        self.app().watch()
        _, _, queued = self.state.load()
        self.assertLessEqual(len(queued), 20)
        self.assertTrue(queued[0].startswith("claude-rc: ") and "earlier alerts not delivered" in queued[0])
        self.assertIn("alerts folded into a summary", self.log())

    def test_the_report_lists_failing_delivery(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.beats.fail = True
        self.app().watch()
        self.assertEqual(self.app().report(), 0)
        _, subject, html = self.mail.sent[0]
        self.assertEqual(subject, "claude-rc: 1/1 OK, alerts failing")
        self.assertIn("heartbeat not delivered since", html)

    def test_wrong_conversation_is_reported_and_never_touched(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, "other-conv", "/w/app", "app", "idle", True, "2.1.291", True)]
        self.app().watch()
        self.assertEqual(self.mux.calls, [])
        self.assertEqual(self.text.sent[-1][1], "claude-rc: needs you app (wrong conversation)")

    def test_a_second_watch_while_one_holds_the_lock_does_nothing(self):
        self.pin_app()
        a = self.app()
        with a.locked() as got:
            self.assertTrue(got)
            self.app().watch()
        self.assertEqual(self.mux.calls, [])

    def test_watch_without_alert_config_still_acts_and_logs(self):
        self.pin_app()
        self.app(text=lambda: None).watch()
        self.assertEqual(len(self.mux.calls), 1)
        with open(os.path.join(self.home, "watch.log")) as f:
            self.assertIn("restarted app", f.read())

    def test_the_log_is_private(self):
        import stat
        self.pin_app()
        self.app().watch()
        mode = stat.S_IMODE(os.stat(os.path.join(self.home, "watch.log")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_every_watch_run_beats_with_its_counts(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.app().watch()
        self.assertEqual(self.beats.sent, [{"source": "claude-rc", "host": "mac", "ok": 1, "total": 1, "needs_you": 0}])

    def test_a_failed_beat_is_logged_and_never_stops_the_watch(self):
        self.pin_app()
        self.beats.fail = True
        self.assertEqual(self.app().watch(), 0)
        self.assertEqual(len(self.mux.calls), 1)  # the restart still happened
        with open(os.path.join(self.home, "watch.log")) as f:
            self.assertIn("heartbeat not delivered", f.read())

    def test_logs_roll_over_at_the_cap_keeping_one_old_copy(self):
        import claude_rc.cli as cli
        self.pin_app()
        log = os.path.join(self.home, "watch.log")
        launchd = os.path.join(self.home, "launchd.log")
        for path in (log, launchd):
            with open(path, "w") as f:
                f.write("x" * (cli.LOG_MAX_BYTES + 10))
        self.app().watch()
        for path in (log, launchd):
            self.assertTrue(os.path.exists(path + ".1"), path)
            self.assertGreater(os.path.getsize(path + ".1"), cli.LOG_MAX_BYTES)
        self.assertLess(os.path.getsize(log), 10_000)
        import stat
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    # check / report --------------------------------------------------------------------------
    def test_check_exits_nonzero_when_something_is_wrong_and_prints_json(self):
        self.pin_app()
        self.assertEqual(self.app().check(as_json=True), 1)
        rows = json.loads(self.out.getvalue())
        self.assertEqual(rows[0]["name"], "app")
        self.assertEqual(rows[0]["state"], "DEAD")
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.assertEqual(self.app().check(), 0)

    def test_report_emails_the_all_clear(self):
        self.pin_app()
        self.mux.sessions.add("app")
        self.reg.records = [Record(1, CONV, "/w/app", "app", "idle", True, "2.1.291", True)]
        self.assertEqual(self.app().report(), 0)
        to, subject, html = self.mail.sent[0]
        self.assertEqual((to, subject), ("me@example.com", "claude-rc: 1/1 OK"))
        self.assertIn("11111111", html)


class FlagsTest(unittest.TestCase):
    def test_keeps_flags_and_their_values_drops_prompts_and_pins(self):
        self.assertEqual(
            flags_to_record(["--dangerously-skip-permissions", "--remote-control", "x", "--session-id", "u",
                             "--permission-mode", "plan", "write tests", "--verbose"]),
            ("--dangerously-skip-permissions", "--permission-mode", "plan", "--verbose"),
        )


if __name__ == "__main__":
    unittest.main()
