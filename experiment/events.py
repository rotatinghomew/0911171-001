"""事件台账与记录完整性登记。

台账是追加式版本历史：同一 event_id 的每次上报都保留到达时间（received_at），
分析按 as_of 取“当时已收到的最新版本”。因此后续补数、更正或撤回不会改写
旧分析所依据的快照。

记录缺失分两类：
- 观察宽限期未结束（由分析计划推导，不写入台账）；
- 显式登记的数据缺口（Gap），例如埋点中断或上游导出不完整。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Conversion:
    event_id: str
    subject_id: str
    occurred_at: int
    retracted: bool = False
    # 台账首次收到该版本的时刻；省略时视为与事件发生时刻相同（兼容旧调用）。
    received_at: int | None = None

    @property
    def effective_received_at(self) -> int:
        return self.occurred_at if self.received_at is None else self.received_at

    def with_received_at(self, received_at: int) -> "Conversion":
        return Conversion(self.event_id, self.subject_id, self.occurred_at, self.retracted, received_at)

    def as_dict(self) -> dict:
        return {
            "event_id": self.event_id,
            "subject_id": self.subject_id,
            "occurred_at": self.occurred_at,
            "retracted": self.retracted,
            "received_at": self.effective_received_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Conversion":
        return cls(
            str(payload["event_id"]),
            str(payload["subject_id"]),
            int(payload["occurred_at"]),
            bool(payload["retracted"]),
            int(payload["received_at"]),
        )


@dataclass(frozen=True)
class Gap:
    subject_id: str
    reason: str
    recorded_at: int
    resolved_at: int | None = None

    def active_at(self, as_of: int) -> bool:
        return self.recorded_at <= as_of and (self.resolved_at is None or self.resolved_at > as_of)

    def as_dict(self) -> dict:
        return {
            "subject_id": self.subject_id,
            "reason": self.reason,
            "recorded_at": self.recorded_at,
            "resolved_at": self.resolved_at,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "Gap":
        resolved = payload.get("resolved_at")
        return cls(
            str(payload["subject_id"]),
            str(payload["reason"]),
            int(payload["recorded_at"]),
            None if resolved is None else int(resolved),
        )


class EventLedger:
    def __init__(self) -> None:
        self._revisions: dict[str, list[Conversion]] = {}
        self._gaps: dict[str, list[Gap]] = {}

    # ---------------------------------------------------------------- 事件
    def record(self, event: Conversion) -> None:
        if not event.event_id or not event.subject_id:
            raise ValueError("事件编号和用户标识不能为空")
        received = event.effective_received_at
        if received < event.occurred_at:
            raise ValueError("事件到达时间不能早于发生时间")
        revisions = self._revisions.setdefault(event.event_id, [])
        if revisions:
            latest = revisions[-1]
            if latest.subject_id != event.subject_id:
                raise ValueError("同一事件编号不能改挂到其他用户")
            if received < latest.effective_received_at:
                raise ValueError("同一事件的版本必须按到达时间顺序登记")
        revisions.append(event.with_received_at(received))

    def revisions(self, event_id: str) -> list[Conversion]:
        return list(self._revisions.get(event_id, []))

    def events_as_of(self, as_of: int) -> list[Conversion]:
        """返回 as_of 时已收到的最新版本事件。"""
        effective = []
        for revisions in self._revisions.values():
            visible = [event for event in revisions if event.effective_received_at <= as_of]
            if visible:
                effective.append(visible[-1])
        return sorted(effective, key=lambda event: (event.occurred_at, event.event_id))

    def revision_counts_as_of(self, as_of: int) -> dict[str, int]:
        return {
            event_id: sum(1 for event in revisions if event.effective_received_at <= as_of)
            for event_id, revisions in self._revisions.items()
            if any(event.effective_received_at <= as_of for event in revisions)
        }


    def through(self, cutoff: int) -> list[Conversion]:
        """兼容旧接口：按事件发生时间取当前（全量）最新版本。"""
        return sorted(
            (revisions[-1] for revisions in self._revisions.values() if revisions[-1].occurred_at <= cutoff),
            key=lambda event: (event.occurred_at, event.event_id),
        )

    # ---------------------------------------------------------------- 缺口
    def mark_gap(self, subject_id: str, reason: str, at: int) -> Gap:
        if not subject_id or not reason:
            raise ValueError("缺口必须指明用户和原因")
        gaps = self._gaps.setdefault(subject_id, [])
        if gaps and gaps[-1].resolved_at is None:
            raise ValueError("该用户已有未解除的记录缺口")
        gap = Gap(subject_id, reason, at)
        gaps.append(gap)
        return gap

    def resolve_gap(self, subject_id: str, at: int) -> Gap:
        gaps = self._gaps.get(subject_id, [])
        if not gaps or gaps[-1].resolved_at is not None:
            raise ValueError("该用户没有待解除的记录缺口")
        active = gaps[-1]
        if at < active.recorded_at:
            raise ValueError("缺口解除时间不能早于登记时间")
        resolved = Gap(active.subject_id, active.reason, active.recorded_at, at)
        gaps[-1] = resolved
        return resolved

    def gaps_as_of(self, as_of: int) -> list[Gap]:
        """返回 as_of 时处于活动状态的数据缺口。"""
        active = []
        for gaps in self._gaps.values():
            for gap in gaps:
                if gap.active_at(as_of):
                    active.append(gap)
        return sorted(active, key=lambda gap: (gap.recorded_at, gap.subject_id))

    # ---------------------------------------------------------------- 快照
    def snapshot(self, as_of: int) -> dict:
        counts = self.revision_counts_as_of(as_of)
        events = [event.as_dict() for event in self.events_as_of(as_of)]
        for payload in events:
            payload["revisions"] = counts.get(payload["event_id"], 1)
        return {
            "as_of": as_of,
            "events": events,
            "gaps": [gap.as_dict() for gap in self.gaps_as_of(as_of)],
        }
