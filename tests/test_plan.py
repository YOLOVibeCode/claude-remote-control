"""plan(): what a watch run does about each host, given what earlier runs remember."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.core import (  # noqa: E402
    NEEDS_YOU,
    Entry,
    History,
    Observation,
    Relink,
    Restart,
    State,
    classify,
    plan,
)

CONV = "11111111-1111-1111-1111-111111111111"
NOW = 1_000_000.0
BIN = "/bin/claude"


def obs(state: State, status: str | None = "idle", name: str = "app", enabled: bool = True, trusted: bool = True) -> Observation:
    e = Entry(name=name, dir=f"/w/{name}", conversation=CONV, flags=("--dangerously-skip-permissions",), enabled=enabled)
    return Observation(entry=e, state=state, status=status, trusted=trusted)


class PlanTest(unittest.TestCase):
    def run_plan(self, *observations, histories=None, now=NOW):
        return plan(list(observations), histories or {}, now, BIN)

    def test_dead_host_is_restarted_on_its_conversation(self):
        d = self.run_plan(obs(State.DEAD))
        self.assertEqual(
            d.actions,
            (Restart(name="app", dir="/w/app", argv=(BIN, "--remote-control", "app", "--resume", CONV, "--dangerously-skip-permissions")),),
        )
        self.assertEqual(d.conditions["app"], "RESTARTED")
        self.assertEqual(d.histories["app"].restarts, (NOW,))

    def test_a_restart_is_remembered_as_starting_so_a_late_check_reports_failure(self):
        # The watch runs every 10 minutes, longer than the 90s grace. The host must not look
        # merely DEAD next time (which would restart it again); it must look RESTART_FAILED.
        d = self.run_plan(obs(State.DEAD))
        h = d.histories["app"]
        self.assertEqual(h.last_state, State.STARTING)
        e = obs(State.DEAD).entry
        self.assertEqual(classify(e, has_session=False, record=None, history=h, now=NOW + 600), State.RESTART_FAILED)

    def test_restarts_back_off_after_three_in_an_hour(self):
        h = {"app": History(restarts=(NOW - 3000, NOW - 2000, NOW - 1000))}
        d = self.run_plan(obs(State.DEAD), histories=h)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "GAVE_UP")
        self.assertIn("GAVE_UP", NEEDS_YOU)

    def test_restarts_older_than_an_hour_do_not_count(self):
        h = {"app": History(restarts=(NOW - 7200, NOW - 5000, NOW - 4000))}
        d = self.run_plan(obs(State.DEAD), histories=h)
        self.assertEqual(len(d.actions), 1)
        self.assertEqual(d.histories["app"].restarts, (NOW,))

    def test_a_dead_host_in_an_untrusted_folder_is_not_restarted(self):
        # claude would stop at "Do you trust this folder?" and never register.
        d = self.run_plan(obs(State.DEAD, status=None, trusted=False))
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "UNTRUSTED")
        self.assertIn("UNTRUSTED", NEEDS_YOU)

    def test_an_untrusted_folder_does_not_matter_while_the_host_runs(self):
        d = self.run_plan(obs(State.OK, trusted=False))
        self.assertEqual(d.conditions["app"], "OK")

    def test_restart_failed_is_reported_not_retried(self):
        d = self.run_plan(obs(State.RESTART_FAILED, status=None))
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "RESTART_FAILED")
        self.assertIn("RESTART_FAILED", NEEDS_YOU)

    def test_unlinked_and_idle_waits_one_run_before_relinking(self):
        d1 = self.run_plan(obs(State.UNLINKED, status="idle"))
        self.assertEqual(d1.actions, ())
        self.assertEqual(d1.conditions["app"], "UNLINKED")
        d2 = self.run_plan(obs(State.UNLINKED, status="idle"), histories=d1.histories)
        self.assertEqual(d2.actions, (Relink(name="app"),))
        self.assertEqual(d2.histories["app"].relinked_at, NOW)

    def test_never_relinks_a_busy_host(self):
        h = {"app": History(idle_unlinked_runs=5)}
        d = self.run_plan(obs(State.UNLINKED, status="busy"), histories=h)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.histories["app"].idle_unlinked_runs, 0)

    def test_still_unlinked_after_a_relink_needs_you_and_is_not_retyped(self):
        h = {"app": History(idle_unlinked_runs=3, relinked_at=NOW - 600)}
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories=h)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "UNLINKED_STUCK")
        self.assertIn("UNLINKED_STUCK", NEEDS_YOU)

    def test_ok_clears_relink_memory(self):
        h = {"app": History(idle_unlinked_runs=3, relinked_at=NOW - 600)}
        d = self.run_plan(obs(State.OK), histories=h)
        self.assertEqual(d.histories["app"].relinked_at, None)
        self.assertEqual(d.histories["app"].idle_unlinked_runs, 0)
        self.assertEqual(d.conditions["app"], "OK")

    def test_wrong_conversation_and_not_running_are_reported_never_touched(self):
        d = self.run_plan(obs(State.WRONG_CONVERSATION, name="a"), obs(State.NOT_RUNNING, status=None, name="b"))
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions, {"a": "WRONG_CONVERSATION", "b": "NOT_RUNNING"})
        self.assertTrue({"WRONG_CONVERSATION", "NOT_RUNNING"} <= NEEDS_YOU)

    def test_disabled_and_starting_do_nothing_and_need_nothing(self):
        d = self.run_plan(obs(State.DISABLED, name="a", enabled=False), obs(State.STARTING, status=None, name="b"))
        self.assertEqual(d.actions, ())
        self.assertFalse({d.conditions["a"], d.conditions["b"]} & NEEDS_YOU)

    def test_last_state_is_recorded_for_every_host(self):
        d = self.run_plan(obs(State.OK, name="a"), obs(State.UNLINKED, name="b"))
        self.assertEqual(d.histories["a"].last_state, State.OK)
        self.assertEqual(d.histories["b"].last_state, State.UNLINKED)


if __name__ == "__main__":
    unittest.main()
