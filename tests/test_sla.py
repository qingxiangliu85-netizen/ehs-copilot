"""P0B tests for the demo SLA / deadline rules."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from workflow import sla


NOW = datetime(2026, 9, 13, 10, 0, 0)


class PolicyTests(unittest.TestCase):
    def test_demo_policy_values(self) -> None:
        self.assertEqual(sla.approval_days("重大"), 1)
        self.assertEqual(sla.approval_days("高"), 2)
        self.assertEqual(sla.approval_days("中"), 3)
        self.assertEqual(sla.approval_days("低"), 5)
        self.assertEqual(sla.rectification_days("重大"), 1)
        self.assertEqual(sla.verification_days("高"), 2)

    def test_unknown_risk_uses_medium_fallback(self) -> None:
        self.assertEqual(sla.approval_days(""), sla.approval_days("中"))
        self.assertEqual(sla.rectification_days("未知"), sla.rectification_days("中"))

    def test_policy_is_labelled_as_demo(self) -> None:
        self.assertIn("Demo", sla.SLA_RULE_LABEL)

    def test_calculate_due_at_adds_natural_days(self) -> None:
        due = sla.calculate_due_at(NOW, 3)
        self.assertEqual(due, NOW + timedelta(days=3))


class DeadlineTests(unittest.TestCase):
    def test_classify_deadline_boundaries(self) -> None:
        cases = (
            (NOW - timedelta(days=1), sla.DEADLINE_OVERDUE),
            (NOW.replace(hour=23), sla.DEADLINE_DUE_TODAY),
            (NOW + timedelta(days=1), sla.DEADLINE_DUE_SOON),
            (NOW + timedelta(days=3), sla.DEADLINE_NORMAL),
            ("", sla.DEADLINE_NORMAL),
            ("not-a-date", sla.DEADLINE_NORMAL),
            (None, sla.DEADLINE_NORMAL),
        )
        for due, expected in cases:
            with self.subTest(due=due):
                self.assertEqual(sla.classify_deadline(due, NOW), expected)

    def test_classify_accepts_iso_strings_and_dates(self) -> None:
        self.assertEqual(
            sla.classify_deadline((NOW - timedelta(days=2)).isoformat(), NOW),
            sla.DEADLINE_OVERDUE,
        )
        self.assertEqual(
            sla.classify_deadline(NOW.date().isoformat(), NOW),
            sla.DEADLINE_DUE_TODAY,
        )

    def test_is_overdue(self) -> None:
        self.assertTrue(sla.is_overdue(NOW - timedelta(days=1), NOW))
        self.assertFalse(sla.is_overdue(NOW + timedelta(days=1), NOW))

    def test_risk_and_deadline_are_separate_dimensions(self) -> None:
        overdue_low = sla.priority_score("低", sla.DEADLINE_OVERDUE)
        high_normal = sla.priority_score("重大", sla.DEADLINE_NORMAL)
        self.assertGreater(overdue_low, high_normal)
        self.assertTrue(
            sla.is_overdue(NOW - timedelta(days=1), NOW)
        )
        self.assertGreater(sla.risk_weight("重大"), sla.risk_weight("低"))
        self.assertGreater(
            sla.deadline_weight(sla.DEADLINE_OVERDUE),
            sla.deadline_weight(sla.DEADLINE_NORMAL),
        )

    def test_priority_orders_within_the_same_window(self) -> None:
        self.assertGreater(
            sla.priority_score("高", sla.DEADLINE_DUE_TODAY),
            sla.priority_score("低", sla.DEADLINE_DUE_TODAY),
        )

    def test_due_soon_window_is_tomorrow(self) -> None:
        self.assertEqual(
            sla.deadline_weight(sla.DEADLINE_DUE_SOON),
            sla.DEADLINE_WEIGHTS[sla.DEADLINE_DUE_SOON],
        )


if __name__ == "__main__":
    unittest.main()
