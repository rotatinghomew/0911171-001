"""查看与停测登记：可追溯、不可改写。

每次正式分析都生成一次 LookRecord，内容包含分析时点、所用入组批次、
证据摘要和结论。记录按顺序哈希链接，任何事后改写都会破坏链。

停测决定单独登记 StopDecision，绑定到某次查看；决定之后不得再新增查看，
除非登记为偏离计划的探索性复查（不允许进入正式结论）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import digest


@dataclass(frozen=True)
class LookRecord:
    look_id: str
    experiment_id: str
    sequence: int
    viewed_at: int
    as_of: int
    batch_id: str
    evidence_digest: str
    analysis_digest: str
    planned: bool
    decision: str
    previous_hash: str | None
    record_hash: str

    def as_dict(self) -> dict:
        return {
            "look_id": self.look_id,
            "experiment_id": self.experiment_id,
            "sequence": self.sequence,
            "viewed_at": self.viewed_at,
            "as_of": self.as_of,
            "batch_id": self.batch_id,
            "evidence_digest": self.evidence_digest,
            "analysis_digest": self.analysis_digest,
            "planned": self.planned,
            "decision": self.decision,
            "previous_hash": self.previous_hash,
            "record_hash": self.record_hash,
        }


@dataclass(frozen=True)
class StopDecision:
    stopped_at: int
    reason: str
    look_id: str
    by: str

    def as_dict(self) -> dict:
        return {
            "stopped_at": self.stopped_at,
            "reason": self.reason,
            "look_id": self.look_id,
            "by": self.by,
        }


class MonitoringRegistry:
    def __init__(self) -> None:
        self.looks: list[LookRecord] = []
        self.stop_decision: StopDecision | None = None

    def _head_hash(self) -> str | None:
        return self.looks[-1].record_hash if self.looks else None

    def register_look(
        self,
        experiment_id: str,
        viewed_at: int,
        as_of: int,
        batch_id: str,
        evidence_digest: str,
        analysis_digest: str,
        planned: bool,
        decision: str,
    ) -> LookRecord:
        if self.stop_decision is not None:
            raise ValueError("实验已经登记停测，不能继续新增正式查看")
        if self.looks and viewed_at < self.looks[-1].viewed_at:
            raise ValueError("查看时间必须按顺序登记")
        previous_hash = self._head_hash()
        payload = {
            "experiment_id": experiment_id,
            "sequence": len(self.looks) + 1,
            "viewed_at": viewed_at,
            "as_of": as_of,
            "batch_id": batch_id,
            "evidence_digest": evidence_digest,
            "analysis_digest": analysis_digest,
            "planned": planned,
            "decision": decision,
            "previous_hash": previous_hash,
        }
        record = LookRecord(
            look_id=f"look-{payload['sequence']:03d}",
            previous_hash=previous_hash,
            record_hash=digest(payload),
            **{
                key: value
                for key, value in payload.items()
                if key not in ("previous_hash",)
            },
        )
        self.looks.append(record)
        return record

    def register_stop(self, stopped_at: int, reason: str, look_id: str, by: str) -> StopDecision:
        if self.stop_decision is not None:
            raise ValueError("停测决定已经登记")
        if not any(look.look_id == look_id for look in self.looks):
            raise ValueError("停测决定必须引用已有查看记录")
        if not reason or not by:
            raise ValueError("停测决定必须记录原因和登记人")
        decision = StopDecision(stopped_at=stopped_at, reason=reason, look_id=look_id, by=by)
        self.stop_decision = decision
        return decision

    def verify_chain(self) -> bool:
        previous_hash = None
        for look in self.looks:
            payload = {
                "experiment_id": look.experiment_id,
                "sequence": look.sequence,
                "viewed_at": look.viewed_at,
                "as_of": look.as_of,
                "batch_id": look.batch_id,
                "evidence_digest": look.evidence_digest,
                "analysis_digest": look.analysis_digest,
                "planned": look.planned,
                "decision": look.decision,
                "previous_hash": previous_hash,
            }
            if look.previous_hash != previous_hash or digest(payload) != look.record_hash:
                return False
            previous_hash = look.record_hash
        return True

    def as_dict(self) -> dict:
        return {
            "looks": [look.as_dict() for look in self.looks],
            "stop_decision": None if self.stop_decision is None else self.stop_decision.as_dict(),
            "chain_valid": self.verify_chain(),
        }
