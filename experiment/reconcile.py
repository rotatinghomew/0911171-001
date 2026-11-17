"""两次查看之间的对账。

补数复盘的重点不是“两个转化率”，而是变化来源。本模块把旧分析与新分析
逐人、逐记录比对，给出确定性分解：

1. 入组批次是否相同、是否有新入组者或分群变化；
2. 每臂的分母变化：新成熟、仍不成熟、记录缺失解除或新增；
3. 转化状态迁移：新记录到达、补数到达、撤回或更正；
4. 期限外、重复、撤回等记录层面的增减。

每一项都只描述“当时看到什么、现在看到什么”，不改写旧分析。
"""

from __future__ import annotations

from dataclasses import dataclass

from .outcomes import CONVERTED, IMMATURE, MISSING, NOT_CONVERTED, PENDING, Outcome
from .report import AnalysisReport

STATUSES = (CONVERTED, NOT_CONVERTED, IMMATURE, PENDING, MISSING)


def _status_counts(outcomes: list[Outcome]) -> dict[str, int]:
    return {status: sum(1 for outcome in outcomes if outcome.status == status) for status in STATUSES}


@dataclass
class Reconciliation:
    earlier: AnalysisReport
    later: AnalysisReport

    def as_dict(self) -> dict:
        earlier, later = self.earlier, self.later
        old = {outcome.subject_id: outcome for outcome in earlier.population.outcomes}
        new = {outcome.subject_id: outcome for outcome in later.population.outcomes}

        added = sorted(set(new) - set(old))
        removed = sorted(set(old) - set(new))
        shared = sorted(set(old) & set(new))

        transitions: dict[str, int] = {}
        transition_detail: list[dict] = []
        conversion_sources = {
            "newly_matured": 0,
            "backfilled_record": 0,
            "corrected_record": 0,
            "recovered_missing": 0,
            "other": 0,
        }
        for subject_id in shared:
            before, after = old[subject_id], new[subject_id]
            key = f"{before.status}->{after.status}"
            transitions[key] = transitions.get(key, 0) + 1
            if before.status == after.status:
                continue
            source = _conversion_source(before, after, later)
            if after.status == CONVERTED:
                conversion_sources[source] += 1
            transition_detail.append(
                {
                    "subject_id": subject_id,
                    "arm": after.arm,
                    "segment": after.segment,
                    "from": before.status,
                    "to": after.status,
                    "from_missing_reason": before.missing_reason,
                    "to_missing_reason": after.missing_reason,
                    "from_converted_at": before.converted_at,
                    "to_converted_at": after.converted_at,
                    "to_evidence_event_id": after.evidence_event_id,
                    "change_source": source,
                }
            )

        per_arm = {}
        for arm in later.plan.comparison_arms:
            before_outcomes = [o for o in earlier.population.outcomes if o.arm == arm]
            after_outcomes = [o for o in later.population.outcomes if o.arm == arm]
            before_counts = _status_counts(before_outcomes)
            after_counts = _status_counts(after_outcomes)
            per_arm[arm] = {
                "earlier": self._arm_block(earlier, arm, before_outcomes, before_counts),
                "later": self._arm_block(later, arm, after_outcomes, after_counts),
                "delta": {
                    "eligible": len(after_outcomes) - len(before_outcomes),
                    "matured": sum(after_counts.values())
                    - after_counts[IMMATURE]
                    - after_counts[MISSING]
                    - (sum(before_counts.values()) - before_counts[IMMATURE] - before_counts[MISSING]),
                    "converted": after_counts[CONVERTED] - before_counts[CONVERTED],
                },
            }

        earlier_records = earlier.diagnostics["records"]
        later_records = later.diagnostics["records"]
        record_delta = {
            key: later_records.get(key, 0) - earlier_records.get(key, 0) for key in later_records
        }

        return {
            "experiment": later.experiment_id,
            "earlier": _headline(earlier),
            "later": _headline(later),
            "population_change": {
                "earlier_batch": earlier.batch_id,
                "later_batch": later.batch_id,
                "same_batch": earlier.batch_id == later.batch_id,
                "earlier_batch_digest": earlier.batch_digest,
                "later_batch_digest": later.batch_digest,
                "added_subjects": added,
                "removed_subjects": removed,
                "shared_subjects": len(shared),
            },
            "outcome_transitions": transitions,
            "conversion_sources": conversion_sources,
            "transition_detail": sorted(transition_detail, key=lambda row: row["subject_id"]),
            "per_arm": per_arm,
            "record_delta": record_delta,
            "comparison_earlier": earlier.comparison,
            "comparison_later": later.comparison,
            "explanation": _explain(
                earlier, later, transitions, added, removed, record_delta, conversion_sources
            ),
        }

    @staticmethod
    def _arm_block(report: AnalysisReport, arm: str, outcomes: list[Outcome], counts: dict[str, int]) -> dict:
        bucket = report.counts.get(arm, {})
        return {
            "eligible": len(outcomes),
            "matured": bucket.get("matured", 0),
            "converted": counts[CONVERTED],
            "rate": bucket.get("rate"),
            **counts,
        }


def _conversion_source(before: Outcome, after: Outcome, report: AnalysisReport) -> str:
    """说明一次“变为已转化”具体由什么驱动。"""
    if after.status != CONVERTED:
        return "other"
    if before.status == IMMATURE:
        return "newly_matured"
    if before.status == MISSING:
        return "recovered_missing"
    if before.status != NOT_CONVERTED or after.converted_at is None:
        return "other"
    evidence = next(
        (
            event
            for event in report.evidence["ledger"]["events"]
            if event["event_id"] == after.evidence_event_id
        ),
        None,
    )
    if evidence is None:
        return "other"
    if evidence.get("revisions", 1) > 1:
        # 旧分析时已知这条记录，但它当时被撤回或还未更正。
        return "corrected_record"
    if evidence["received_at"] - evidence["occurred_at"] > report.plan.conversion_window:
        # 首次到达就晚于窗口：补数。
        return "backfilled_record"
    return "other"


def _headline(report: AnalysisReport) -> dict:
    return {
        "look": report.context.as_dict(),
        "as_of": report.as_of,
        "viewed_at": report.viewed_at,
        "batch_id": report.batch_id,
        "batch_digest": report.batch_digest,
        "evidence_digest": report.evidence_digest,
        "decision": report.decision,
        "decision_basis": report.decision_basis,
        "counts": report.counts,
        "risk_difference": report.comparison["risk_difference"],
        "risk_difference_interval": report.comparison["risk_difference_interval"],
        "repeated_confidence_interval": report.comparison["repeated_confidence_interval"],
        "boundary_z": report.context.boundary_z,
    }


def _explain(
    earlier: AnalysisReport,
    later: AnalysisReport,
    transitions: dict[str, int],
    added: list[str],
    removed: list[str],
    record_delta: dict[str, int],
    conversion_sources: dict[str, int],
) -> list[str]:
    notes = []
    if earlier.batch_id != later.batch_id:
        notes.append(
            f"入组批次由 {earlier.batch_id} 改为 {later.batch_id}；两次分析使用不同的入组依据，"
            "旧分析的批次摘要保持不变。"
        )
    if added:
        notes.append(f"新增 {len(added)} 名入组者进入分析人群。")
    if removed:
        notes.append(f"新分析缺少旧分析中的 {len(removed)} 名入组者，需要核对批次选择或分群定义。")
    matured_added = sum(
        count
        for key, count in transitions.items()
        if key.split("->")[0] == IMMATURE and key.split("->")[1] != IMMATURE
    )
    if matured_added:
        notes.append(f"{matured_added} 名原先未成熟的入组者已走完固定转化期限，进入转化率分母。")
    newly_converted = transitions.get(f"{NOT_CONVERTED}->{CONVERTED}", 0)
    if newly_converted:
        notes.append(
            f"{newly_converted} 名原本已成熟且未转化者变为已转化"
            f"（其中 {conversion_sources['backfilled_record']} 名来自迟到到达的补数，"
            f"{conversion_sources['corrected_record']} 名来自记录更正）。"
        )
    recovered = (
        transitions.get(f"{MISSING}->{CONVERTED}", 0) + transitions.get(f"{MISSING}->{NOT_CONVERTED}", 0)
    )
    if recovered:
        notes.append(f"{recovered} 名记录缺失者在本期解除缺口，重新进入分母。")
    pending_matured = sum(
        count for key, count in transitions.items() if key.startswith(f"{PENDING}->")
    )
    if pending_matured:
        notes.append(f"{pending_matured} 名待定者走完记录宽限期，获得确定结果。")
    newly_missing = sum(count for key, count in transitions.items() if key.split("->")[1] == MISSING)
    if newly_missing:
        notes.append(f"{newly_missing} 名已成熟者变为记录缺失，不能当作未转化。")
    if record_delta.get("retracted_records", 0):
        notes.append(f"撤回记录较上次增加 {record_delta['retracted_records']} 条。")
    if record_delta.get("out_of_window_records", 0):
        notes.append(f"期限外记录较上次增加 {record_delta['out_of_window_records']} 条，仍不计为转化。")
    if record_delta.get("duplicate_records", 0):
        notes.append(f"窗口内重复记录较上次增加 {record_delta['duplicate_records']} 条，按人只计一次。")
    if (
        earlier.batch_id == later.batch_id
        and earlier.batch_digest == later.batch_digest
        and not added
        and not removed
        and not transitions
    ):
        notes.append("两次分析使用同一批次摘要且个体状态未变化；差异只可能来自查看界值或信息量。")
    pending_now = sum(
        1 for outcome in later.population.outcomes if outcome.status == PENDING
    )
    immature_now = sum(
        1 for outcome in later.population.outcomes if outcome.status == IMMATURE
    )
    if pending_now or immature_now:
        notes.append(
            f"截至新分析仍有 {immature_now} 名未成熟、{pending_now} 名待定；他们的结果尚未确定，"
            "既不能算作未转化，也不应进入分母。"
        )
    if later.context.information_fraction > earlier.context.information_fraction:
        notes.append(
            "本次查看的信息量占计划最终成熟人数的 "
            f"{later.context.information_fraction:.0%}（上次 {earlier.context.information_fraction:.0%}），"
            "序贯界值随之变化；旧分析的界值不会被追溯修改。"
        )
    return notes


def reconcile(earlier: AnalysisReport, later: AnalysisReport) -> Reconciliation:
    if earlier.experiment_id != later.experiment_id:
        raise ValueError("只能对同一实验的两次分析做对账")
    if later.as_of < earlier.as_of:
        raise ValueError("后一次分析的时刻不能早于前一次")
    return Reconciliation(earlier=earlier, later=later)
