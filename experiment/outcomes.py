"""按入组后固定转化期限判定每个人的结果。

每个人的观察窗口为 [joined_at, joined_at + conversion_window]。两个时钟分开：

- occurred_at：事件在业务时钟上的发生时刻，决定是否落在个人窗口内；
- received_at：台账收到该版本的时刻，决定分析时是否“当时已知”。

分析只使用 received_at <= as_of 的版本，因此当时未收到的补数不会污染旧结论。

结果状态：
- 未成熟 (immature)：窗口尚未走完，不进入转化率分母；
- 待定 (pending)：窗口刚结束、仍在计划允许的记录到达宽限期内，暂时无法判断。
  它不等于“未转化”，也不计入记录缺失率，只说明本次查看还看不到结果；
- 已成熟且转化 (converted)：窗口内至少有一条当时已收到的有效记录；
- 已成熟且未转化 (not_converted)：窗口走完、记录完整且无有效记录；
- 记录缺失 (missing)：窗口走完、已过宽限期，但记录不可用。细分原因：
    data_gap           登记了尚未解除的数据缺口；
    retracted_record   窗口内唯一记录被撤回，无法确认是否转化。

期限外、入组前、重复与撤回记录都逐条计数，报告会说明它们对结果的影响。
"""

from __future__ import annotations

from dataclasses import dataclass

from .cohorts import Enrollment, EnrollmentBatch
from .events import Conversion, Gap

IMMATURE = "immature"
PENDING = "pending"
CONVERTED = "converted"
NOT_CONVERTED = "not_converted"
MISSING = "missing"

MISSING_GAP = "data_gap"
MISSING_RETRACTED = "retracted_record"
PENDING_GRACE = "reporting_grace"

ALL_STATUSES = (IMMATURE, PENDING, CONVERTED, NOT_CONVERTED, MISSING)


@dataclass(frozen=True)
class Outcome:
    subject_id: str
    arm: str
    segment: str
    joined_at: int
    deadline: int
    status: str
    converted_at: int | None = None
    missing_reason: str | None = None
    evidence_event_id: str | None = None
    window_records: int = 0

    @property
    def in_denominator(self) -> bool:
        return self.status in (CONVERTED, NOT_CONVERTED)

    def as_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "arm": self.arm,
            "segment": self.segment,
            "joined_at": self.joined_at,
            "deadline": self.deadline,
            "status": self.status,
            "converted_at": self.converted_at,
            "missing_reason": self.missing_reason,
            "evidence_event_id": self.evidence_event_id,
            "window_records": self.window_records,
        }


@dataclass(frozen=True)
class RecordImpact:
    """记录层面的完整性诊断（按记录条数计）。"""

    out_of_window: int
    before_join: int
    duplicate: int
    retracted: int
    retracted_within_window: int
    outside_population: int
    revisions: int
    late_first_receipt: int

    def as_dict(self) -> dict:
        return {
            "out_of_window_records": self.out_of_window,
            "before_join_records": self.before_join,
            "duplicate_records": self.duplicate,
            "retracted_records": self.retracted,
            "retracted_records_in_window": self.retracted_within_window,
            "records_outside_population": self.outside_population,
            "revisions_received": self.revisions,
            "late_first_receipt_records": self.late_first_receipt,
        }


@dataclass(frozen=True)
class Population:
    members: tuple[Enrollment, ...]
    outcomes: tuple[Outcome, ...]
    impact: RecordImpact

    def by_subject(self) -> dict[str, Outcome]:
        return {outcome.subject_id: outcome for outcome in self.outcomes}


def select_population(batch: EnrollmentBatch, as_of: int) -> tuple[Enrollment, ...]:
    """批次是分析所用的固定入组依据；只纳入截止前已入组的成员。

    分群不在这里筛掉：报告需要对所有分群给出描述性结果，只有计划指定的
    分群才进入支持决策的主比较。
    """
    if batch.received_at > as_of:
        raise ValueError("分析时刻早于入组批次接收时间，不能使用该批次")
    members = [member for member in batch.members if member.joined_at <= as_of]
    return tuple(sorted(members, key=lambda member: member.subject_id))


def classify(
    members: tuple[Enrollment, ...],
    ledger,
    as_of: int,
    conversion_window: int,
    grace: int = 0,
    arm_lookup=None,
) -> Population:
    if arm_lookup is None:
        raise ValueError("必须提供分组函数")
    events = ledger.events_as_of(as_of)
    revisions = sum(ledger.revision_counts_as_of(as_of).values())
    gaps = ledger.gaps_as_of(as_of)
    gap_by_subject: dict[str, Gap] = {gap.subject_id: gap for gap in gaps}

    events_by_subject: dict[str, list[Conversion]] = {}
    for event in events:
        events_by_subject.setdefault(event.subject_id, []).append(event)

    population_ids = {member.subject_id for member in members}

    out_of_window = before_join = duplicate = retracted_total = retracted_in_window = 0
    late_first_receipt = 0
    outcomes: list[Outcome] = []

    for member in members:
        deadline = member.joined_at + conversion_window
        subject_events = events_by_subject.get(member.subject_id, [])

        active_events: list[Conversion] = []
        window_records = 0
        retracted_in_window = 0
        for event in subject_events:
            in_window = member.joined_at <= event.occurred_at <= deadline
            if in_window and event.effective_received_at - event.occurred_at > conversion_window:
                # 窗口内事件，但首次到达就比转化期限还晚：这是补数而非实时上报。
                late_first_receipt += 1
            if event.retracted:
                retracted_total += 1
                retracted_in_window += int(in_window)
            elif event.occurred_at < member.joined_at:
                before_join += 1
            elif event.occurred_at > deadline:
                out_of_window += 1
            else:
                window_records += 1
                active_events.append(event)

        active_events.sort(key=lambda event: (event.occurred_at, event.event_id))
        duplicate += max(0, len(active_events) - 1)

        if as_of < deadline:
            status = IMMATURE
            missing_reason = None
        elif active_events:
            status = CONVERTED
            missing_reason = None
        elif member.subject_id in gap_by_subject:
            status = MISSING
            missing_reason = MISSING_GAP
        elif retracted_in_window:
            status = MISSING
            missing_reason = MISSING_RETRACTED
        elif as_of < deadline + grace:
            status = PENDING
            missing_reason = PENDING_GRACE
        else:
            status = NOT_CONVERTED
            missing_reason = None

        outcomes.append(
            Outcome(
                subject_id=member.subject_id,
                arm=arm_lookup(member.subject_id),
                segment=member.segment,
                joined_at=member.joined_at,
                deadline=deadline,
                status=status,
                converted_at=active_events[0].occurred_at if active_events else None,
                missing_reason=missing_reason,
                evidence_event_id=active_events[0].event_id if active_events else None,
                window_records=window_records,
            )
        )

    outside = sum(1 for event in events if event.subject_id not in population_ids)
    impact = RecordImpact(
        out_of_window=out_of_window,
        before_join=before_join,
        duplicate=duplicate,
        retracted=retracted_total,
        retracted_within_window=retracted_in_window,
        outside_population=outside,
        revisions=revisions,
        late_first_receipt=late_first_receipt,
    )
    return Population(members=members, outcomes=tuple(outcomes), impact=impact)


def arm_counts(outcomes: tuple[Outcome, ...] | list[Outcome]) -> dict[str, dict]:
    counts: dict[str, dict] = {}
    for outcome in outcomes:
        bucket = counts.setdefault(
            outcome.arm,
            {IMMATURE: 0, PENDING: 0, CONVERTED: 0, NOT_CONVERTED: 0, MISSING: 0, "eligible": 0},
        )
        bucket["eligible"] += 1
        bucket[outcome.status] += 1
    return counts
