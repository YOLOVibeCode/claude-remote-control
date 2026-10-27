"""plan(): what a watch run does about each host, given what earlier runs remember."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.core import (  # noqa: E402
    MAX_RELINKS,
    NEEDS_YOU,
    RELINK_RETRY_S,
    Entry,
    History,
    Observation,
    Policy,
    Relink,
    Restart,
    State,
    Stop,
    Throttle,
    classify,
    plan,
    relink_not_typed,
)

CONV = "11111111-1111-1111-1111-111111111111"
NOW = 1_000_000.0
BIN = "/bin/claude"


def obs(state: State, status: str | None = "idle", name: str = "app", enabled: bool = True, trusted: bool = True,
        tier: str = "auto", pid: int | None = 42, idle_s: float | None = None) -> Observation:
    e = Entry(name=name, dir=f"/w/{name}", conversation=CONV, flags=("--dangerously-skip-permissions",), enabled=enabled,
              tier=tier)
    live = state in (State.OK, State.UNLINKED)
    return Observation(entry=e, state=state, status=status, trusted=trusted, pid=pid if live else None,
                       status_since=None if idle_s is None else NOW - idle_s)


ON = Policy(enabled=True)
H = 3600


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

    def test_a_stuck_link_is_retyped_an_hour_later_and_stays_stuck(self):
        h = {"app": History(idle_unlinked_runs=8, relinked_at=NOW - RELINK_RETRY_S, relinks=1)}
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories=h)
        self.assertEqual(d.actions, (Relink(name="app"),))
        self.assertEqual(d.conditions["app"], "UNLINKED_STUCK")   # not "UNLINKED": that would read as recovered
        self.assertEqual((d.histories["app"].relinked_at, d.histories["app"].relinks), (NOW, 2))

    def test_relink_retries_stop_at_the_limit(self):
        h = {"app": History(idle_unlinked_runs=40, relinked_at=NOW - 5 * H, relinks=MAX_RELINKS)}
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories=h)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "UNLINKED_STUCK")

    def test_a_retry_waits_for_two_idle_runs(self):
        h = {"app": History(idle_unlinked_runs=8, relinked_at=NOW - 2 * H, relinks=1)}
        d = self.run_plan(obs(State.UNLINKED, status="busy"), histories=h)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "UNLINKED_STUCK")
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories=d.histories)
        self.assertEqual(d.actions, ())
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories=d.histories)
        self.assertEqual(d.actions, (Relink(name="app"),))

    def test_a_relink_that_was_not_typed_is_tried_again_next_run(self):
        first = relink_not_typed(History(relinked_at=NOW, relinks=1, idle_unlinked_runs=2))
        self.assertEqual((first.relinked_at, first.relinks), (None, 0))
        retry = relink_not_typed(History(relinked_at=NOW, relinks=3, idle_unlinked_runs=9))
        self.assertEqual(retry.relinks, 2)
        d = self.run_plan(obs(State.UNLINKED, status="idle"), histories={"app": retry})
        self.assertEqual(d.actions, (Relink(name="app"),))
        self.assertEqual(d.conditions["app"], "UNLINKED_STUCK")

    def test_ok_clears_relink_memory(self):
        h = {"app": History(idle_unlinked_runs=3, relinked_at=NOW - 600, relinks=2)}
        d = self.run_plan(obs(State.OK), histories=h)
        self.assertEqual(d.histories["app"].relinked_at, None)
        self.assertEqual(d.histories["app"].relinks, 0)
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


class ParkedPlanTest(unittest.TestCase):
    def test_a_parked_host_is_left_alone_and_needs_nothing(self):
        h = {"app": History(parked_at=NOW - 600, parked_reason="idle")}
        d = plan([obs(State.PARKED, status=None)], h, NOW, BIN)
        self.assertEqual(d.actions, ())
        self.assertEqual(d.conditions["app"], "PARKED")
        self.assertEqual(d.histories["app"].parked_reason, "idle")
        self.assertNotIn("PARKED", NEEDS_YOU)

    def test_back_on_its_conversation_clears_parked(self):
        h = {"app": History(parked_at=NOW - 600, parked_reason="manual")}
        d = plan([obs(State.OK)], h, NOW, BIN)
        self.assertIsNone(d.histories["app"].parked_at)
        self.assertIsNone(d.histories["app"].parked_reason)

    def test_wrong_conversation_keeps_the_parked_memory(self):
        h = {"app": History(parked_at=NOW - 600, parked_reason="manual")}
        d = plan([obs(State.WRONG_CONVERSATION)], h, NOW, BIN)
        self.assertEqual(d.histories["app"].parked_reason, "manual")


def stops(d):
    return [a for a in d.actions if isinstance(a, Stop)]


def restarts(d):
    return [a for a in d.actions if isinstance(a, Restart)]


def throttles(d):
    return [a for a in d.actions if isinstance(a, Throttle)]


class DayPolicyTest(unittest.TestCase):
    def test_policy_off_never_parks(self):
        self.assertEqual(stops(plan([obs(State.OK, idle_s=30 * H)], {}, NOW, BIN)), [])

    def test_day_parks_a_host_idle_past_the_limit(self):
        d = plan([obs(State.OK, idle_s=9 * H)], {}, NOW, BIN, ON)
        self.assertEqual(stops(d), [Stop("app", 42, "idle", NOW - 9 * H, State.OK, "OK")])
        self.assertEqual(d.conditions["app"], "PARKED")
        self.assertEqual((d.histories["app"].parked_at, d.histories["app"].parked_reason), (NOW, "idle"))

    def test_day_keeps_a_host_idle_under_the_limit(self):
        d = plan([obs(State.OK, idle_s=7 * H)], {}, NOW, BIN, ON)
        self.assertEqual((stops(d), d.conditions["app"]), ([], "OK"))

    def test_never_parks_busy_waiting_or_unknown(self):
        for o in (obs(State.OK, status="busy"), obs(State.OK, status="waiting"), obs(State.OK, idle_s=None)):
            self.assertEqual(stops(plan([o], {}, NOW, BIN, ON)), [], o.status)

    def test_never_parks_keep_tier(self):
        self.assertEqual(stops(plan([obs(State.OK, tier="keep", idle_s=99 * H)], {}, NOW, BIN, ON)), [])

    def test_budget_parks_the_least_recently_active_first(self):
        d = plan([obs(State.OK, name="a", idle_s=2 * H), obs(State.OK, name="b", idle_s=3 * H),
                  obs(State.OK, name="c", idle_s=4 * H)], {}, NOW, BIN, Policy(enabled=True, max_live=2))
        self.assertEqual([(s.name, s.reason) for s in stops(d)], [("c", "budget")])

    def test_budget_never_cuts_a_host_used_in_the_last_hour(self):
        d = plan([obs(State.OK, name="a", idle_s=600), obs(State.OK, name="b", idle_s=1200)], {}, NOW, BIN,
                 Policy(enabled=True, max_live=1))
        self.assertEqual(stops(d), [])

    def test_keep_and_busy_count_against_the_budget(self):
        d = plan([obs(State.OK, name="k", tier="keep", idle_s=H), obs(State.OK, name="b", status="busy"),
                  obs(State.OK, name="i", idle_s=2 * H)], {}, NOW, BIN, Policy(enabled=True, max_live=2))
        self.assertEqual([(s.name, s.reason) for s in stops(d)], [("i", "budget")])

    def test_parking_an_unlinked_host_does_not_relink_it(self):
        d = plan([obs(State.UNLINKED, idle_s=9 * H)], {"app": History(idle_unlinked_runs=5)}, NOW, BIN, ON)
        self.assertEqual([type(a).__name__ for a in d.actions], ["Stop"])
        self.assertEqual(stops(d)[0].fallback, "UNLINKED")

    def test_a_dead_host_with_unknown_activity_is_parked_not_restarted(self):
        d = plan([obs(State.DEAD)], {}, NOW, BIN, ON)
        self.assertEqual((restarts(d), d.conditions["app"], d.histories["app"].parked_reason), ([], "PARKED", "idle"))

    def test_a_dead_host_used_recently_is_restarted(self):
        d = plan([obs(State.DEAD)], {"app": History(last_active=NOW - H)}, NOW, BIN, ON)
        self.assertEqual(len(restarts(d)), 1)

    def test_morning_does_not_revive_by_default(self):
        h = {"app": History(parked_at=NOW - 6 * H, parked_reason="night", last_active=NOW - 7 * H)}
        self.assertEqual(restarts(plan([obs(State.PARKED, status=None)], h, NOW, BIN, ON)), [])

    def test_morning_revive_restarts_night_parked_only(self):
        pol = Policy(enabled=True, morning_revive=True)
        night = {"app": History(parked_at=NOW - 6 * H, parked_reason="night", last_active=NOW - 7 * H)}
        manual = {"app": History(parked_at=NOW - 6 * H, parked_reason="manual", last_active=NOW - 7 * H)}
        self.assertEqual(len(restarts(plan([obs(State.PARKED, status=None)], night, NOW, BIN, pol))), 1)
        self.assertEqual(restarts(plan([obs(State.PARKED, status=None)], manual, NOW, BIN, pol)), [])

    def test_last_active_is_now_when_busy_and_idle_since_when_idle(self):
        d = plan([obs(State.OK, name="b", status="busy"), obs(State.OK, name="i", idle_s=H)], {}, NOW, BIN, ON)
        self.assertEqual(d.histories["b"].last_active, NOW)
        self.assertEqual(d.histories["i"].last_active, NOW - H)


class NightPolicyTest(unittest.TestCase):
    def test_night_parks_idle_past_night_minutes_but_not_recent_work(self):
        d = plan([obs(State.OK, name="old", idle_s=40 * 60), obs(State.OK, name="new", idle_s=10 * 60)],
                 {}, NOW, BIN, ON, night=True)
        self.assertEqual([(s.name, s.reason) for s in stops(d)], [("old", "night")])

    def test_night_does_not_restart_a_host_idle_for_hours(self):
        d = plan([obs(State.DEAD)], {"app": History(last_active=NOW - 3 * H)}, NOW, BIN, ON, night=True)
        self.assertEqual((restarts(d), d.conditions["app"]), ([], "PARKED"))

    def test_night_restarts_a_host_that_died_while_in_use(self):
        d = plan([obs(State.DEAD)], {"app": History(last_active=NOW - 300)}, NOW, BIN, ON, night=True)
        self.assertEqual(len(restarts(d)), 1)

    def test_a_dead_keep_host_is_restarted_at_night(self):
        d = plan([obs(State.DEAD, tier="keep")], {}, NOW, BIN, ON, night=True)
        self.assertEqual(len(restarts(d)), 1)

    def test_night_throttles_an_idle_survivor_once(self):
        d1 = plan([obs(State.OK, tier="keep", idle_s=2 * H)], {}, NOW, BIN, ON, night=True)
        self.assertEqual(throttles(d1), [Throttle("app", 42, True)])
        d2 = plan([obs(State.OK, tier="keep", idle_s=2 * H)], d1.histories, NOW + 600, BIN, ON, night=True)
        self.assertEqual(throttles(d2), [])

    def test_day_restores_normal_priority(self):
        d = plan([obs(State.OK, tier="keep", idle_s=2 * H)], {"app": History(throttled_pid=42)}, NOW, BIN, ON)
        self.assertEqual(throttles(d), [Throttle("app", 42, False)])

    def test_a_new_process_is_not_considered_throttled(self):
        d = plan([obs(State.OK, tier="keep", idle_s=2 * H)], {"app": History(throttled_pid=7)}, NOW, BIN, ON)
        self.assertEqual((throttles(d), d.histories["app"].throttled_pid), ([], None))

    def test_an_active_survivor_is_not_throttled_at_night(self):
        self.assertEqual(throttles(plan([obs(State.OK, tier="keep", status="busy")], {}, NOW, BIN, ON, night=True)), [])

    def test_turning_the_policy_off_releases_a_leftover_throttle(self):
        d = plan([obs(State.OK, idle_s=2 * H)], {"app": History(throttled_pid=42)}, NOW, BIN, Policy(), night=True)
        self.assertEqual(throttles(d), [Throttle("app", 42, False)])

    def test_a_host_parked_at_night_also_drops_to_background_in_case_the_stop_is_blocked(self):
        d = plan([obs(State.OK, idle_s=2 * H)], {}, NOW, BIN, ON, night=True)
        self.assertEqual([type(a).__name__ for a in d.actions], ["Stop", "Throttle"])


if __name__ == "__main__":
    unittest.main()
