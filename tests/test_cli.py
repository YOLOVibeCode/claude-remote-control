"""The commands, wired to fakes: a whole watch cycle without tmux, claude, or the network."""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.adapters.files import JsonManifestStore, JsonStateStore  # noqa: E402
from claude_rc.cli import App, flags_to_record  # noqa: E402
from claude_rc.core import Entry, Record  # noqa: E402
from claude_rc.ports import DeliveryError  # noqa: E402

CONV = "11111111-1111-1111-1111-111111111111"
BIN = "/bin/claude"


class FakeRegistry:
    def __init__(self):
        self.records = []

    def live_sessions(self):
        return list(self.records)


class FakeMux:
    def __init__(self, registry, comes_up=True):
        self.sessions, self.calls, self.registry, self.comes_up = set(), [], registry, comes_up

    def has_session(self, name):
        return name in self.sessions

    def new_session(self, name, directory, argv):
        self.calls.append(("new", name, directory, tuple(argv)))
        self.sessions.add(name)
        if self.comes_up:  # claude starts and registers on the resumed conversation
            conv = argv[argv.index("--resume") + 1]
            self.registry.records.append(Record(99, conv, directory, name, "idle", True, "2.1.291", True))

    def send_keys(self, name, text):
        self.calls.append(("keys", name, text))

    pane = "\x1b[39m❯\xa0\n"

    def capture(self, name, lines, ansi=False):
        return self.pane if ansi else "Workspace not trusted\n"


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

    def app(self, **kw):
        base = dict(home=self.home, registry=self.reg, mux=self.mux, manifest=self.manifest, state=self.state,
                    trust=self.trust,
                    clock=self.clock, claude_bin=BIN, text=lambda: (self.text, "+15125550100"),
                    mail=lambda: (self.mail, "me@example.com"), out=self.out, sleep=lambda s: None,
                    heartbeat=lambda: self.beats, hostname="mac")
        base.update(kw)
        return App(**base)

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

    # watch ----------------------------------------------------------------------------------
    def test_watch_restarts_a_dead_host_and_reports_restart_then_return(self):
        self.pin_app()
        self.app().watch()
        self.assertEqual(self.mux.calls[0], ("new", "app", "/w/app",
                                             (BIN, "--remote-control", "app", "--resume", CONV, "--dangerously-skip-permissions")))
        self.assertEqual(self.text.sent, [("+15125550100", "claude-rc: restarted app")])
        self.clock.t += 600
        self.app().watch()
        self.assertEqual(self.text.sent[-1], ("+15125550100", "claude-rc: back app"))
        self.clock.t += 600
        self.app().watch()
        self.assertEqual(len(self.text.sent), 2)  # stable: silent

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

    def test_failed_delivery_is_queued_and_sent_next_run(self):
        self.pin_app()
        self.text.fail = True
        self.app().watch()
        self.assertEqual(self.text.sent, [])
        self.text.fail = False
        self.clock.t += 600
        self.app().watch()
        bodies = [b for _, b in self.text.sent]
        self.assertEqual(bodies, ["claude-rc: restarted app", "claude-rc: back app"])

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
