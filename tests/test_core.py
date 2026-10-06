"""Pure decision logic: no tmux, no claude, no network."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.core import (  # noqa: E402
    Entry,
    History,
    Record,
    State,
    classify,
    restart_argv,
)

CONV = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"
NOW = 1_000_000.0


def entry(**kw) -> Entry:
    base = dict(name="app", dir="/w/app", conversation=CONV, flags=("--dangerously-skip-permissions",), enabled=True)
    base.update(kw)
    return Entry(**base)


def record(**kw) -> Record:
    base = dict(pid=42, conversation=CONV, cwd="/w/app", tmux_session="app", status="idle", linked=True, version="2.1.291", alive=True)
    base.update(kw)
    return Record(**base)


class ClassifyTest(unittest.TestCase):
    def test_ok_when_alive_on_its_conversation_and_linked(self):
        self.assertEqual(classify(entry(), has_session=True, record=record(), history=History(), now=NOW), State.OK)

    def test_dead_when_the_tmux_session_is_gone(self):
        self.assertEqual(classify(entry(), has_session=False, record=None, history=History(), now=NOW), State.DEAD)

    def test_not_running_when_tmux_session_has_no_live_claude(self):
        # A session we did not start as `tmux new-session … claude` (user typed claude in a shell):
        # its pane may hold anything, so it is never restarted, only reported.
        self.assertEqual(classify(entry(), True, None, History(), NOW), State.NOT_RUNNING)
        self.assertEqual(classify(entry(), True, record(alive=False), History(), NOW), State.NOT_RUNNING)

    def test_wrong_conversation_when_alive_on_another_conversation(self):
        self.assertEqual(classify(entry(), True, record(conversation=OTHER), History(), NOW), State.WRONG_CONVERSATION)

    def test_unlinked_when_remote_control_is_off(self):
        self.assertEqual(classify(entry(), True, record(linked=False), History(), NOW), State.UNLINKED)

    def test_starting_inside_the_grace_period_after_a_restart(self):
        h = History(restarts=(NOW - 30,))
        self.assertEqual(classify(entry(), True, None, h, NOW), State.STARTING)
        self.assertEqual(classify(entry(), False, None, h, NOW), State.STARTING)

    def test_restart_failed_once_the_grace_period_passes_without_a_live_record(self):
        h = History(restarts=(NOW - 120,), last_state=State.STARTING)
        self.assertEqual(classify(entry(), True, None, h, NOW), State.RESTART_FAILED)
        self.assertEqual(classify(entry(), False, None, h, NOW), State.RESTART_FAILED)

    def test_a_restart_that_came_up_on_the_right_conversation_is_ok(self):
        h = History(restarts=(NOW - 30,))
        self.assertEqual(classify(entry(), True, record(), h, NOW), State.OK)

    def test_disabled_entries_are_never_judged(self):
        self.assertEqual(classify(entry(enabled=False), False, None, History(), NOW), State.DISABLED)


class RestartArgvTest(unittest.TestCase):
    def test_resumes_the_pinned_conversation_with_remote_control_under_its_name(self):
        self.assertEqual(
            restart_argv(entry(), claude_bin="/bin/claude"),
            ["/bin/claude", "--remote-control", "app", "--resume", CONV, "--dangerously-skip-permissions"],
        )

    def test_per_session_flags_are_kept_in_order(self):
        e = entry(flags=("--model", "opus"))
        self.assertEqual(restart_argv(e, "/bin/claude")[-2:], ["--model", "opus"])


if __name__ == "__main__":
    unittest.main()
