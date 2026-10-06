"""diff_alerts() and format_report(): what reaches the phone and the inbox."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from claude_rc.core import ReportRow, diff_alerts, format_report  # noqa: E402

CONV = "11111111-1111-1111-1111-111111111111"


class DiffAlertsTest(unittest.TestCase):
    def test_nothing_changed_sends_nothing(self):
        self.assertIsNone(diff_alerts({"a": "OK", "b": "UNLINKED"}, {"a": "OK", "b": "UNLINKED"}))

    def test_first_run_with_everything_fine_sends_nothing(self):
        self.assertIsNone(diff_alerts({}, {"a": "OK", "b": "STARTING"}))

    def test_a_restart_is_reported(self):
        self.assertEqual(diff_alerts({"a": "OK"}, {"a": "RESTARTED"}), "claude-rc: restarted a")

    def test_coming_back_after_a_restart_is_reported_once(self):
        self.assertEqual(diff_alerts({"a": "RESTARTED"}, {"a": "OK"}), "claude-rc: back a")
        self.assertIsNone(diff_alerts({"a": "OK"}, {"a": "OK"}))

    def test_a_new_problem_is_reported_once_with_its_reason(self):
        self.assertEqual(
            diff_alerts({"Dev": "OK"}, {"Dev": "WRONG_CONVERSATION"}),
            "claude-rc: needs you Dev (wrong conversation)",
        )
        self.assertIsNone(diff_alerts({"Dev": "WRONG_CONVERSATION"}, {"Dev": "WRONG_CONVERSATION"}))

    def test_a_problem_changing_kind_is_reported_again(self):
        self.assertEqual(
            diff_alerts({"a": "RESTART_FAILED"}, {"a": "GAVE_UP"}),
            "claude-rc: needs you a (gave up after 3 restarts this hour)",
        )

    def test_untrusted_alert_says_how_to_fix_it(self):
        self.assertEqual(
            diff_alerts({"admin": "OK"}, {"admin": "UNTRUSTED"}),
            "claude-rc: needs you admin (folder not trusted: open claude there once and accept)",
        )

    def test_recovery_from_a_problem_is_reported(self):
        self.assertEqual(diff_alerts({"a": "UNLINKED_STUCK"}, {"a": "OK"}), "claude-rc: back a")

    def test_several_changes_share_one_message(self):
        msg = diff_alerts(
            {"Ava-2": "OK", "CF": "OK", "Dev": "OK", "x": "RESTARTED"},
            {"Ava-2": "RESTARTED", "CF": "RESTARTED", "Dev": "NOT_RUNNING", "x": "OK"},
        )
        self.assertEqual(msg, "claude-rc: restarted Ava-2, CF; needs you Dev (claude not running in its tmux session); back x")

    def test_a_host_removed_from_the_manifest_is_not_reported(self):
        self.assertIsNone(diff_alerts({"gone": "WRONG_CONVERSATION"}, {}))

    def test_long_messages_are_capped_for_sms(self):
        prev = {f"host-{i:02d}": "OK" for i in range(40)}
        now = {k: "RESTARTED" for k in prev}
        msg = diff_alerts(prev, now)
        self.assertLessEqual(len(msg), 300)
        self.assertTrue(msg.endswith("more"))


class FormatReportTest(unittest.TestCase):
    def row(self, name, condition, linked=True, trusted=True):
        return ReportRow(name=name, condition=condition, conversation=CONV, linked=linked, version="2.1.291",
                         restarts_24h=0, dir=f"/w/{name}", trusted=trusted)

    def test_all_clear_subject(self):
        subject, html = format_report([self.row("a", "OK"), self.row("b", "OK")])
        self.assertEqual(subject, "claude-rc: 2/2 OK")
        self.assertIn("11111111", html)

    def test_subject_counts_what_needs_you(self):
        subject, _ = format_report([self.row("a", "OK"), self.row("b", "WRONG_CONVERSATION"), self.row("c", "GAVE_UP"), self.row("d", "UNLINKED", linked=False)])
        self.assertEqual(subject, "claude-rc: 1/4 OK, 2 need you")

    def test_untrusted_folders_are_flagged_in_the_report(self):
        _, html = format_report([self.row("admin", "OK", trusted=False)])
        self.assertIn("not trusted", html)

    def test_names_are_escaped(self):
        _, html = format_report([self.row("<b>x</b>", "OK")])
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", html)
        self.assertNotIn("<b>x</b>", html)


if __name__ == "__main__":
    unittest.main()
