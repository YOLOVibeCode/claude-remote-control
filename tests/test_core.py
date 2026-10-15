"""Pure decision logic: no tmux, no claude, no network."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.core import (  # noqa: E402
    DOOR_KEY,
    ChannelHealth,
    DoorConfig,
    DoorObservation,
    Entry,
    PaneInfo,
    Policy,
    StartDoor,
    Throttle,
    History,
    Record,
    State,
    classify,
    coalesce_queue,
    door_argv,
    input_is_empty,
    is_door_command,
    is_night,
    parse_door,
    parse_policy,
    plan_door,
    restart_argv,
    stop_blocker,
    track_delivery,
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

    def test_a_failed_restart_is_retried_once_its_restart_is_an_hour_old(self):
        h = History(restarts=(NOW - 3700,), last_state=State.RESTART_FAILED)
        self.assertEqual(classify(entry(), False, None, h, NOW), State.DEAD)

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


class InputIsEmptyTest(unittest.TestCase):
    """Captured with `tmux capture-pane -p -e` from real Claude Code 2.1.289/2.1.291 panes."""

    PANE = "⏺ done\n\n────\n{line}\n────\n  ⏵⏵ bypass permissions on\n"

    def pane(self, line):
        return self.PANE.format(line=line)

    def test_an_empty_input_box(self):
        self.assertTrue(input_is_empty(self.pane("\x1b[39m❯\xa0")))

    def test_a_dim_suggestion_is_still_empty(self):
        self.assertTrue(input_is_empty(self.pane("\x1b[39m❯\xa0\x1b[2mpromote develop to main\x1b[0m")))

    def test_a_typed_draft_is_not_empty(self):
        self.assertFalse(input_is_empty(self.pane("\x1b[39m❯\xa0mzbm")))
        self.assertFalse(input_is_empty(self.pane("\x1b[39m❯\xa0I want you to understand the nature of this application.")))

    def test_a_draft_followed_by_an_autocomplete_hint_is_not_empty(self):
        self.assertFalse(input_is_empty(self.pane("\x1b[39m❯\xa0prom\x1b[2mote develop to main\x1b[0m")))

    def test_only_the_last_prompt_line_counts(self):
        pane = "❯ an earlier message\n⏺ reply\n" + self.pane("\x1b[39m❯\xa0")
        self.assertTrue(input_is_empty(pane))

    def test_a_menu_cursor_is_not_an_input_box(self):
        self.assertFalse(input_is_empty(" Quick safety check\n ❯ No, exit\n   Yes, I trust this folder\n"))

    def test_no_prompt_at_all_is_not_safe_to_type_into(self):
        self.assertFalse(input_is_empty("$ \n"))


class ParkedClassifyTest(unittest.TestCase):
    def test_parked_when_history_says_parked_and_nothing_runs(self):
        h = History(parked_at=NOW - 60, parked_reason="idle")
        self.assertEqual(classify(entry(), has_session=False, record=None, history=h, now=NOW), State.PARKED)
        self.assertEqual(classify(entry(), has_session=True, record=None, history=h, now=NOW), State.PARKED)

    def test_parked_beats_restart_failed_and_starting(self):
        h = History(restarts=(NOW - 10,), last_state=State.STARTING, parked_at=NOW - 5)
        self.assertEqual(classify(entry(), has_session=False, record=None, history=h, now=NOW), State.PARKED)

    def test_a_parked_host_running_on_its_conversation_is_ok(self):
        h = History(parked_at=NOW - 60)
        self.assertEqual(classify(entry(), has_session=True, record=record(), history=h, now=NOW), State.OK)

    def test_disabled_beats_parked(self):
        h = History(parked_at=NOW - 60)
        self.assertEqual(classify(entry(enabled=False), has_session=False, record=None, history=h, now=NOW), State.DISABLED)


EMPTY = "\x1b[39m❯\xa0\n"


class StopBlockerTest(unittest.TestCase):
    def fresh(self, **kw):
        base = dict(status_since=NOW - 3600)
        base.update(kw)
        return record(**base)

    def pane(self, **kw):
        base = dict(pid=42, in_mode=False, dead=False, windows=1, panes=1)
        base.update(kw)
        return PaneInfo(**base)

    def blocker(self, fresh=None, pane=None, text=EMPTY, since=NOW - 3600):
        return stop_blocker(self.fresh() if fresh is None else fresh, self.pane() if pane is None else pane, text, 42, since)

    def test_a_clean_idle_pane_has_no_blocker(self):
        self.assertIsNone(self.blocker())

    def test_an_exited_process_is_gone(self):
        self.assertEqual(stop_blocker(None, self.pane(), EMPTY, 42, NOW), "gone")
        self.assertEqual(self.blocker(fresh=self.fresh(alive=False)), "gone")

    def test_busy_or_waiting_blocks(self):
        self.assertEqual(self.blocker(fresh=self.fresh(status="busy")), "became busy")
        self.assertEqual(self.blocker(fresh=self.fresh(status="waiting")), "became waiting")

    def test_activity_since_the_plan_blocks(self):
        self.assertEqual(self.blocker(fresh=self.fresh(status_since=NOW - 10), since=NOW - 3600), "became active")

    def test_other_windows_or_panes_block(self):
        self.assertEqual(self.blocker(pane=self.pane(windows=2)), "session has other windows or panes")
        self.assertEqual(self.blocker(pane=self.pane(panes=2)), "session has other windows or panes")

    def test_copy_mode_blocks(self):
        self.assertEqual(self.blocker(pane=self.pane(in_mode=True)), "copy mode")

    def test_claude_must_be_the_pane_process(self):
        self.assertEqual(self.blocker(pane=self.pane(pid=7)), "claude is not the pane's own process")

    def test_a_draft_blocks(self):
        self.assertEqual(self.blocker(text="\x1b[39m❯\xa0half-written\n"), "text in its input box")

    def test_no_pane_blocks(self):
        self.assertEqual(stop_blocker(self.fresh(), None, "", 42, NOW - 3600), "no pane")


class PolicyParseTest(unittest.TestCase):
    def test_a_missing_or_non_object_section_is_off_with_defaults(self):
        self.assertEqual(parse_policy(None), (Policy(), []))
        policy, warnings = parse_policy("on")
        self.assertEqual(policy, Policy())
        self.assertEqual(len(warnings), 1)

    def test_valid_values_are_taken(self):
        policy, warnings = parse_policy({"enabled": True, "night_start_hour": 23, "idle_park_hours": 4, "max_live": 3})
        self.assertEqual(warnings, [])
        self.assertEqual((policy.enabled, policy.night_start_hour, policy.idle_park_hours, policy.max_live), (True, 23, 4.0, 3))

    def test_each_bad_value_falls_back_with_one_warning(self):
        policy, warnings = parse_policy({"enabled": "yes", "night_start_hour": 24, "max_live": True,
                                         "idle_park_hours": "8", "night_idle_minutes": -1})
        self.assertEqual(policy, Policy())
        self.assertEqual(len(warnings), 5)

    def test_unknown_keys_warn_and_comments_are_ignored(self):
        policy, warnings = parse_policy({"enabeld": True, "_comment": "night = 00-06"})
        self.assertFalse(policy.enabled)
        self.assertEqual(warnings, ["policy.enabeld: unknown setting, ignored"])


class IsNightTest(unittest.TestCase):
    def test_the_default_window_is_midnight_to_six(self):
        self.assertEqual([h for h in range(24) if is_night(Policy(), h)], [0, 1, 2, 3, 4, 5])

    def test_the_window_can_wrap_past_midnight(self):
        p = Policy(night_start_hour=23, night_end_hour=6)
        self.assertEqual([h for h in range(24) if is_night(p, h)], [0, 1, 2, 3, 4, 5, 23])

    def test_equal_bounds_is_never_night(self):
        self.assertFalse(any(is_night(Policy(night_start_hour=3, night_end_hour=3), h) for h in range(24)))


class DoorConfigTest(unittest.TestCase):
    def test_defaults_ask_before_commands(self):
        d, warnings = parse_door(None)
        self.assertEqual((d.enabled, d.session, d.spawn, d.capacity, d.permission_mode), (False, "remote", "same-dir", 2, "acceptEdits"))
        self.assertEqual(warnings, [])

    def test_bad_values_fall_back(self):
        d, warnings = parse_door({"spawn": "everywhere", "capacity": 99, "session": "re mote", "permission_mode": "a b"})
        self.assertEqual(d, DoorConfig())
        self.assertEqual(len(warnings), 4)

    def test_argv_is_ccserve_plus_spawn_and_capacity(self):
        self.assertEqual(door_argv(DoorConfig(), "/bin/claude", "mac"),
                         ["/bin/claude", "remote-control", "--name", "mac", "--spawn", "same-dir", "--capacity", "2",
                          "--permission-mode", "acceptEdits"])

    def test_session_spawn_has_no_capacity(self):
        self.assertNotIn("--capacity", door_argv(DoorConfig(spawn="session"), "claude", "mac"))

    def test_only_the_server_command_is_the_door(self):
        self.assertTrue(is_door_command("/Users/x/.local/bin/claude remote-control --name mac"))
        self.assertFalse(is_door_command("/Users/x/.local/bin/claude --remote-control remote-control"))
        self.assertFalse(is_door_command("-zsh"))
        self.assertFalse(is_door_command(""))


class PlanDoorTest(unittest.TestCase):
    ON = DoorConfig(enabled=True)
    ARGV = ["claude", "remote-control"]

    def test_disabled_has_no_condition(self):
        self.assertEqual(plan_door(DoorConfig(), DoorObservation(False, None), History(), NOW, self.ARGV, False),
                         ((), None, History()))

    def test_running_is_ok(self):
        actions, cond, _ = plan_door(self.ON, DoorObservation(True, 77), History(), NOW, self.ARGV, False)
        self.assertEqual((actions, cond), ((), "OK"))

    def test_down_is_started_with_its_argv(self):
        actions, cond, h = plan_door(self.ON, DoorObservation(False, None), History(), NOW, self.ARGV, False)
        self.assertEqual((actions, cond, h.restarts), ((StartDoor("remote", "~/Dev", tuple(self.ARGV)),), "RESTARTED", (NOW,)))

    def test_backs_off_after_three_an_hour(self):
        h = History(restarts=(NOW - 3000, NOW - 2000, NOW - 1000), last_state=State.OK)
        self.assertEqual(plan_door(self.ON, DoorObservation(False, None), h, NOW, self.ARGV, False)[1], "GAVE_UP")

    def test_a_session_running_something_else_is_reported_never_touched(self):
        actions, cond, _ = plan_door(self.ON, DoorObservation(True, None), History(), NOW, self.ARGV, False)
        self.assertEqual((actions, cond), ((), "NOT_RUNNING"))

    def test_an_untrusted_folder_is_not_started(self):
        self.assertEqual(plan_door(self.ON, DoorObservation(False, None, trusted=False), History(), NOW, self.ARGV, False)[1],
                         "UNTRUSTED")

    def test_background_at_night_and_back_by_day(self):
        actions, _, h = plan_door(self.ON, DoorObservation(True, 77), History(), NOW, self.ARGV, True)
        self.assertEqual(actions, (Throttle(DOOR_KEY, 77, True),))
        actions, _, h = plan_door(self.ON, DoorObservation(True, 77), h, NOW, self.ARGV, False)
        self.assertEqual(actions, (Throttle(DOOR_KEY, 77, False),))


class DeliveryHealthTest(unittest.TestCase):
    def test_first_failure_is_failing_then_quiet(self):
        h, ev = track_delivery(ChannelHealth(), "relay: 503", NOW)
        self.assertEqual((h.failures, h.since, ev), (1, NOW, "failing"))
        h, ev = track_delivery(h, "relay: 503", NOW + 600)
        self.assertEqual((h.failures, h.since, ev), (2, NOW, None))

    def test_escalates_once_at_the_threshold(self):
        h = ChannelHealth()
        events = []
        for i in range(6):
            h, ev = track_delivery(h, "relay: 503", NOW + 600 * i)
            events.append(ev)
        self.assertEqual(events, ["failing", None, "escalate", None, None, None])
        self.assertTrue(h.escalated)

    def test_success_after_failures_recovers_and_resets(self):
        h, _ = track_delivery(ChannelHealth(), "x", NOW)
        h, ev = track_delivery(h, None, NOW + 600)
        self.assertEqual((h, ev), (ChannelHealth(), "recovered"))

    def test_success_when_healthy_is_silent(self):
        self.assertEqual(track_delivery(ChannelHealth(), None, NOW), (ChannelHealth(), None))

    def test_a_threshold_of_one_escalates_the_first_failure(self):
        self.assertEqual(track_delivery(ChannelHealth(), "smtp down", NOW, after=1)[1], "escalate")


class CoalesceQueueTest(unittest.TestCase):
    def test_under_the_limit_is_unchanged(self):
        self.assertEqual(coalesce_queue(["a", "b"], 3), (["a", "b"], []))

    def test_over_the_limit_keeps_the_newest_with_a_summary(self):
        kept, dropped = coalesce_queue([str(i) for i in range(10)], 4)
        self.assertEqual(kept, ["claude-rc: 7 earlier alerts not delivered (see watch.log)", "7", "8", "9"])
        self.assertEqual(dropped, [str(i) for i in range(7)])


if __name__ == "__main__":
    unittest.main()
