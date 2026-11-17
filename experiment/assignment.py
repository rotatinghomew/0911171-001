import hashlib


def arm_for(subject_id: str, experiment_id: str, arms: tuple[str, ...]) -> str:
    if not arms:
        raise ValueError("至少需要一个实验组")
    digest = hashlib.sha256(f"{experiment_id}:{subject_id}".encode()).digest()
    return arms[int.from_bytes(digest[:8], "big") % len(arms)]
