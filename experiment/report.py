"""正式复盘报告。

一次分析绑定到三样不可变输入：
- as_of：当时已收到的台账版本；
- 入组批次：具体批次编号与内容摘要；
- 预先登记的分析计划：转化期限、主比较分群、计划查看次数。

所以几周后的补数只能形成新的分析，不能改写旧结论的依据。证据快照与报告
各自带 SHA-256 摘要，可独立核对字节级一致性。
"""

from __future__ import annotations

from dataclasses import dataclass

from scipy.stats import norm

from . import stats
from .cohorts import Cohort
from .outcomes import (
    CONVERTED,
    IMMATURE,
    MISSING,
    NOT_CONVERTED,
    PENDING,
    Outcome,
    Population,
    arm_counts,
    classify,
    select_population,
)
from .plan import AnalysisPlan
from .util import digest, json_safe

UNDETERMINED = "undetermined"

DECISION_MATURE_DIFFERENCE = "mature_difference"
DECISION_CONTINUE = "continue"
DECISION_INSUFFICIENT = "insufficient_evidence"


# ---------------------------------------------------------------------- 证据
def build_evidence(cohort: Cohort, ledger, plan: AnalysisPlan, as_of: int, batch_id: str) -> dict:
    """冻结一次分析所依据的全部输入。"""
    batch = cohort.batch(batch_id)
    if batch.received_at > as_of:
        raise ValueError("分析时刻早于入组批次接收时间")
    return freeze_evidence(
        {
            "experiment": cohort.experiment_id,
            "arms": list(cohort.arms),
            "as_of": as_of,
            "plan": plan.as_dict(),
            "plan_digest": plan.digest,
            "batch": batch.as_dict(),
            "ledger": ledger.snapshot(as_of),
        }
    )


def freeze_evidence(evidence: dict) -> tuple[dict, str]:
    frozen = json_safe(evidence)
    return frozen, digest(frozen)


@dataclass(frozen=True)
class LookContext:
    sequence: int
    information_fraction: float
    # 信息量为 0 时没有可比较数据，界值为 None（不能判断）。
    boundary_z: float | None
    nominal_alpha: float
    # 截至本次查看累计消耗的族系误报率（应 <= 计划 alpha）。
    alpha_spent: float | None = None

    def as_dict(self) -> dict:
        return {
            "sequence": self.sequence,
            "information_fraction": self.information_fraction,
            "boundary_z": self.boundary_z,
            "nominal_alpha": self.nominal_alpha,
            "alpha_spent": self.alpha_spent,
        }


# ---------------------------------------------------------------------- 报告
@dataclass
class AnalysisReport:
    experiment_id: str
    as_of: int
    viewed_at: int
    plan: AnalysisPlan
    batch_id: str
    batch_digest: str
    batch_received_at: int
    batch_size: int
    analyzed_members: int
    evidence: dict
    evidence_digest: str
    population: Population
    counts: dict[str, dict]
    comparison: dict
    segments: dict[str, dict]
    diagnostics: dict
    decision: str
    decision_basis: str
    context: LookContext

    def as_dict(self) -> dict:
        return json_safe(
            {
                "experiment": self.experiment_id,
                "as_of": self.as_of,
                "viewed_at": self.viewed_at,
                "plan": self.plan.as_dict(),
                "plan_digest": self.plan.digest,
                "batch": {
                    "batch_id": self.batch_id,
                    "digest": self.batch_digest,
                    "received_at": self.batch_received_at,
                    "size": self.batch_size,
                    "analyzed": self.analyzed_members,
                },
                "evidence_digest": self.evidence_digest,
                "look": {
                    **self.context.as_dict(),
                    "planned_looks": self.plan.planned_looks,
                    "alpha": self.plan.alpha,
                    "boundary_scale": "z",
                },
                "counts": self.counts,
                "comparison": self.comparison,
                "segments": self.segments,
                "diagnostics": self.diagnostics,
                "decision": self.decision,
                "decision_basis": self.decision_basis,
                "outcomes": [outcome.as_dict() for outcome in self.population.outcomes],
            }
        )

    @property
    def report_digest(self) -> str:
        return digest(self.as_dict())

    def verify_evidence(self) -> bool:
        return digest(self.evidence) == self.evidence_digest


# ---------------------------------------------------------------------- 汇总
def _group_summary(outcomes: list[Outcome], arm: str) -> dict:
    bucket = [outcome for outcome in outcomes if outcome.arm == arm]
    matured = [outcome for outcome in bucket if outcome.in_denominator]
    converted = [outcome for outcome in bucket if outcome.status == CONVERTED]
    return {
        "eligible": len(bucket),
        "immature": sum(1 for outcome in bucket if outcome.status == IMMATURE),
        "pending": sum(1 for outcome in bucket if outcome.status == PENDING),
        "matured": len(matured),
        "converted": len(converted),
        "not_converted": sum(1 for outcome in bucket if outcome.status == NOT_CONVERTED),
        "missing": sum(1 for outcome in bucket if outcome.status == MISSING),
        "rate": (len(converted) / len(matured)) if matured else None,
    }


def _compare(
    outcomes: list[Outcome],
    plan: AnalysisPlan,
    *,
    level: float,
    boundary_z: float | None,
    claim_level: float | None = None,
) -> dict:
    """实验臂减对照臂的风险差。

    ``level`` 是描述性区间（Newcombe 得分区间）的名义水平；
    ``claim_level`` 是“越过序贯界值的重复置信区间”的水平，由 O'Brien-Fleming
    界值换算而来。两者分开记录，避免把描述区间误读成可重复查看的结论。
    """
    treatment = _group_summary(outcomes, plan.treatment_arm)
    control = _group_summary(outcomes, plan.control_arm)
    difference = interval = z = p_value = None
    if treatment["rate"] is not None and control["rate"] is not None:
        difference = treatment["rate"] - control["rate"]
        interval = stats.newcombe_difference(
            treatment["converted"], treatment["matured"], control["converted"], control["matured"], level
        )
        z, p_value = stats.pooled_z_test(
            treatment["converted"], treatment["matured"], control["converted"], control["matured"]
        )
    standard_error = None
    if treatment["matured"] and control["matured"]:
        standard_error = stats.unpooled_standard_error(
            treatment["converted"], treatment["matured"], control["converted"], control["matured"]
        )
    repeated = stats.sequential_interval(difference, standard_error, boundary_z)
    crossed = None if z is None or boundary_z is None else abs(z) >= boundary_z
    return {
        "treatment_arm": treatment,
        "control_arm": control,
        "risk_difference": difference,
        "risk_difference_interval": interval,
        "repeated_confidence_interval": repeated,
        "z": z,
        "nominal_p_value": p_value,
        "crosses_boundary": crossed,
        "direction": (
            None
            if difference is None
            else "treatment_higher"
            if difference > 0
            else "treatment_lower"
            if difference < 0
            else "none"
        ),
        "status": (
            UNDETERMINED
            if difference is None or crossed is None
            else ("difference" if crossed else "no_mature_difference")
        ),
        "nominal_level": level,
        "claim_level": claim_level,
    }


# ---------------------------------------------------------------------- 分群
def segment_reports(
    outcomes: list[Outcome],
    plan: AnalysisPlan,
    *,
    level: float,
    boundary_z: float | None,
) -> dict[str, dict]:
    """按业务分群分别比较。

    只有计划预先指定的那个分群支持决策；其余分群按分群数做 Bonferroni
    调整并明确标注为事后描述，避免“挑一个有利分群”当结论。
    """
    segments = sorted({outcome.segment for outcome in outcomes})
    exploratory_count = max(len(segments) - (1 if plan.primary_segment else 0), 1)
    reports: dict[str, dict] = {}
    for segment in segments:
        prespecified = plan.primary_segment == segment
        subset = [outcome for outcome in outcomes if outcome.segment == segment]
        segment_level = level if prespecified else min(0.999, 1.0 - (1.0 - level) / exploratory_count)
        # 探索性分群不给序贯结论：没有越界判断，也没有重复置信区间。
        comparison = _compare(
            subset, plan, level=segment_level, boundary_z=boundary_z if prespecified else None
        )
        comparison["prespecified"] = prespecified
        comparison["role"] = "primary" if prespecified else "exploratory"
        comparison["supports_decision"] = prespecified
        comparison["multiplicity_note"] = (
            None
            if prespecified
            else f"未在计划中预先指定；区间按 {exploratory_count} 个分群做 Bonferroni 调整，仅作描述，不用于决策"
        )
        reports[segment] = comparison
    return reports


# ---------------------------------------------------------------------- 诊断
def _diagnostics(population: Population, counts: dict[str, dict], plan: AnalysisPlan) -> dict:
    matured_per_arm = {arm: counts.get(arm, {}).get("matured", 0) for arm in plan.comparison_arms}
    missing_per_arm = {arm: counts.get(arm, {}).get(MISSING, 0) for arm in plan.comparison_arms}
    immature_per_arm = {arm: counts.get(arm, {}).get(IMMATURE, 0) for arm in plan.comparison_arms}
    # 缺失比例只应看“已到期、本该有结果”的人：分母是已成熟 + 缺失。
    due_per_arm = {
        arm: matured_per_arm[arm] + missing_per_arm[arm] for arm in plan.comparison_arms
    }
    missing_share = {
        arm: (missing_per_arm[arm] / due_per_arm[arm]) if due_per_arm[arm] else 0.0
        for arm in plan.comparison_arms
    }
    missing_reasons: dict[str, int] = {}
    for outcome in population.outcomes:
        if outcome.status == MISSING and outcome.missing_reason:
            missing_reasons[outcome.missing_reason] = missing_reasons.get(outcome.missing_reason, 0) + 1
    return {
        "records": population.impact.as_dict(),
        "matured_per_arm": matured_per_arm,
        "immature_per_arm": immature_per_arm,
        "missing_per_arm": missing_per_arm,
        "due_per_arm": due_per_arm,
        "missing_share_per_arm": missing_share,
        "missing_reasons": missing_reasons,
        "minimum_matured_satisfied": min(matured_per_arm.values()) >= plan.min_matured_per_arm,
        "missing_share_within_tolerance": max(missing_share.values()) <= plan.max_missing_share,
    }


# ---------------------------------------------------------------------- 决策
def _decide(comparison: dict, diagnostics: dict, plan: AnalysisPlan, boundary_z: float | None) -> tuple[str, str]:
    if not diagnostics["minimum_matured_satisfied"]:
        return (
            DECISION_INSUFFICIENT,
            "每臂至少需要 {} 名已成熟成员，当前最少一臂仅 {} 名；结果保持未定，不能据此停测或宣称差异".format(
                plan.min_matured_per_arm, min(diagnostics["matured_per_arm"].values())
            ),
        )
    if not diagnostics["missing_share_within_tolerance"]:
        return (
            DECISION_INSUFFICIENT,
            "记录缺失比例超过计划上限 {:.0%}；结果保持未定，先修复记录再分析".format(plan.max_missing_share),
        )
    if boundary_z is None:
        return (DECISION_INSUFFICIENT, "尚无可比较的成熟数据，界值无法计算；结果保持未定")
    if comparison["crosses_boundary"]:
        return (
            DECISION_MATURE_DIFFERENCE,
            "已成熟成员的组间差异越过本次查看的序贯界值 (|z| = {:.3f} >= {:.3f})".format(
                abs(comparison["z"] or 0.0), boundary_z
            ),
        )
    return (DECISION_CONTINUE, "尚未越过序贯界值；按计划继续观察，不能宣称存在差异")


def _boundary_for_look(plan: AnalysisPlan, information_fractions: list[float]) -> tuple[float | None, float]:
    """按各次查看的实际累计信息量计算本次界值与名义 α。

    信息量为 0 时没有可比较的数据：不给界值、名义 α 为 0，决策必然是
    “证据不足”，不能沿用单次查看的 5%。界值只由本次之前已登记的查看与本次
    信息量决定，旧查看的界值已经写进旧报告，不会被追溯修改。
    """
    # 信息量为 0 的查看不可能越过任何界值，等价于不消耗 α；直接从序列里去掉。
    trimmed: list[float] = []
    for value in information_fractions:
        value = max(0.0, float(value))
        if not trimmed or value > trimmed[-1]:
            trimmed.append(value)
    trimmed = [value for value in trimmed if value > 0.0]
    if not trimmed:
        return (None, 0.0)
    boundaries = stats.sequential_boundaries(trimmed, plan.alpha)
    boundary_z = boundaries[-1]
    return boundary_z, 2.0 * float(norm.sf(boundary_z))


def _spent_alpha(plan: AnalysisPlan, information_fractions: list[float]) -> float | None:
    """截至本次查看实际消耗的族系误报率。"""
    trimmed: list[float] = []
    for value in information_fractions:
        value = max(0.0, float(value))
        if not trimmed or value > trimmed[-1]:
            trimmed.append(value)
    trimmed = [value for value in trimmed if value > 0.0]
    if not trimmed:
        return None
    boundaries = stats.sequential_boundaries(trimmed, plan.alpha)
    return stats.crossing_probability(tuple(boundaries), tuple(trimmed))


# ---------------------------------------------------------------------- 入口
def analyze(
    cohort: Cohort,
    ledger,
    plan: AnalysisPlan,
    *,
    as_of: int,
    batch_id: str,
    viewed_at: int | None = None,
    sequence: int = 1,
    matured_cap: int | None = None,
    prior_information_fractions: list[float] | None = None,
) -> AnalysisReport:
    """执行一次计划内分析。

    sequence 是一次实验中的第几次计划查看；信息量按“已成熟人数 ÷ 计划最终
    成熟人数”计算。未给最终成熟人数时本次视为 100% 信息量，等同于单次查看。
    界值只依赖计划与已发生的查看，旧查看记录不会被改写。
    """
    if plan.experiment_id != cohort.experiment_id:
        raise ValueError("分析计划的实验编号与入组登记不一致")
    if not 1 <= sequence <= plan.planned_looks:
        raise ValueError("查看序号必须在计划查看次数范围内")

    evidence, evidence_digest = build_evidence(cohort, ledger, plan, as_of, batch_id)
    batch = cohort.batch(batch_id)
    members = select_population(batch, as_of)
    full_population = classify(
        members,
        ledger,
        as_of,
        plan.conversion_window,
        grace=plan.recording_grace,
        arm_lookup=cohort.arm_of,
    )

    # 主比较人群：未指定分群即全人群；指定后只比较该分群。记录影响仍然
    # 描述整批证据（台账当时收到了什么），不随分群选择而改变。
    if plan.primary_segment is None:
        analyzed_member_rows = full_population.members
        analyzed_outcomes = full_population.outcomes
    else:
        analyzed_member_rows = tuple(
            member for member in full_population.members if member.segment == plan.primary_segment
        )
        analyzed_outcomes = tuple(
            outcome for outcome in full_population.outcomes if outcome.segment == plan.primary_segment
        )
    population = Population(
        members=analyzed_member_rows,
        outcomes=analyzed_outcomes,
        impact=full_population.impact,
    )
    outcomes = list(population.outcomes)
    counts = arm_counts(outcomes)
    for arm in plan.comparison_arms:
        bucket = counts.setdefault(
            arm, {IMMATURE: 0, PENDING: 0, CONVERTED: 0, NOT_CONVERTED: 0, MISSING: 0, "eligible": 0}
        )
        bucket["matured"] = bucket[CONVERTED] + bucket[NOT_CONVERTED]
        bucket["rate"] = (bucket[CONVERTED] / bucket["matured"]) if bucket["matured"] else None

    matured_total = sum(
        1 for outcome in outcomes if outcome.in_denominator and outcome.arm in plan.comparison_arms
    )
    cap = matured_cap if matured_cap is not None else plan.planned_max_matured
    information_fraction = 1.0 if cap is None else (min(1.0, matured_total / cap) if cap > 0 else 0.0)
    realized_fractions = list(prior_information_fractions or [])
    if len(realized_fractions) != sequence - 1:
        raise ValueError("此前各次查看的信息量序列长度与查看序号不符")
    realized_fractions.append(information_fraction)
    boundary_z, nominal_alpha = _boundary_for_look(plan, realized_fractions)
    alpha_spent = _spent_alpha(plan, realized_fractions)
    # 描述性区间固定用 95% 名义水平；可重复查看的结论用序贯界值对应的水平。
    # 两者分开报告，避免把 95% 区间误解成对多次查看都有效的承诺。
    level = 0.95
    claim_level = 1.0 - nominal_alpha if boundary_z is not None else None

    comparison = _compare(
        outcomes, plan, level=level, boundary_z=boundary_z, claim_level=claim_level
    )
    comparison["prespecified"] = True
    comparison["role"] = "primary"
    comparison["supports_decision"] = True
    comparison["multiplicity_note"] = None
    comparison["population"] = "all_segments" if plan.primary_segment is None else plan.primary_segment

    segments = segment_reports(list(full_population.outcomes), plan, level=level, boundary_z=boundary_z)
    diagnostics = _diagnostics(population, counts, plan)
    decision, basis = _decide(comparison, diagnostics, plan, boundary_z)

    return AnalysisReport(
        experiment_id=cohort.experiment_id,
        as_of=as_of,
        viewed_at=as_of if viewed_at is None else viewed_at,
        plan=plan,
        batch_id=batch_id,
        batch_digest=batch.digest,
        batch_received_at=batch.received_at,
        batch_size=len(batch.members),
        analyzed_members=len(analyzed_member_rows),
        evidence=evidence,
        evidence_digest=evidence_digest,
        population=population,
        counts=counts,
        comparison=comparison,
        segments=segments,
        diagnostics=diagnostics,
        decision=decision,
        decision_basis=basis,
        context=LookContext(
            sequence=sequence,
            information_fraction=information_fraction,
            boundary_z=boundary_z,
            nominal_alpha=nominal_alpha,
            alpha_spent=alpha_spent,
        ),
    )


# ---------------------------------------------------------------------- 兼容旧接口
def conversion_summary(
    subjects: list[str],
    experiment_id: str,
    arms: tuple[str, ...],
    ledger,
    cutoff: int,
) -> dict[str, dict]:
    """旧版描述性汇总：按事件发生时间截断，不做成熟度判定。

    保留用于兼容早期调用；正式复盘请使用 ``analyze``。
    """
    from .assignment import arm_for

    groups = {arm: set() for arm in arms}
    for subject in subjects:
        groups[arm_for(subject, experiment_id, arms)].add(subject)
    converted = {arm: set() for arm in arms}
    for event in ledger.through(cutoff):
        if not event.retracted:
            converted[arm_for(event.subject_id, experiment_id, arms)].add(event.subject_id)
    return {
        arm: {
            "eligible": len(members),
            "converted": len(converted[arm] & members),
            "rate": len(converted[arm] & members) / len(members) if members else 0.0,
        }
        for arm, members in groups.items()
    }
