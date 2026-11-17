"""入组登记与不可变入组批次。

成员只登记一次，分组由稳定标识推导。每次导出/登记形成不可变的 EnrollmentBatch：
分析报告绑定到具体批次编号与内容摘要，后续重复导出不会替换历史分析的入组依据。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Iterable

from .assignment import arm_for
from .util import digest


@dataclass(frozen=True)
class Enrollment:
    subject_id: str
    joined_at: int
    segment: str

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class EnrollmentBatch:
    batch_id: str
    received_at: int
    digest: str
    members: tuple[Enrollment, ...]

    def as_dict(self) -> dict:
        return {
            "batch_id": self.batch_id,
            "received_at": self.received_at,
            "digest": self.digest,
            "size": len(self.members),
        }

    def subjects(self) -> list[str]:
        return [member.subject_id for member in self.members]


class Cohort:
    def __init__(self, experiment_id: str, arms: tuple[str, ...]) -> None:
        if len(set(arms)) != len(arms) or not arms:
            raise ValueError("实验组必须非空且不重复")
        self.experiment_id = experiment_id
        self.arms = arms
        self.members: dict[str, Enrollment] = {}
        self.batches: dict[str, EnrollmentBatch] = {}

    # ------------------------------------------------------------- 登记
    def enroll(self, enrollment: Enrollment) -> str:
        if not enrollment.subject_id:
            raise ValueError("用户标识不能为空")
        previous = self.members.get(enrollment.subject_id)
        if previous is not None and previous != enrollment:
            raise ValueError("同一用户的入组信息不能被重新登记覆盖")
        self.members[enrollment.subject_id] = enrollment
        return self.arm_of(enrollment.subject_id)

    def enroll_many(self, enrollments: Iterable[Enrollment]) -> None:
        for enrollment in enrollments:
            self.enroll(enrollment)

    def arm_of(self, subject_id: str) -> str:
        return arm_for(subject_id, self.experiment_id, self.arms)

    # ------------------------------------------------------------- 批次
    def register_batch(self, batch_id: str, received_at: int, subjects: Iterable[str]) -> EnrollmentBatch:
        """把一批已登记成员固化为不可变快照。"""
        if batch_id in self.batches:
            raise ValueError(f"入组批次 {batch_id!r} 已存在，不可覆盖")
        if not batch_id:
            raise ValueError("批次编号不能为空")
        subject_ids = sorted(set(subjects))
        missing = [subject for subject in subject_ids if subject not in self.members]
        if missing:
            raise ValueError(f"批次包含未登记成员：{missing}")
        members = tuple(self.members[subject] for subject in subject_ids)
        payload = [member.as_dict() for member in members]
        batch = EnrollmentBatch(
            batch_id=batch_id,
            received_at=received_at,
            digest=digest({"experiment": self.experiment_id, "members": payload}),
            members=members,
        )
        self.batches[batch_id] = batch
        return batch

    def batch(self, batch_id: str) -> EnrollmentBatch:
        try:
            return self.batches[batch_id]
        except KeyError as error:
            raise KeyError(f"未知的入组批次 {batch_id!r}") from error

    # ------------------------------------------------------------- 兼容旧接口
    def observed_report(self, ledger, cutoff: int, segment: str | None = None) -> dict:
        members = [
            member for member in self.members.values()
            if member.joined_at <= cutoff and (segment is None or member.segment == segment)
        ]
        from .report import conversion_summary
        return {
            "experiment": self.experiment_id,
            "cutoff": cutoff,
            "members": [member.as_dict() for member in sorted(members, key=lambda member: member.subject_id)],
            "groups": conversion_summary(
                [member.subject_id for member in members], self.experiment_id, self.arms, ledger, cutoff,
            ),
        }
