import unittest

from experiment.assignment import arm_for
from experiment.events import Conversion, EventLedger
from experiment.report import conversion_summary


class ExperimentTests(unittest.TestCase):
    def test_assignment_is_repeatable(self):
        self.assertEqual(arm_for("r1", "exp", ("a", "b")), arm_for("r1", "exp", ("a", "b")))

    def test_report_counts_distinct_subjects_through_cutoff(self):
        ledger = EventLedger()
        subject = next(f"r{i}" for i in range(100) if arm_for(f"r{i}", "exp", ("a", "b")) == "a")
        ledger.record(Conversion("e1", subject, 2))
        ledger.record(Conversion("e2", subject, 3))
        ledger.record(Conversion("e3", subject, 8))
        result = conversion_summary([subject, "other"], "exp", ("a", "b"), ledger, 4)
        self.assertEqual(result["a"]["converted"], 1)


if __name__ == "__main__":
    unittest.main()
