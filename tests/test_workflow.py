import unittest

from experiment.assignment import arm_for
from experiment.cohorts import Cohort, Enrollment
from experiment.events import Conversion, EventLedger
from experiment.monitoring import MonitoringRegistry
from experiment.plan import AnalysisPlan
from experiment.reconcile import reconcile
from experiment.service import ExperimentService

EXPERIMENT = "workflow"
ARMS = ("control", "new")
WINDOW = 100


def subjects_for(arm, count):
    found, index = [], 0
    while len(found) < count:
        candidate = f"s{index:04d}"
        if arm_for(candidate, EXPERIMENT, ARMS) == arm:
            found.append(candidate)
        index += 1
    return found


def make_scenario(control_total=40, treatment_total=40, control_converted=8, treatment_converted=14):
    ledger = EventLedger()
    cohort = Cohort(EXPERIMENT, ARMS)
    control = subjects_for("control", control_total)
    treatment = subjects_for("new", treatment_total)
    for subject_id in control + treatment:
        cohort.enroll(Enrollment(subject_id, 0, "all"))
    for index, subject_id in enumerate(control[:control_converted]):
        ledger.record(Conversion(f"c{index}", subject_id, 10, received_at=10))
    for index, subject_id in enumerate(treatment[:treatment_converted]):
        ledger.record(Conversion(f"t{index}", subject_id, 10, received_at=10))
    plan = AnalysisPlan(
        EXPERIMENT, ARMS, "control", "new", WINDOW,
        planned_looks=2, alpha=0.05, min_matured_per_arm=5, max_missing_share=0.2,
        planned_max_matured=80,
    )
    return cohort, ledger, plan, control + treatment


class MonitoringTests(unittest.TestCase):
    def test_chain_detects_tampering(self):
        registry = MonitoringRegistry()
        registry.register_look(EXPERIMENT, 1, 1, "b1", "e1", "a1", True, "continue")
        registry.register_look(EXPERIMENT, 2, 2, "b1", "e2", "a2", True, "continue")
        self.assertTrue(registry.verify_chain())
        registry.looks[0] = registry.looks[0].__class__(**{**registry.looks[0].__dict__, "as_of": 99})
        self.assertFalse(registry.verify_chain())

    def test_no_looks_after_stop(self):
        registry = MonitoringRegistry()
        look = registry.register_look(EXPERIMENT, 1, 1, "b1", "e1", "a1", True, "continue")
        registry.register_stop(2, "planned end", look.look_id, "ops")
        with self.assertRaises(ValueError):
            registry.register_look(EXPERIMENT, 3, 3, "b1", "e2", "a2", True, "continue")

    def test_stop_requires_existing_look_and_reason(self):
        registry = MonitoringRegistry()
        with self.assertRaises(ValueError):
            registry.register_stop(1, "done", "look-999", "ops")
        registry.register_look(EXPERIMENT, 1, 1, "b1", "e1", "a1", True, "continue")
        with self.assertRaises(ValueError):
            registry.register_stop(1, "", "look-001", "ops")

    def test_duplicate_stop_is_rejected(self):
        registry = MonitoringRegistry()
        registry.register_look(EXPERIMENT, 1, 1, "b1", "e1", "a1", True, "continue")
        registry.register_stop(1, "done", "look-001", "ops")
        with self.assertRaises(ValueError):
            registry.register_stop(2, "again", "look-001", "ops")


class ServiceTests(unittest.TestCase):
    def test_view_registers_look_and_is_reproducible(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=20, batch_id=batch.batch_id, viewed_at=20)
        self.assertEqual(first.look.sequence, 1)
        self.assertEqual(first.look.viewed_at, 20)
        self.assertTrue(service.monitoring.verify_chain())
        self.assertEqual(service.history()["views"][0]["evidence_digest"], first.report.evidence_digest)

    def test_second_view_reconciles_backfill(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=20, batch_id=batch.batch_id, viewed_at=20)
        # 补数：给未转化的对照成员补一条窗口内记录。
        late_subject = subjects_for("control", 40)[20]
        ledger.record(Conversion("backfill-1", late_subject, 30, received_at=200))
        second = service.view(as_of=200, batch_id=batch.batch_id, viewed_at=200)
        review = service.compare(first, second).as_dict()
        self.assertIn("immature->converted", review["outcome_transitions"])
        self.assertEqual(review["record_delta"]["revisions_received"], 1)
        self.assertTrue(first.report.verify_evidence())
        self.assertNotEqual(
            review["earlier"]["evidence_digest"], review["later"]["evidence_digest"]
        )

    def test_third_look_beyond_plan_is_rejected(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        service.view(as_of=20, batch_id=batch.batch_id)
        service.view(as_of=40, batch_id=batch.batch_id)
        with self.assertRaises(ValueError):
            service.view(as_of=60, batch_id=batch.batch_id)

    def test_stopped_service_refuses_further_views(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=20, batch_id=batch.batch_id)
        service.stop(at=21, reason="达到计划终期", look_id=first.look.look_id, by="ops")
        with self.assertRaises(ValueError):
            service.view(as_of=40, batch_id=batch.batch_id)

    def test_later_boundary_uses_realized_information(self):
        # 一半人先入组、一半人 100 秒后才入组：第一次查看只有一半人成熟。
        ledger = EventLedger()
        cohort = Cohort(EXPERIMENT, ARMS)
        control = subjects_for("control", 40)
        treatment = subjects_for("new", 40)
        for index, subject_id in enumerate(control + treatment):
            cohort.enroll(Enrollment(subject_id, 0 if index % 2 == 0 else 100, "all"))
        for index, subject_id in enumerate(control[:8]):
            ledger.record(Conversion(f"c{index}", subject_id, 10, received_at=10))
        for index, subject_id in enumerate(treatment[:14]):
            ledger.record(Conversion(f"t{index}", subject_id, 10, received_at=10))
        plan = AnalysisPlan(
            EXPERIMENT, ARMS, "control", "new", WINDOW,
            planned_looks=2, alpha=0.05, min_matured_per_arm=5, max_missing_share=0.2,
            planned_max_matured=80,
        )
        batch = cohort.register_batch("b1", 1, control + treatment)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=100, batch_id=batch.batch_id)
        second = service.view(as_of=400, batch_id=batch.batch_id)
        self.assertGreater(first.report.context.information_fraction, 0.0)
        self.assertLess(first.report.context.information_fraction, 1.0)
        self.assertEqual(second.report.context.information_fraction, 1.0)
        # 两次查看共享 α 预算：第一次的界值应比单次 1.96 更严格。
        self.assertGreater(first.report.context.boundary_z, 1.959963984540054)
        self.assertGreater(second.report.context.boundary_z, 1.959963984540054)

    def test_reconcile_rejects_time_travel(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=20, batch_id=batch.batch_id)
        second = service.view(as_of=200, batch_id=batch.batch_id)
        with self.assertRaises(ValueError):
            reconcile(second.report, first.report)

    def test_same_batch_reuses_digest_across_views(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=20, batch_id=batch.batch_id)
        second = service.view(as_of=200, batch_id=batch.batch_id)
        self.assertEqual(first.report.batch_digest, second.report.batch_digest)


if __name__ == "__main__":
    unittest.main()


class ReconciliationDetailTests(unittest.TestCase):
    def test_explains_where_change_came_from(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=120, batch_id=batch.batch_id)
        # 首次分析时已成熟但未转化的对照成员，补数后才变为转化。
        matured_not_converted = [
            o.subject_id for o in first.report.population.outcomes
            if o.status == "not_converted"
        ]
        backfill_subject = matured_not_converted[0]
        ledger.record(Conversion("backfill", backfill_subject, 30, received_at=400))
        second = service.view(as_of=400, batch_id=batch.batch_id)
        payload = service.compare(first, second).as_dict()
        self.assertGreaterEqual(payload["conversion_sources"]["backfilled_record"], 1)
        self.assertEqual(
            payload["outcome_transitions"].get("not_converted->converted"), 1
        )
        self.assertTrue(any("补数" in note for note in payload["explanation"]))
        detail = [row for row in payload["transition_detail"] if row["to"] == "converted"]
        self.assertTrue(all(row["change_source"] == "backfilled_record" for row in detail))

    def test_pending_members_are_not_counted_as_unconverted(self):
        cohort, ledger, plan, subjects = make_scenario()
        plan = AnalysisPlan(
            EXPERIMENT, ARMS, "control", "new", WINDOW,
            planned_looks=2, alpha=0.05, min_matured_per_arm=1, max_missing_share=1.0,
            recording_grace=50, planned_max_matured=80,
        )
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        # as_of=120：窗口已结束但仍在 50 秒宽限期内。
        first = service.view(as_of=120, batch_id=batch.batch_id)
        counts = first.report.counts
        for arm in ("control", "new"):
            self.assertGreater(counts[arm]["pending"], 0)
            self.assertEqual(
                counts[arm]["matured"] + counts[arm]["pending"] + counts[arm]["immature"],
                counts[arm]["eligible"],
            )


class DeterminismTests(unittest.TestCase):
    def test_demo_report_is_byte_stable(self):
        import io
        import contextlib
        from experiment.demo import main

        def run():
            buffer = io.StringIO()
            with contextlib.redirect_stdout(buffer):
                main()
            return buffer.getvalue()

        first, second = run(), run()
        self.assertEqual(first, second)
        self.assertIn("旧分析证据摘要未被后续记录改写：True", first)


class CorrectionAttributionTests(unittest.TestCase):
    def test_retraction_then_correction_is_attributed(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        # 先报告一个未转化者，然后登记并撤回该记录，再补正为有效转化。
        target = subjects_for("control", 40)[30]
        ledger.record(Conversion("fix", target, 30, retracted=True, received_at=31))
        plan = AnalysisPlan(
            EXPERIMENT, ARMS, "control", "new", WINDOW,
            planned_looks=2, alpha=0.05, min_matured_per_arm=1, max_missing_share=1.0,
            planned_max_matured=80,
        )
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=150, batch_id=batch.batch_id)
        self.assertEqual(
            {o.subject_id: o.status for o in first.report.population.outcomes}[target], "missing"
        )
        ledger.record(Conversion("fix", target, 30, retracted=False, received_at=400))
        second = service.view(as_of=450, batch_id=batch.batch_id)
        payload = service.compare(first, second).as_dict()
        self.assertEqual(payload["conversion_sources"]["recovered_missing"], 1)
        detail = [row for row in payload["transition_detail"] if row["subject_id"] == target]
        self.assertEqual(detail[0]["from"], "missing")
        self.assertEqual(detail[0]["to"], "converted")


class ExploratoryReviewTests(unittest.TestCase):
    def test_post_stop_review_is_explicitly_non_confirmatory(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        first = service.view(as_of=120, batch_id=batch.batch_id)
        service.stop(at=121, reason="达到计划终期", look_id=first.look.look_id, by="ops")
        with self.assertRaises(ValueError):
            service.view(as_of=300, batch_id=batch.batch_id)
        review = service.exploratory_review(as_of=300, batch_id=batch.batch_id, note="问询：补数后是否变化")
        self.assertFalse(review["exploratory"]["is_formal_look"])
        self.assertFalse(review["exploratory"]["supports_decision"])
        self.assertFalse(review["exploratory"]["consumes_alpha"])
        self.assertEqual(review["decision"], "exploratory_only")
        # 正式查看记录仍然只有一次。
        self.assertEqual(len(service.history()["views"]), 1)

    def test_exploratory_review_requires_note(self):
        cohort, ledger, plan, subjects = make_scenario()
        batch = cohort.register_batch("b1", 1, subjects)
        service = ExperimentService(cohort, ledger, plan)
        with self.assertRaises(ValueError):
            service.exploratory_review(as_of=120, batch_id=batch.batch_id, note="")
