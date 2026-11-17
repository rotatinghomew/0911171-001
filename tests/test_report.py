import unittest

from experiment.assignment import arm_for
from experiment.cohorts import Cohort, Enrollment
from experiment.events import Conversion, EventLedger
from experiment.plan import AnalysisPlan
from experiment.report import DECISION_INSUFFICIENT, DECISION_MATURE_DIFFERENCE, analyze

EXPERIMENT = "review"
ARMS = ("control", "new")
WINDOW = 100


def members_for(arm, count):
    found = []
    index = 0
    while len(found) < count:
        candidate = f"u{index:04d}"
        if arm_for(candidate, EXPERIMENT, ARMS) == arm:
            found.append(candidate)
        index += 1
    return found


def build(cohort, ledger, control_converted, treatment_converted, control_total, treatment_total):
    control = members_for("control", control_total)
    treatment = members_for("new", treatment_total)
    for subject_id in control:
        cohort.enroll(Enrollment(subject_id, 0, "all"))
    for subject_id in treatment:
        cohort.enroll(Enrollment(subject_id, 0, "all"))
    for index, subject_id in enumerate(control[:control_converted]):
        ledger.record(Conversion(f"c{index}", subject_id, 10, received_at=10))
    for index, subject_id in enumerate(treatment[:treatment_converted]):
        ledger.record(Conversion(f"t{index}", subject_id, 10, received_at=10))
    return control + treatment


def make_plan(**overrides):
    settings = dict(
        experiment_id=EXPERIMENT,
        arms=ARMS,
        control_arm="control",
        treatment_arm="new",
        conversion_window=WINDOW,
        planned_looks=1,
        alpha=0.05,
        min_matured_per_arm=5,
        max_missing_share=0.2,
    )
    settings.update(overrides)
    return AnalysisPlan(**settings)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.ledger = EventLedger()
        self.cohort = Cohort(EXPERIMENT, ARMS)

    def test_rates_use_only_matured_members(self):
        subjects = build(self.cohort, self.ledger, control_converted=10, treatment_converted=15,
                         control_total=30, treatment_total=30)
        batch = self.cohort.register_batch("b1", 1, subjects)
        plan = make_plan()
        report = analyze(self.cohort, self.ledger, plan, as_of=100, batch_id=batch.batch_id)
        self.assertEqual(report.counts["control"]["matured"], 30)
        self.assertAlmostEqual(report.counts["control"]["rate"], 10 / 30)
        self.assertAlmostEqual(report.counts["new"]["rate"], 15 / 30)
        self.assertAlmostEqual(report.comparison["risk_difference"], 5 / 30)

    def test_immature_members_stay_out_of_denominator(self):
        late = members_for("new", 5)
        for subject_id in late:
            self.cohort.enroll(Enrollment(subject_id, 80, "all"))
        early = members_for("control", 10)
        for subject_id in early:
            self.cohort.enroll(Enrollment(subject_id, 0, "all"))
        subjects = list(dict.fromkeys(late + early))
        batch = self.cohort.register_batch("b1", 1, subjects)
        report = analyze(self.cohort, self.ledger, make_plan(), as_of=100, batch_id=batch.batch_id)
        self.assertEqual(report.counts["new"]["immature"], 5)
        self.assertEqual(report.counts["new"]["matured"], 0)
        self.assertIsNone(report.counts["new"]["rate"])

    def test_evidence_digest_is_stable_and_reproducible(self):
        subjects = build(self.cohort, self.ledger, 5, 6, 20, 20)
        batch = self.cohort.register_batch("b1", 1, subjects)
        plan = make_plan()
        first = analyze(self.cohort, self.ledger, plan, as_of=100, batch_id=batch.batch_id)
        second = analyze(self.cohort, self.ledger, plan, as_of=100, batch_id=batch.batch_id)
        self.assertEqual(first.evidence_digest, second.evidence_digest)
        self.assertEqual(first.report_digest, second.report_digest)
        self.assertTrue(first.verify_evidence())

    def test_backfill_does_not_change_old_evidence(self):
        subjects = build(self.cohort, self.ledger, 5, 6, 20, 20)
        batch = self.cohort.register_batch("b1", 1, subjects)
        plan = make_plan()
        before = analyze(self.cohort, self.ledger, plan, as_of=100, batch_id=batch.batch_id)
        self.ledger.record(Conversion("late", subjects[0], 50, received_at=500))
        self.assertTrue(before.verify_evidence())
        after = analyze(self.cohort, self.ledger, plan, as_of=600, batch_id=batch.batch_id)
        self.assertNotEqual(before.evidence_digest, after.evidence_digest)
        self.assertTrue(before.verify_evidence())
        self.assertTrue(after.verify_evidence())

    def test_insufficient_evidence_when_too_few_matured(self):
        subjects = build(self.cohort, self.ledger, 1, 1, 3, 3)
        batch = self.cohort.register_batch("b1", 1, subjects)
        report = analyze(self.cohort, self.ledger, make_plan(min_matured_per_arm=5), as_of=100, batch_id=batch.batch_id)
        self.assertEqual(report.decision, DECISION_INSUFFICIENT)

    def test_insufficient_when_missing_share_exceeds_limit(self):
        subjects = build(self.cohort, self.ledger, 10, 10, 20, 20)
        # 选尚未转化的实验成员登记缺口，才能保证他们被判为缺失。
        treatment = self.cohort.members
        never_converted = [s for s in treatment if s.startswith("u") and self.cohort.arm_of(s) == "new"]
        for subject_id in never_converted[10:20]:
            self.ledger.mark_gap(subject_id, "outage", at=1)
        batch = self.cohort.register_batch("b1", 1, subjects)
        report = analyze(self.cohort, self.ledger, make_plan(), as_of=100, batch_id=batch.batch_id)
        self.assertEqual(report.counts["new"]["missing"], 10)
        self.assertEqual(report.decision, DECISION_INSUFFICIENT)

    def test_batch_created_after_analysis_time_is_rejected(self):
        subjects = build(self.cohort, self.ledger, 5, 6, 20, 20)
        batch = self.cohort.register_batch("b1", 50, subjects)
        with self.assertRaises(ValueError):
            analyze(self.cohort, self.ledger, make_plan(), as_of=10, batch_id=batch.batch_id)

    def test_unplanned_look_sequence_is_rejected(self):
        subjects = build(self.cohort, self.ledger, 5, 6, 20, 20)
        batch = self.cohort.register_batch("b1", 1, subjects)
        with self.assertRaises(ValueError):
            analyze(self.cohort, self.ledger, make_plan(planned_looks=1), as_of=100, batch_id=batch.batch_id, sequence=2)

    def test_segment_requires_plan_to_support_decision(self):
        control = members_for("control", 30)
        treatment = members_for("new", 30)
        for index, subject_id in enumerate(control):
            self.cohort.enroll(Enrollment(subject_id, 0, "returning" if index < 15 else "newcomer"))
        for index, subject_id in enumerate(treatment):
            self.cohort.enroll(Enrollment(subject_id, 0, "returning" if index < 15 else "newcomer"))
        subjects = control + treatment
        for index, subject_id in enumerate(control[:20]):
            self.ledger.record(Conversion(f"c{index}", subject_id, 10, received_at=10))
        for index, subject_id in enumerate(treatment[:10]):
            self.ledger.record(Conversion(f"t{index}", subject_id, 10, received_at=10))
        batch = self.cohort.register_batch("b1", 1, subjects)

        unplanned = analyze(self.cohort, self.ledger, make_plan(), as_of=100, batch_id=batch.batch_id)
        self.assertAlmostEqual(unplanned.comparison["risk_difference"], 10 / 30 - 20 / 30)
        self.assertEqual(set(unplanned.segments), {"newcomer", "returning"})
        for segment, comparison in unplanned.segments.items():
            self.assertFalse(comparison["prespecified"])
            self.assertFalse(comparison["supports_decision"])
            self.assertEqual(comparison["role"], "exploratory")
            self.assertGreater(comparison["nominal_level"], unplanned.comparison["nominal_level"])

        # 预先指定分群后，只有该分群进入主比较，且只有它支持决策。
        planned = analyze(
            self.cohort, self.ledger, make_plan(primary_segment="newcomer"), as_of=100, batch_id=batch.batch_id
        )
        newcomer_control = [subject for subject in control if self.cohort.members[subject].segment == "newcomer"]
        newcomer_treatment = [subject for subject in treatment if self.cohort.members[subject].segment == "newcomer"]
        self.assertEqual(planned.analyzed_members, len(newcomer_control) + len(newcomer_treatment))
        self.assertTrue(planned.segments["newcomer"]["prespecified"])
        self.assertTrue(planned.segments["newcomer"]["supports_decision"])
        self.assertFalse(planned.segments["returning"]["supports_decision"])
        self.assertEqual(planned.comparison["population"], "newcomer")

    def test_planned_segment_limits_population(self):
        control = members_for("control", 10)
        treatment = members_for("new", 10)
        for subject_id in control:
            self.cohort.enroll(Enrollment(subject_id, 0, "returning"))
        for subject_id in treatment:
            self.cohort.enroll(Enrollment(subject_id, 0, "newcomer"))
        batch = self.cohort.register_batch("b1", 1, control + treatment)
        report = analyze(
            self.cohort, self.ledger, make_plan(primary_segment="newcomer"), as_of=100, batch_id=batch.batch_id
        )
        self.assertEqual(report.analyzed_members, 10)
        self.assertEqual(report.comparison["population"], "newcomer")
        # 主比较只用计划分群；其他分群仍作描述性展示，但不支持决策。
        self.assertEqual(set(report.segments), {"newcomer", "returning"})
        self.assertTrue(report.segments["newcomer"]["supports_decision"])
        self.assertFalse(report.segments["returning"]["supports_decision"])

    def test_multiple_looks_use_stricter_than_single_look_boundary(self):
        subjects = build(self.cohort, self.ledger, 20, 21, 40, 40)
        batch = self.cohort.register_batch("b1", 1, subjects)
        single = analyze(self.cohort, self.ledger, make_plan(), as_of=100, batch_id=batch.batch_id)
        multi = analyze(
            self.cohort, self.ledger, make_plan(planned_looks=3), as_of=100, batch_id=batch.batch_id,
            sequence=1, matured_cap=160,
        )
        self.assertGreater(multi.context.boundary_z, single.context.boundary_z)
        self.assertLess(multi.context.nominal_alpha, single.context.nominal_alpha)


if __name__ == "__main__":
    unittest.main()


class SignalTests(unittest.TestCase):
    def _build(self, control_converted, treatment_converted, total=100):
        ledger = EventLedger()
        cohort = Cohort(EXPERIMENT, ARMS)
        control = members_for("control", total)
        treatment = members_for("new", total)
        for subject_id in control + treatment:
            cohort.enroll(Enrollment(subject_id, 0, "all"))
        for index, subject_id in enumerate(control[:control_converted]):
            ledger.record(Conversion(f"c{index}", subject_id, 5, received_at=5))
        for index, subject_id in enumerate(treatment[:treatment_converted]):
            ledger.record(Conversion(f"t{index}", subject_id, 5, received_at=5))
        return cohort, ledger, cohort.register_batch("b1", 0, control + treatment)

    def test_clear_difference_crosses_single_look_boundary(self):
        cohort, ledger, batch = self._build(10, 30)
        report = analyze(
            cohort, ledger,
            make_plan(planned_looks=1, planned_max_matured=200), as_of=WINDOW, batch_id=batch.batch_id,
        )
        self.assertTrue(report.comparison["crosses_boundary"])
        self.assertEqual(report.decision, DECISION_MATURE_DIFFERENCE)
        self.assertGreater(report.comparison["risk_difference_interval"][0], 0)

    def test_early_look_needs_larger_z_than_final_look(self):
        # 一半人一开始就入组，另一半要等 100 秒：第一次查看只有一半信息。
        ledger = EventLedger()
        cohort = Cohort(EXPERIMENT, ARMS)
        control = members_for("control", 100)
        treatment = members_for("new", 100)
        for index, subject_id in enumerate(control + treatment):
            cohort.enroll(Enrollment(subject_id, 0 if index % 2 == 0 else 100, "all"))
        for index, subject_id in enumerate(control[:5]):
            ledger.record(Conversion(f"c{index}", subject_id, 5, received_at=5))
        for index, subject_id in enumerate(treatment[:15]):
            ledger.record(Conversion(f"t{index}", subject_id, 5, received_at=5))
        batch = cohort.register_batch("b1", 0, control + treatment)
        plan = make_plan(planned_looks=3, planned_max_matured=200)

        early = analyze(cohort, ledger, plan, as_of=WINDOW, batch_id=batch.batch_id, sequence=1)
        self.assertLess(early.context.information_fraction, 1.0)
        final = analyze(
            cohort, ledger, plan, as_of=400, batch_id=batch.batch_id, sequence=3,
            prior_information_fractions=[
                early.context.information_fraction, early.context.information_fraction
            ],
        )
        self.assertEqual(final.context.information_fraction, 1.0)
        self.assertGreater(early.context.boundary_z, final.context.boundary_z)
