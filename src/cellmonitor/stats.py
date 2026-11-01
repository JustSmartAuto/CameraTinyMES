"""波动统计：均值 / 标准差 / CPK / 超差率。"""

from __future__ import annotations

import math


def basic_stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0}
    mean = sum(values) / n
    if n > 1:
        stdev = math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))
    else:
        stdev = 0.0
    out = {"n": n, "mean": mean, "stdev": stdev, "min": min(values), "max": max(values)}
    if stdev > 0:
        out["cp"] = (max(values) - min(values)) / (6 * stdev)
    return out


def capability(values: list[float], lower: float | None, upper: float | None) -> dict:
    """CPK 与超差率。需至少一个阈值；CPK 需要双侧阈值。"""
    stats = basic_stats(values)
    if not values:
        return stats
    n = stats["n"]
    mean, stdev = stats["mean"], stats["stdev"]
    out_of = 0
    for v in values:
        bad = (lower is not None and v < lower) or (upper is not None and v > upper)
        out_of += bool(bad)
    stats["out_of_spec"] = out_of
    stats["out_of_spec_rate"] = out_of / n if n else None
    if stdev > 0 and lower is not None and upper is not None:
        stats["cpu"] = (upper - mean) / (3 * stdev)
        stats["cpl"] = (mean - lower) / (3 * stdev)
        stats["cpk"] = min(stats["cpu"], stats["cpl"])
    return stats
