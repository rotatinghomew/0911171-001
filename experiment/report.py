from .assignment import arm_for
from .events import EventLedger


def conversion_summary(subjects: list[str], experiment_id: str, arms: tuple[str, ...], ledger: EventLedger, cutoff: int) -> dict[str, dict[str, int | float]]:
    groups = {arm: set() for arm in arms}
    for subject in subjects:
        groups[arm_for(subject, experiment_id, arms)].add(subject)
    converted = {arm: set() for arm in arms}
    for event in ledger.through(cutoff):
        if not event.retracted:
            arm = arm_for(event.subject_id, experiment_id, arms)
            converted[arm].add(event.subject_id)
    return {
        arm: {
            "eligible": len(members),
            "converted": len(converted[arm] & members),
            "rate": len(converted[arm] & members) / len(members) if members else 0.0,
        }
        for arm, members in groups.items()
    }
