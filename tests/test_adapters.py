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

from claude_rc.adapters.files import JsonManifestStore, JsonStateStore  # noqa: E402
from claude_rc.adapters.registry import FileSessionRegistry  # noqa: E402
from claude_rc.adapters.tmux import TmuxMultiplexer  # noqa: E402
from claude_rc.adapters.trust import ClaudeJsonTrust  # noqa: E402
from claude_rc.core import Entry, History, Record, State, adopt  # noqa: E402

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


class FilesTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

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

    def test_a_taken_name_raises(self):
        d = tempfile.mkdtemp()
        self.tmux.new_session("rc-probe", d, ("cat",))
        with self.assertRaises(RuntimeError):
            self.tmux.new_session("rc-probe", d, ("cat",))


if __name__ == "__main__":
    unittest.main()
