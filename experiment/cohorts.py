from dataclasses import asdict, dataclass

from .assignment import arm_for
from .report import conversion_summary


@dataclass(frozen=True)
class Enrollment:
    subject_id: str
    joined_at: int
    segment: str


class Cohort:
    def __init__(self, experiment_id: str, arms: tuple[str, ...]) -> None:
        if len(set(arms)) != len(arms) or not arms:
            raise ValueError("实验组必须非空且不重复")
        self.experiment_id = experiment_id
        self.arms = arms
        self.members: dict[str, Enrollment] = {}

    def enroll(self, enrollment: Enrollment) -> str:
        previous = self.members.get(enrollment.subject_id)
        if previous is not None and previous != enrollment:
            raise ValueError("同一用户的入组信息不能被重新登记覆盖")
        self.members[enrollment.subject_id] = enrollment
        return arm_for(enrollment.subject_id, self.experiment_id, self.arms)

    def observed_report(self, ledger, cutoff: int, segment: str | None = None) -> dict:
        members = [
            member for member in self.members.values()
            if member.joined_at <= cutoff and (segment is None or member.segment == segment)
        ]
        return {
            "experiment": self.experiment_id,
            "cutoff": cutoff,
            "members": [asdict(member) for member in sorted(members, key=lambda member: member.subject_id)],
            "groups": conversion_summary(
                [member.subject_id for member in members], self.experiment_id, self.arms, ledger, cutoff,
            ),
        }
