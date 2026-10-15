"""adopt() and the adapters behind the ports: registry files, manifest/state files, tmux."""
from __future__ import annotations

import json
import os
import shutil
import stat
import time
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.adapters.qos import TaskPolicy  # noqa: E402
from claude_rc.adapters.files import JsonHealthStore, JsonManifestStore, JsonStateStore  # noqa: E402
from claude_rc.adapters.registry import FileSessionRegistry  # noqa: E402
from claude_rc.adapters.run import run_command  # noqa: E402
from claude_rc.adapters.tmux import TmuxMultiplexer  # noqa: E402
from claude_rc.adapters.trust import ClaudeJsonTrust  # noqa: E402
from claude_rc.core import ChannelHealth, Entry, History, PaneInfo, Record, State, adopt  # noqa: E402
from claude_rc.notify.local import MacNotification  # noqa: E402

CONV = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
FLAGS = ("--dangerously-skip-permissions",)


def rec(name, conv=CONV, tmux=True, alive=True):
    return Record(pid=1, conversation=conv, cwd=f"/w/{name}", tmux_session=name if tmux else None,
                  status="idle", linked=True, version="2.1.291", alive=alive)


class AdoptTest(unittest.TestCase):
    def test_new_tmux_hosts_are_added_with_default_flags(self):
        entries, notes = adopt([], [rec("a"), rec("b")], FLAGS)
        self.assertEqual(entries, [Entry("a", "/w/a", CONV, FLAGS), Entry("b", "/w/b", CONV, FLAGS)])
        self.assertEqual(notes, ["added a", "added b"])

    def test_existing_entries_are_never_overwritten_but_drift_is_noted(self):
        mine = Entry("a", "/w/a", CONV, ("--model", "opus"))
        entries, notes = adopt([mine], [rec("a", conv=OTHER)], FLAGS)
        self.assertEqual(entries, [mine])
        self.assertEqual(notes, ["a runs 22222222, manifest pins 11111111"])

    def test_processes_outside_tmux_and_dead_ones_are_ignored(self):
        entries, notes = adopt([], [rec("vscode", tmux=False), rec("old", alive=False)], FLAGS)
        self.assertEqual((entries, notes), ([], []))


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def write(self, pid, **kw):
        d = dict(pid=pid, sessionId=CONV, cwd="/w/app", tmux="app:@3.%3", status="idle",
                 bridgeSessionId="session_01x", version="2.1.291", kind="interactive")
        d.update(kw)
        with open(os.path.join(self.dir, f"{pid}.json"), "w") as f:
            json.dump(d, f)

    def test_reads_records_and_maps_the_tmux_pane_to_its_session_name(self):
        self.write(10)
        self.write(11, tmux=None, bridgeSessionId=None, status="busy", sessionId=OTHER)
        reg = FileSessionRegistry(self.dir, pid_alive=lambda pid: pid == 10)
        got = sorted(reg.live_sessions(), key=lambda r: r.pid)
        self.assertEqual(got[0], Record(10, CONV, "/w/app", "app", "idle", True, "2.1.291", True))
        self.assertEqual(got[1], Record(11, OTHER, "/w/app", None, "busy", False, "2.1.291", False))

    def test_reads_status_since_in_seconds_and_waiting_for(self):
        self.write(10, statusUpdatedAt=1792062167406, waitingFor="input needed", status="waiting")
        self.write(11, statusUpdatedAt="not a number")
        reg = FileSessionRegistry(self.dir, pid_alive=lambda pid: True)
        got = {r.pid: r for r in reg.live_sessions()}
        self.assertAlmostEqual(got[10].status_since, 1792062167.406)
        self.assertEqual(got[10].waiting_for, "input needed")
        self.assertIsNone(got[11].status_since)

    def test_skips_unreadable_files_and_key_files(self):
        self.write(10)
        open(os.path.join(self.dir, "12.json"), "w").write("{not json")
        open(os.path.join(self.dir, "10.abc.key"), "w").write("secret")
        reg = FileSessionRegistry(self.dir, pid_alive=lambda pid: True)
        self.assertEqual([r.pid for r in reg.live_sessions()], [10])


class TrustTest(unittest.TestCase):
    def test_reads_the_trust_flag_per_folder_from_claude_json(self):
        p = os.path.join(tempfile.mkdtemp(), ".claude.json")
        with open(p, "w") as f:
            json.dump({"projects": {"/w/ok": {"hasTrustDialogAccepted": True}, "/w/no": {"hasTrustDialogAccepted": False}},
                       "oauthAccount": {"token": "never-read"}}, f)
        t = ClaudeJsonTrust(p)
        self.assertTrue(t.trusted("/w/ok"))
        self.assertFalse(t.trusted("/w/no"))
        self.assertFalse(t.trusted("/w/unknown"))

    def test_a_missing_file_trusts_everything_rather_than_blocking_restarts(self):
        self.assertTrue(ClaudeJsonTrust("/nonexistent/.claude.json").trusted("/w/x"))

    def test_parses_once_until_the_file_changes(self):
        p = os.path.join(tempfile.mkdtemp(), ".claude.json")
        with open(p, "w") as f:
            json.dump({"projects": {"/w/ok": {"hasTrustDialogAccepted": True}}}, f)
        t = ClaudeJsonTrust(p)
        parses = []
        real_load = json.load

        def counting_load(f):
            parses.append(1)
            return real_load(f)

        json.load = counting_load
        try:
            for _ in range(5):
                self.assertTrue(t.trusted("/w/ok"))
            self.assertEqual(len(parses), 1)
            with open(p, "w") as f:
                json.dump({"projects": {}, "padding": "x" * 50}, f)  # new size: re-read
            self.assertFalse(t.trusted("/w/ok"))
            self.assertEqual(len(parses), 2)
        finally:
            json.load = real_load


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_manifest_reads_old_entries_as_auto_and_round_trips_tier(self):
        path = os.path.join(self.dir, "sessions.json")
        with open(path, "w") as f:
            json.dump({"sessions": [{"name": "a", "dir": "/w/a", "conversation": CONV, "flags": [], "enabled": True},
                                    {"name": "b", "dir": "/w/b", "conversation": CONV, "tier": "bogus"}]}, f)
        store = JsonManifestStore(path)
        self.assertEqual([e.tier for e in store.load()], ["auto", "auto"])
        store.save([Entry("a", "/w/a", CONV, tier="keep")])
        self.assertEqual(store.load()[0].tier, "keep")

    def test_state_round_trips_the_parking_fields_and_reads_old_files(self):
        store = JsonStateStore(os.path.join(self.dir, "state.json"))
        h = {"a": History(restarts=(1.0,), last_state=State.OK, note="copy mode", parked_at=5.0, parked_reason="night",
                          last_active=4.0, throttled_pid=99)}
        store.save({"a": "PARKED"}, h, [])
        self.assertEqual(store.load()[1], h)
        with open(store.path, "w") as f:
            json.dump({"conditions": {"a": "OK"}, "histories": {"a": {"restarts": [1.0], "last_state": "OK"}}}, f)
        self.assertEqual(store.load()[1]["a"], History(restarts=(1.0,), last_state=State.OK))

    def test_manifest_round_trips_and_is_private(self):
        store = JsonManifestStore(os.path.join(self.dir, "sessions.json"))
        self.assertEqual(store.load(), [])
        entries = [Entry("a", "/w/a", CONV, FLAGS), Entry("b", "/w/b", OTHER, (), enabled=False)]
        store.save(entries)
        self.assertEqual(store.load(), entries)
        mode = stat.S_IMODE(os.stat(store.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_state_round_trips_conditions_histories_and_queued_messages(self):
        store = JsonStateStore(os.path.join(self.dir, "state.json"))
        self.assertEqual(store.load(), ({}, {}, []))
        h = {"a": History(restarts=(1.0, 2.0), last_state=State.STARTING, idle_unlinked_runs=1, relinked_at=3.0)}
        store.save({"a": "RESTARTED"}, h, ["claude-rc: restarted a"])
        self.assertEqual(store.load(), ({"a": "RESTARTED"}, h, ["claude-rc: restarted a"]))


class HealthStoreTest(unittest.TestCase):
    def test_round_trips_and_a_missing_file_is_healthy(self):
        store = JsonHealthStore(os.path.join(tempfile.mkdtemp(), "health.json"))
        self.assertEqual(store.load(), {})
        h = {"alerts": ChannelHealth(3, 1000.0, "relay: 503", True)}
        store.save(h)
        self.assertEqual(store.load(), h)
        self.assertEqual(stat.S_IMODE(os.stat(store.path).st_mode), 0o600)


class TaskPolicyTest(unittest.TestCase):
    def test_off_unless_enabled(self):
        calls = []
        self.assertFalse(TaskPolicy(run=lambda a: (calls.append(a), (0, ""))[1], enabled=False).background(42, True))
        self.assertEqual(calls, [])

    def test_background_on_and_off(self):
        calls = []
        q = TaskPolicy(run=lambda a: (calls.append(a), (0, ""))[1], enabled=True, binary="/usr/sbin/taskpolicy")
        self.assertTrue(q.background(42, True))
        self.assertTrue(q.background(42, False))
        self.assertEqual(calls, [["/usr/sbin/taskpolicy", "-b", "-p", "42"], ["/usr/sbin/taskpolicy", "-B", "-p", "42"]])

    def test_a_failure_returns_false(self):
        self.assertFalse(TaskPolicy(run=lambda a: (1, "no such process"), enabled=True).background(42, True))

    def test_it_is_on_only_where_taskpolicy_exists(self):
        self.assertEqual(TaskPolicy().enabled, sys.platform == "darwin" and os.path.exists("/usr/sbin/taskpolicy"))


class MacNotificationTest(unittest.TestCase):
    def test_text_is_passed_as_arguments_not_script(self):
        calls = []
        n = MacNotification(run=lambda argv: (calls.append(argv), (0, ""))[1], enabled=True)
        self.assertTrue(n.notify("claude-rc", 'alerts "failing" \\ since 05:00'))
        argv = calls[0]
        self.assertEqual(argv[-2:], ["claude-rc", 'alerts "failing" \\ since 05:00'])
        self.assertNotIn("failing", " ".join(argv[:-2]))  # the text is never part of the script

    def test_off_unless_enabled(self):
        calls = []
        self.assertFalse(MacNotification(run=lambda argv: (calls.append(argv), (0, ""))[1], enabled=False).notify("t", "b"))
        self.assertEqual(calls, [])


class TmuxTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.rc = {}

        def run(argv):
            self.calls.append(argv)
            return self.rc.get(argv[1], 0), "pane text\n"

        self.tmux = TmuxMultiplexer(run=run, path="/usr/bin:/opt/homebrew/bin")

    def test_has_session_matches_the_exact_name(self):
        self.assertTrue(self.tmux.has_session("Ava"))
        self.assertEqual(self.calls[-1], ["tmux", "has-session", "-t", "=Ava"])
        self.rc["has-session"] = 1
        self.assertFalse(self.tmux.has_session("Ava"))

    def test_new_session_is_detached_in_the_folder_with_a_path(self):
        self.tmux.new_session("Ava", "/w/Ava", ("/bin/claude", "--remote-control", "Ava", "--resume", CONV))
        self.assertEqual(self.calls[-1], [
            "tmux", "new-session", "-d", "-s", "Ava", "-c", "/w/Ava", "-e", "PATH=/usr/bin:/opt/homebrew/bin",
            "/bin/claude", "--remote-control", "Ava", "--resume", CONV,
        ])

    def test_send_keys_types_the_text_literally_then_enter(self):
        self.tmux.send_keys("Ava", "/remote-control")
        self.assertEqual(self.calls[-2:], [
            ["tmux", "send-keys", "-t", "=Ava:", "-l", "/remote-control"],
            ["tmux", "send-keys", "-t", "=Ava:", "Enter"],
        ])

    def test_capture_returns_the_last_lines_of_the_pane(self):
        self.assertEqual(self.tmux.capture("Ava", 20), "pane text\n")
        self.assertEqual(self.calls[-1], ["tmux", "capture-pane", "-p", "-t", "=Ava:", "-S", "-20"])

    def test_capture_can_keep_styles(self):
        self.tmux.capture("Ava", 20, ansi=True)
        self.assertEqual(self.calls[-1], ["tmux", "capture-pane", "-p", "-e", "-t", "=Ava:", "-S", "-20"])

    def test_send_keys_does_not_press_enter_when_the_text_failed(self):
        self.rc["send-keys"] = 1
        with self.assertRaises(RuntimeError):
            self.tmux.send_keys("Ava", "/exit")
        self.assertEqual(self.calls, [["tmux", "send-keys", "-t", "=Ava:", "-l", "/exit"]])

    def test_has_session_treats_a_timeout_as_present(self):
        self.rc["has-session"] = 124
        self.assertTrue(self.tmux.has_session("Ava"))  # never restart a session we could not see

    def test_list_sessions_names_none_on_timeout_empty_without_server(self):
        def run(argv):
            return self.rc.get(argv[1], 0), "Ava\nAva-2\n"
        tmux = TmuxMultiplexer(run=run)
        self.assertEqual(tmux.list_sessions(), {"Ava", "Ava-2"})
        self.rc["list-sessions"] = 1
        self.assertEqual(tmux.list_sessions(), set())
        self.rc["list-sessions"] = 124
        self.assertIsNone(tmux.list_sessions())

    def test_pane_info_parses_the_format(self):
        tmux = TmuxMultiplexer(run=lambda argv: (0, "9925|1|0|1|1\n"))
        self.assertEqual(tmux.pane_info("Ava"), PaneInfo(9925, True, False, 1, 1))
        self.assertIsNone(TmuxMultiplexer(run=lambda argv: (0, "garbage\n")).pane_info("Ava"))
        self.assertIsNone(TmuxMultiplexer(run=lambda argv: (1, "")).pane_info("Ava"))

    def test_kill_session_targets_the_exact_name(self):
        self.assertTrue(self.tmux.kill_session("Ava"))
        self.assertEqual(self.calls[-1], ["tmux", "kill-session", "-t", "=Ava"])

    def test_timeouts_are_reported_through_on_error(self):
        seen = []
        tmux = TmuxMultiplexer(run=lambda argv: (124, "timed out after 10s: tmux send-keys"), on_error=seen.append)
        tmux.capture("Ava", 5)
        self.assertEqual(len(seen), 1)
        self.assertIn("capture-pane", seen[0])


class RunTest(unittest.TestCase):
    def test_a_timed_out_command_returns_124_not_raises(self):
        rc, out = run_command(["sleep", "5"], timeout=0.2)
        self.assertEqual(rc, 124)
        self.assertIn("timed out", out)

    def test_a_missing_program_returns_127(self):
        self.assertEqual(run_command(["/nonexistent/claude-rc-test"])[0], 127)


@unittest.skipUnless(shutil.which("tmux"), "tmux not installed")
class RealTmuxTest(unittest.TestCase):
    """Same adapter against a real tmux server on a private socket: proves the target syntax."""

    def setUp(self):
        self.sock = f"claude-rc-test-{os.getpid()}"
        self.tmux = TmuxMultiplexer(socket=self.sock)

    def tearDown(self):
        os.system(f"tmux -L {self.sock} kill-server >/dev/null 2>&1")

    def test_create_find_type_and_read_back(self):
        d = tempfile.mkdtemp()
        self.tmux.new_session("rc-probe", d, ("cat",))
        self.assertTrue(self.tmux.has_session("rc-probe"))
        self.assertFalse(self.tmux.has_session("rc-prob"))   # exact match, no prefix match
        self.tmux.send_keys("rc-probe", "/remote-control")
        for _ in range(20):
            if "/remote-control" in self.tmux.capture("rc-probe", 20):
                break
            time.sleep(0.1)
        self.assertIn("/remote-control", self.tmux.capture("rc-probe", 20))

    def test_list_sessions_pane_info_copy_mode_and_kill(self):
        d = tempfile.mkdtemp()
        self.tmux.new_session("rc-probe", d, ("cat",))
        self.assertIn("rc-probe", self.tmux.list_sessions())
        info = self.tmux.pane_info("rc-probe")
        self.assertEqual((info.in_mode, info.windows, info.panes), (False, 1, 1))
        os.system(f"tmux -L {self.sock} copy-mode -t =rc-probe: >/dev/null 2>&1")
        self.assertTrue(self.tmux.pane_info("rc-probe").in_mode)
        self.assertTrue(self.tmux.kill_session("rc-probe"))
        self.assertFalse(self.tmux.has_session("rc-probe"))
        self.assertIsNone(self.tmux.pane_info("rc-probe"))

    def test_a_taken_name_raises(self):
        d = tempfile.mkdtemp()
        self.tmux.new_session("rc-probe", d, ("cat",))
        with self.assertRaises(RuntimeError):
            self.tmux.new_session("rc-probe", d, ("cat",))


if __name__ == "__main__":
    unittest.main()
