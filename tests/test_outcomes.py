import unittest

from experiment.assignment import arm_for
from experiment.cohorts import Cohort, Enrollment
from experiment.events import Conversion, EventLedger
from experiment.outcomes import (
    CONVERTED,
    IMMATURE,
    MISSING,
    NOT_CONVERTED,
    PENDING,
    classify,
)
from experiment.plan import AnalysisPlan

EXPERIMENT = "exp"
ARMS = ("control", "new")
WINDOW = 100


def arm_of(subject):
    return arm_for(subject, EXPERIMENT, ARMS)


def subjects_for(arm, count):
    found = []
    index = 0
    while len(found) < count:
        candidate = f"subject-{index}"
        if arm_of(candidate) == arm:
            found.append(candidate)
        index += 1
    return found


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.ledger = EventLedger()

    def run_classify(self, members, as_of, grace=0):
        return classify(tuple(members), self.ledger, as_of, WINDOW, grace, arm_lookup=arm_of)

    def test_immature_member_is_excluded_from_denominator(self):
        members = [Enrollment("a", 0, "s")]
        population = self.run_classify(members, as_of=50)
        self.assertEqual(population.outcomes[0].status, IMMATURE)
        self.assertFalse(population.outcomes[0].in_denominator)

    def test_matured_without_event_is_not_converted(self):
        members = [Enrollment("a", 0, "s")]
        population = self.run_classify(members, as_of=100)
        self.assertEqual(population.outcomes[0].status, NOT_CONVERTED)

    def test_conversion_inside_window_counts(self):
        self.ledger.record(Conversion("e1", "a", 99, received_at=99))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=100)
        self.assertEqual(population.outcomes[0].status, CONVERTED)
        self.assertEqual(population.outcomes[0].converted_at, 99)

    def test_conversion_at_exact_deadline_counts(self):
        self.ledger.record(Conversion("e1", "a", 100, received_at=100))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, CONVERTED)

    def test_conversion_after_window_never_counts(self):
        self.ledger.record(Conversion("e1", "a", 101, received_at=101))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=500)
        self.assertEqual(population.outcomes[0].status, NOT_CONVERTED)
        self.assertEqual(population.impact.out_of_window, 1)

    def test_conversion_before_join_does_not_count(self):
        self.ledger.record(Conversion("e1", "a", -5, received_at=-5))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, NOT_CONVERTED)
        self.assertEqual(population.impact.before_join, 1)

    def test_late_received_record_is_invisible_to_earlier_analysis(self):
        # 事件发生在窗口内，但分析时刻之后才收到。
        self.ledger.record(Conversion("e1", "a", 50, received_at=300))
        early = self.run_classify([Enrollment("a", 0, "s")], as_of=100)
        later = self.run_classify([Enrollment("a", 0, "s")], as_of=300)
        self.assertEqual(early.outcomes[0].status, NOT_CONVERTED)
        self.assertEqual(later.outcomes[0].status, CONVERTED)

    def test_duplicate_records_counted_once_per_person(self):
        self.ledger.record(Conversion("e1", "a", 10, received_at=10))
        self.ledger.record(Conversion("e2", "a", 20, received_at=20))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, CONVERTED)
        self.assertEqual(population.outcomes[0].window_records, 2)
        self.assertEqual(population.impact.duplicate, 1)

    def test_retracted_only_record_is_missing_not_unconverted(self):
        self.ledger.record(Conversion("e1", "a", 10, retracted=True, received_at=10))
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, MISSING)
        self.assertEqual(population.outcomes[0].missing_reason, "retracted_record")
        self.assertEqual(population.impact.retracted, 1)
        self.assertEqual(population.impact.retracted_within_window, 1)

    def test_retraction_never_marks_other_members_missing(self):
        self.ledger.record(Conversion("e1", "a", 10, retracted=True, received_at=10))
        members = [Enrollment("a", 0, "s"), Enrollment("b", 0, "s")]
        population = self.run_classify(members, as_of=200)
        statuses = {outcome.subject_id: outcome.status for outcome in population.outcomes}
        self.assertEqual(statuses["a"], MISSING)
        self.assertEqual(statuses["b"], NOT_CONVERTED)

    def test_active_gap_marks_missing(self):
        self.ledger.mark_gap("a", "tracking outage", at=10)
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, MISSING)
        self.assertEqual(population.outcomes[0].missing_reason, "data_gap")

    def test_resolved_gap_allows_observation(self):
        self.ledger.mark_gap("a", "tracking outage", at=10)
        self.ledger.resolve_gap("a", at=150)
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, NOT_CONVERTED)

    def test_gap_resolves_to_missing_if_it_was_active_at_deadline(self):
        self.ledger.mark_gap("a", "tracking outage", at=10)
        self.ledger.resolve_gap("a", at=150)
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=140)
        self.assertEqual(population.outcomes[0].status, MISSING)

    def test_reporting_grace_is_pending_then_unconverted(self):
        members = [Enrollment("a", 0, "s")]
        in_grace = classify(tuple(members), self.ledger, 120, WINDOW, grace=30, arm_lookup=arm_of)
        after_grace = classify(tuple(members), self.ledger, 130, WINDOW, grace=30, arm_lookup=arm_of)
        self.assertEqual(in_grace.outcomes[0].status, PENDING)
        self.assertEqual(in_grace.outcomes[0].missing_reason, "reporting_grace")
        self.assertEqual(after_grace.outcomes[0].status, NOT_CONVERTED)
        # 待定不是记录缺失，不进入缺失率的分母。
        self.assertEqual(in_grace.impact.retracted, 0)

    def test_conversion_beats_missing_gap(self):
        self.ledger.record(Conversion("e1", "a", 10, received_at=10))
        self.ledger.mark_gap("a", "outage", at=5)
        population = self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(population.outcomes[0].status, CONVERTED)

    def test_event_outside_population_is_reported(self):
        self.ledger.record(Conversion("e1", "not-enrolled", 10, received_at=10))
        self.run_classify([Enrollment("a", 0, "s")], as_of=200)
        self.assertEqual(self.run_classify([Enrollment("a", 0, "s")], as_of=200).impact.outside_population, 1)


class LedgerTests(unittest.TestCase):
    def test_revisions_must_arrive_in_order(self):
        ledger = EventLedger()
        ledger.record(Conversion("e1", "a", 10, received_at=20))
        with self.assertRaises(ValueError):
            ledger.record(Conversion("e1", "a", 10, retracted=True, received_at=15))

    def test_correction_is_visible_only_after_receipt(self):
        ledger = EventLedger()
        ledger.record(Conversion("e1", "a", 10, received_at=20))
        ledger.record(Conversion("e1", "a", 10, retracted=True, received_at=40))
        before = ledger.events_as_of(30)
        after = ledger.events_as_of(50)
        self.assertFalse(before[0].retracted)
        self.assertTrue(after[0].retracted)
        self.assertEqual(ledger.revision_counts_as_of(50)["e1"], 2)

    def test_event_cannot_move_to_another_subject(self):
        ledger = EventLedger()
        ledger.record(Conversion("e1", "a", 10, received_at=10))
        with self.assertRaises(ValueError):
            ledger.record(Conversion("e1", "b", 10, received_at=11))

    def test_gap_cannot_be_double_opened(self):
        ledger = EventLedger()
        ledger.mark_gap("a", "outage", at=1)
        with self.assertRaises(ValueError):
            ledger.mark_gap("a", "outage again", at=2)

    def test_gap_without_open_cannot_be_resolved(self):
        ledger = EventLedger()
        with self.assertRaises(ValueError):
            ledger.resolve_gap("a", at=2)


class BatchTests(unittest.TestCase):
    def test_batch_is_immutable_and_reproducible(self):
        cohort = Cohort(EXPERIMENT, ARMS)
        cohort.enroll(Enrollment("a", 0, "s"))
        cohort.enroll(Enrollment("b", 0, "s"))
        first = cohort.register_batch("b1", 10, ["a", "b"])
        with self.assertRaises(ValueError):
            cohort.register_batch("b1", 20, ["a"])
        self.assertEqual(first.digest, cohort.batch("b1").digest)
        self.assertEqual([member.subject_id for member in first.members], ["a", "b"])

    def test_batch_digest_changes_with_members(self):
        cohort = Cohort(EXPERIMENT, ARMS)
        cohort.enroll(Enrollment("a", 0, "s"))
        cohort.enroll(Enrollment("b", 0, "s"))
        self.assertNotEqual(
            cohort.register_batch("b1", 10, ["a"]).digest,
            cohort.register_batch("b2", 10, ["a", "b"]).digest,
        )

    def test_batch_rejects_unknown_member(self):
        cohort = Cohort(EXPERIMENT, ARMS)
        with self.assertRaises(ValueError):
            cohort.register_batch("b1", 10, ["missing"])

    def test_conflicting_enrollment_is_rejected(self):
        cohort = Cohort(EXPERIMENT, ARMS)
        cohort.enroll(Enrollment("a", 0, "s"))
        with self.assertRaises(ValueError):
            cohort.enroll(Enrollment("a", 5, "s"))


class PlanTests(unittest.TestCase):
    def test_plan_requires_valid_arms_and_window(self):
        base = dict(experiment_id=EXPERIMENT, arms=ARMS, control_arm="control", treatment_arm="new")
        with self.assertRaises(ValueError):
            AnalysisPlan(conversion_window=0, **base)
        with self.assertRaises(ValueError):
            AnalysisPlan(conversion_window=10, **{**base, "treatment_arm": "control"})
        with self.assertRaises(ValueError):
            AnalysisPlan(conversion_window=10, planned_looks=0, **base)

    def test_plan_digest_is_stable(self):
        plan = AnalysisPlan(EXPERIMENT, ARMS, "control", "new", 100)
        self.assertEqual(plan.digest, AnalysisPlan(EXPERIMENT, ARMS, "control", "new", 100).digest)


if __name__ == "__main__":
    unittest.main()
