"""确定性序列化与摘要工具。

分析记录必须可按字节复现：所有可归档对象先规范化为 JSON 兼容结构，
再用稳定的 JSON 编码计算 SHA-256。禁止 NaN，避免同一逻辑值产生不同摘要。
"""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any


class DigestError(ValueError):
    """摘要校验失败。"""


def canonical(value: Any) -> str:
    """返回稳定的 JSON 文本：键排序、紧凑分隔符、禁止 NaN。"""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def json_safe(value: Any) -> Any:
    """递归转换为可稳定序列化的结构；元组转为列表。"""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(json_safe(item) for item in value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise DigestError("分析结果不能包含 NaN 或无穷值")
        return value
    return value
