"""实验分析服务：把入组登记、台账、计划和查看登记串成可复盘的流程。

典型用法：

    service = ExperimentService(cohort, ledger, plan)
    first = service.view(as_of=..., batch_id=..., viewed_at=..., sequence=1)
    ...
    service.stop(...)  # 可选，登记停测
    later = service.view(...)   # 补数后的新分析，旧分析保持不变
    review = service.compare(first, later)

服务只追加记录：已登记的分析、批次和查看不会因为后续数据到达而改变。
"""

from __future__ import annotations

from dataclasses import dataclass

from .cohorts import Cohort, Enrollment
from .monitoring import LookRecord, MonitoringRegistry, StopDecision
from .plan import AnalysisPlan
from .reconcile import Reconciliation, reconcile
from .report import DECISION_INSUFFICIENT, AnalysisReport, analyze


@dataclass(frozen=True)
class LoggedView:
    report: AnalysisReport
    look: LookRecord


class ExperimentService:
    def __init__(self, cohort: Cohort, ledger, plan: AnalysisPlan) -> None:
        if plan.experiment_id != cohort.experiment_id:
            raise ValueError("分析计划与入组登记的 experiment_id 不一致")
        self.cohort = cohort
        self.ledger = ledger
        self.plan = plan
        self.monitoring = MonitoringRegistry()
        self._views: dict[str, LoggedView] = {}

    # ------------------------------------------------------------- 入组
    def enroll(self, subject_id: str, joined_at: int, segment: str) -> str:
        return self.cohort.enroll(Enrollment(subject_id, joined_at, segment))

    def register_batch(self, batch_id: str, received_at: int, subjects: list[str]):
        return self.cohort.register_batch(batch_id, received_at, subjects)

    # ------------------------------------------------------------- 查看
    def view(
        self,
        *,
        as_of: int,
        batch_id: str,
        viewed_at: int | None = None,
        planned: bool = True,
        decision_note: str | None = None,
    ) -> LoggedView:
        """执行并登记一次查看。新数据必须显式给出新的 as_of。"""
        if self.monitoring.stop_decision is not None:
            raise ValueError("实验已登记停测；只能做标注为探索性的事后复核，不能新增正式查看")
        sequence = len(self.monitoring.looks) + 1
        if sequence > self.plan.planned_looks:
            raise ValueError(
                f"分析计划只登记了 {self.plan.planned_looks} 次查看；超出计划会增加误报风险，"
                "如需继续查看请先更新计划的查看次数并重新登记"
            )
        prior = [logged.report.context.information_fraction for logged in self._views.values()]
        report = analyze(
            self.cohort,
            self.ledger,
            self.plan,
            as_of=as_of,
            batch_id=batch_id,
            viewed_at=viewed_at,
            sequence=sequence,
            prior_information_fractions=prior,
        )
        look = self.monitoring.register_look(
            experiment_id=self.cohort.experiment_id,
            viewed_at=report.viewed_at,
            as_of=as_of,
            batch_id=batch_id,
            evidence_digest=report.evidence_digest,
            analysis_digest=report.report_digest,
            planned=planned,
            decision=report.decision if decision_note is None else f"{report.decision}: {decision_note}",
        )
        logged = LoggedView(report=report, look=look)
        self._views[look.look_id] = logged
        return logged

    def exploratory_review(
        self,
        *,
        as_of: int,
        batch_id: str,
        note: str,
        viewed_at: int | None = None,
    ) -> dict:
        """停测后的事后复核：只给描述，不消耗 α，也不进入正式查看登记。

        它使用同样的证据冻结与成熟度判定，但明确标记为探索性、不能用于停测
        或对外结论。新数据仍只能通过新的 ``as_of`` 进入。
        """
        if not note:
            raise ValueError("探索性复核必须说明目的")
        report = analyze(
            self.cohort,
            self.ledger,
            self.plan,
            as_of=as_of,
            batch_id=batch_id,
            viewed_at=viewed_at,
            sequence=max(1, len(self.monitoring.looks)),
            prior_information_fractions=[
                logged.report.context.information_fraction for logged in self._views.values()
            ][: max(0, len(self.monitoring.looks) - 1)],
        )
        payload = report.as_dict()
        # 事后复核不能携带任何“可宣称”的信号：去掉界值判断，标注为描述。
        payload["comparison"].update(
            {
                "crosses_boundary": None,
                "supports_decision": False,
                "status": "exploratory_only",
            }
        )
        for segment in payload["segments"].values():
            segment.update({"supports_decision": False, "status": "exploratory_only"})
        payload["look"].update({"boundary_z": None, "nominal_alpha": None, "alpha_spent": None})
        payload["exploratory"] = {
            "is_formal_look": False,
            "consumes_alpha": False,
            "supports_decision": False,
            "note": note,
        }
        payload["decision"] = "exploratory_only"
        payload["decision_basis"] = f"事后复核，仅供描述，不能作为正式结论：{note}"
        return payload

    def stop(self, *, at: int, reason: str, look_id: str, by: str) -> StopDecision:
        return self.monitoring.register_stop(stopped_at=at, reason=reason, look_id=look_id, by=by)

    def compare(self, earlier: LoggedView | AnalysisReport, later: LoggedView | AnalysisReport) -> Reconciliation:
        return reconcile(_report_of(earlier), _report_of(later))

    # ------------------------------------------------------------- 审计
    def history(self) -> dict:
        return {
            "experiment": self.cohort.experiment_id,
            "plan": self.plan.as_dict(),
            "plan_digest": self.plan.digest,
            "batches": [batch.as_dict() for batch in self.cohort.batches.values()],
            "views": [view.look.as_dict() for view in self._views.values()],
            "monitoring": self.monitoring.as_dict(),
        }

    def view_record(self, look_id: str) -> LoggedView:
        try:
            return self._views[look_id]
        except KeyError as error:
            raise KeyError(f"未知的查看 {look_id!r}") from error


def _report_of(item: LoggedView | AnalysisReport) -> AnalysisReport:
    return item.report if isinstance(item, LoggedView) else item


def needs_more_evidence(report: AnalysisReport) -> bool:
    return report.decision == DECISION_INSUFFICIENT
