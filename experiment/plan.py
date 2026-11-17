"""预先登记的分析计划。

正式复盘要求“允许选择业务分群，但不能把事后挑出的有利分群当成预先比较”。
因此计划在查看数据前固定：
- 转化期限（每个人入组后相同的期限，单位秒）；
- 主比较分群（全人群或一个具名分群）；
- 对照臂与实验臂；
- 计划的查看次数与整体误报率。

任何与计划不符的分群只能作为描述性探索，不进入正式结论。
"""

from __future__ import annotations

from dataclasses import dataclass

from .util import digest


@dataclass(frozen=True)
class AnalysisPlan:
    experiment_id: str
    arms: tuple[str, ...]
    control_arm: str
    treatment_arm: str
    conversion_window: int
    primary_segment: str | None = None
    planned_looks: int = 1
    alpha: float = 0.05
    # 观察宽限期：用于把“尚未成熟的记录缺失”与真正的数据缺口区分开。
    recording_grace: int = 0
    # 计划最终可成熟人数，用作序贯信息量的分母。缺省时本次信息量视为
    # 100%，即等同于单次查看，无法区分早看与终看。
    planned_max_matured: int | None = None
    # 正式判断所需的最少成熟人数（每臂）与可接受的最大记录缺失比例。
    min_matured_per_arm: int = 5
    max_missing_share: float = 0.2
    label: str = ""

    def __post_init__(self) -> None:
        if self.control_arm not in self.arms or self.treatment_arm not in self.arms:
            raise ValueError("对照臂和实验臂必须属于实验分组")
        if self.control_arm == self.treatment_arm:
            raise ValueError("对照臂和实验臂不能相同")
        if self.conversion_window <= 0:
            raise ValueError("转化期限必须为正")
        if self.planned_looks < 1:
            raise ValueError("计划查看次数至少为 1")
        if not 0.0 < self.alpha < 1.0:
            raise ValueError("整体误报率必须在 (0, 1) 之间")
        if self.recording_grace < 0:
            raise ValueError("观察宽限期不能为负")
        if self.min_matured_per_arm < 0:
            raise ValueError("最少成熟人数不能为负")
        if not 0.0 <= self.max_missing_share <= 1.0:
            raise ValueError("最大记录缺失比例必须在 [0, 1] 之间")

    @property
    def comparison_arms(self) -> tuple[str, str]:
        return (self.treatment_arm, self.control_arm)

    def as_dict(self) -> dict:
        return {
            "experiment_id": self.experiment_id,
            "arms": list(self.arms),
            "control_arm": self.control_arm,
            "treatment_arm": self.treatment_arm,
            "conversion_window": self.conversion_window,
            "primary_segment": self.primary_segment,
            "planned_looks": self.planned_looks,
            "alpha": self.alpha,
            "recording_grace": self.recording_grace,
            "planned_max_matured": self.planned_max_matured,
            "min_matured_per_arm": self.min_matured_per_arm,
            "max_missing_share": self.max_missing_share,
            "label": self.label,
        }

    @property
    def digest(self) -> str:
        return digest(self.as_dict())
