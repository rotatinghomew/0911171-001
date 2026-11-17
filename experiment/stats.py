"""组间比较与重复查看的统计工具。

- 风险差（实验组 - 对照组）使用 Newcombe 混合得分区间，在小样本下比 Wald 区间稳定。
- 假设检验使用合并比例的标准误（H0 下的 z 统计量）。
- 多次查看使用 Lan-DeMets O'Brien-Fleming 消耗函数：第 k 次查看的名义界值
  由累计信息量决定，从而控制整个试验族系错误率，而不是每次查看都复用单次 5%。
"""

from __future__ import annotations

import math
from functools import lru_cache

import numpy as np
from scipy.stats import norm

_EPS = 1e-12


def _z_for_level(level: float) -> float:
    if not 0.0 < level < 1.0:
        raise ValueError("置信水平必须在 (0, 1) 之间")
    return float(norm.ppf(1.0 - (1.0 - level) / 2.0))


def wilson_interval(x: int, n: int, level: float = 0.95) -> tuple[float, float] | None:
    if n <= 0:
        return None
    if not 0 <= x <= n:
        raise ValueError("转化数必须位于 [0, n]")
    z = _z_for_level(level)
    p = x / n
    denominator = 1.0 + z * z / n
    centre = (p + z * z / (2.0 * n)) / denominator
    half = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n)) / denominator
    return (max(0.0, centre - half), min(1.0, centre + half))


def newcombe_difference(
    x_treatment: int,
    n_treatment: int,
    x_control: int,
    n_control: int,
    level: float = 0.95,
) -> tuple[float, float] | None:
    """风险差 p_treatment - p_control 的 Newcombe 混合得分区间。"""
    if n_treatment <= 0 or n_control <= 0:
        return None
    treatment = wilson_interval(x_treatment, n_treatment, level)
    control = wilson_interval(x_control, n_control, level)
    assert treatment is not None and control is not None
    p1, p0 = x_treatment / n_treatment, x_control / n_control
    lower = (p1 - p0) - math.sqrt((p1 - treatment[0]) ** 2 + (control[1] - p0) ** 2)
    upper = (p1 - p0) + math.sqrt((treatment[1] - p1) ** 2 + (p0 - control[0]) ** 2)
    return (max(-1.0, lower), min(1.0, upper))


def unpooled_standard_error(x_treatment: int, n_treatment: int, x_control: int, n_control: int) -> float | None:
    if n_treatment <= 0 or n_control <= 0:
        return None
    p1, p0 = x_treatment / n_treatment, x_control / n_control
    return math.sqrt(p1 * (1.0 - p1) / n_treatment + p0 * (1.0 - p0) / n_control)


def pooled_z_test(
    x_treatment: int,
    n_treatment: int,
    x_control: int,
    n_control: int,
) -> tuple[float | None, float | None]:
    """返回 (z, 双侧名义 p 值)；z 以实验组减对照组为正方向。"""
    if n_treatment <= 0 or n_control <= 0:
        return (None, None)
    diff = x_treatment / n_treatment - x_control / n_control
    pooled = (x_treatment + x_control) / (n_treatment + n_control)
    variance = pooled * (1.0 - pooled) * (1.0 / n_treatment + 1.0 / n_control)
    if variance <= 0.0:
        return (0.0, 1.0)
    z = diff / math.sqrt(variance)
    p_value = 2.0 * float(norm.sf(abs(z)))
    return (z, p_value)


def obf_spending(information_fraction: float, alpha: float) -> float:
    """Lan-DeMets O'Brien-Fleming 消耗函数（双侧）。"""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha 必须在 (0, 1) 之间")
    if information_fraction <= 0.0:
        return 0.0
    if information_fraction >= 1.0:
        return alpha
    z_alpha = float(norm.ppf(1.0 - alpha / 2.0))
    spent = 2.0 * float(norm.sf(z_alpha / math.sqrt(information_fraction)))
    return min(alpha, spent)


def _non_crossing_probability(
    boundaries: tuple[float, ...],
    fractions: tuple[float, ...],
    points_per_unit: float = 200.0,
    min_intervals: int = 200,
    max_intervals: int = 2400,
) -> float:
    """H0 下所有查看的 |Z_i| 都不越过各自界值的概率。

    使用 Armitage-McPherson-Rowe 递推数值积分。标准化得分过程在信息量分数
    处是布朗运动：增量独立、方差为 t_k - t_{k-1}。第 k 次查看的密度
        f_k(x) = ∫ f_{k-1}(y) * phi((x - y) / sqrt(d)) / sqrt(d) dy
    积分区间为上次查看的界值，用梯形法则近似。

    网格密度固定为每单位 z 若干点（而不是固定分段数），这样在二分搜索早期
    的大界值处仍然足够细，不同界值之间也可以直接比较。积分区间随界值增长
    而截断，越界质量已经为 0，不影响结果。
    """
    dimension = len(boundaries)
    if dimension == 0:
        return 1.0
    if dimension == 1:
        return float(2.0 * norm.cdf(boundaries[0]) - 1.0)

    def grid_for(boundary: float) -> tuple[np.ndarray, np.ndarray]:
        width = max(float(boundary), 0.0)
        intervals = int(min(max_intervals, max(min_intervals, math.ceil(points_per_unit * width))))
        points = np.linspace(-width, width, intervals + 1)
        spacing = 2.0 * width / intervals if intervals else 0.0
        weights = np.full(intervals + 1, spacing)
        weights[0] = weights[-1] = spacing / 2.0
        return points, weights

    points, weights = grid_for(boundaries[0])
    density = np.exp(-0.5 * points**2) / math.sqrt(2.0 * math.pi)

    for index in range(1, dimension):
        delta = fractions[index] - fractions[index - 1]
        next_points, next_weights = grid_for(boundaries[index])
        if delta <= _EPS:
            # 信息量没有增加：不积分，沿用密度并裁剪到更窄的界值。
            keep = np.abs(points) <= boundaries[index]
            if not np.any(keep):
                density = np.zeros_like(next_points)
            else:
                density = np.interp(
                    next_points, points[keep], density[keep], left=0.0, right=0.0
                )
            points, weights = next_points, next_weights
            continue
        spread = math.sqrt(delta)
        kernel = np.exp(-0.5 * ((next_points[:, None] - points[None, :]) / spread) ** 2)
        kernel /= math.sqrt(2.0 * math.pi) * spread
        density = kernel @ (density * weights)
        points, weights = next_points, next_weights

    return float(np.sum(density * weights))


def crossing_probability(boundaries: tuple[float, ...], fractions: tuple[float, ...]) -> float:
    if len(boundaries) != len(fractions):
        raise ValueError("界值与信息量序列长度必须一致")
    return 1.0 - _non_crossing_probability(tuple(boundaries), tuple(fractions))


@lru_cache(maxsize=128)
def _sequential_boundaries(fractions: tuple[float, ...], alpha: float) -> tuple[float, ...]:
    """逐次求解界值：第 k 次界值使前 k 次查看的累计越界概率等于消耗目标。"""
    boundaries: list[float] = []
    for index, fraction in enumerate(fractions):
        target = obf_spending(fraction, alpha)
        # 对称双侧界值的单调区间：目标概率越大，界值越小。
        def crossing(candidate: float) -> float:
            return crossing_probability(tuple(boundaries + [candidate]), fractions[: index + 1])

        if index == 0:
            boundary = float(norm.ppf(1.0 - target / 2.0)) if target > 0 else 20.0
            boundaries.append(min(20.0, boundary))
            continue

        # 求解界值使前 k 次查看的累计越界概率等于消耗目标。信息量间距很均匀时
        # 解自然非递增；间距极端不均时可能出现极小的回升，这是消耗函数本身
        # 的性质，报告层会给出实际消耗的 α 供核对。
        low, high = 0.0, 20.0
        for _ in range(40):
            middle = (low + high) / 2.0
            if crossing(middle) > target:
                low = middle
            else:
                high = middle
        boundaries.append((low + high) / 2.0)
    return tuple(boundaries)


def sequential_boundaries(fractions: list[float] | tuple[float, ...], alpha: float) -> list[float]:
    """按实际累计信息量计算各次查看的界值（z 尺度，双侧）。

    信息量为 0 表示尚无可用数据：调用方（报告层）必须把它当作“无法判断”，
    不能据此计算 p 值或界值。这里返回 ``inf`` 让任何比较都失败。
    """
    normalized = tuple(min(1.0, max(0.0, float(value))) for value in fractions)
    if not normalized:
        return []
    if any(b < a for a, b in zip(normalized, normalized[1:])):
        raise ValueError("信息量序列必须非递减")
    if normalized[0] <= 0.0:
        return [math.inf]
    return list(_sequential_boundaries(normalized, alpha))


def sequential_interval(
    difference: float,
    standard_error: float | None,
    boundary_z: float | None,
) -> tuple[float, float] | None:
    """重复置信区间：在全部查看上同时覆盖的区间；界值为无穷时不可估计。"""
    if standard_error is None or boundary_z is None or not math.isfinite(boundary_z):
        return None
    return (difference - boundary_z * standard_error, difference + boundary_z * standard_error)
