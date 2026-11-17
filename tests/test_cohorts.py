import unittest

from experiment.cohorts import Cohort, Enrollment
from experiment.events import Conversion, EventLedger


class CohortTests(unittest.TestCase):
    def test_repeated_enrollment_does_not_reassign(self):
        cohort = Cohort("entry", ("control", "new"))
        member = Enrollment("a", 10, "reader")
        self.assertEqual(cohort.enroll(member), cohort.enroll(member))
        with self.assertRaises(ValueError):
            cohort.enroll(Enrollment("a", 11, "reader"))

    def test_segment_and_join_time_limit_observation_population(self):
        cohort = Cohort("entry", ("control",))
        for member in [Enrollment("a", 10, "reader"), Enrollment("b", 30, "reader"), Enrollment("c", 10, "visitor")]:
            cohort.enroll(member)
        ledger = EventLedger()
        ledger.record(Conversion("e1", "a", 15))
        result = cohort.observed_report(ledger, 20, "reader")
        self.assertEqual(result["groups"]["control"], {"eligible": 1, "converted": 1, "rate": 1.0})
        self.assertEqual([row["subject_id"] for row in result["members"]], ["a"])
