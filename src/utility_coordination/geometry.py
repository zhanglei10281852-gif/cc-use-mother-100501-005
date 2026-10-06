"""平面几何与区间工具：施工走廊与管段的空间/时间冲突判定。

坐标采用平面直角坐标（米），深度为地表以下正值（米）。
所有函数均为纯函数，便于审计重放时得到一致结果。
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

Point = tuple[float, float]
Path = tuple[Point, ...]


def _sub(a: Point, b: Point) -> Point:
    return (a[0] - b[0], a[1] - b[1])


def _dot(a: Point, b: Point) -> float:
    return a[0] * b[0] + a[1] * b[1]


def _cross(a: Point, b: Point) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _norm(a: Point) -> float:
    return _dot(a, a) ** 0.5


def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    """点到有限线段的最短距离。"""
    ab = _sub(b, a)
    denom = _dot(ab, ab)
    if denom == 0.0:
        return _norm(_sub(p, a))
    t = max(0.0, min(1.0, _dot(_sub(p, a), ab) / denom))
    projection = (a[0] + t * ab[0], a[1] + t * ab[1])
    return _norm(_sub(p, projection))


def _orientation(a: Point, b: Point, c: Point) -> float:
    return _cross(_sub(b, a), _sub(c, a))


def _segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    o1 = _orientation(a, b, c)
    o2 = _orientation(a, b, d)
    o3 = _orientation(c, d, a)
    o4 = _orientation(c, d, b)
    if o1 == 0.0 and o2 == 0.0 and o3 == 0.0 and o4 == 0.0:
        # 共线：检查投影区间是否重叠
        return (
            min(a[0], b[0]) <= max(c[0], d[0])
            and min(c[0], d[0]) <= max(a[0], b[0])
            and min(a[1], b[1]) <= max(c[1], d[1])
            and min(c[1], d[1]) <= max(a[1], b[1])
        )
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def segment_distance(a: Point, b: Point, c: Point, d: Point) -> float:
    """两条有限线段之间的最短距离。"""
    if _segments_intersect(a, b, c, d):
        return 0.0
    return min(
        point_segment_distance(a, c, d),
        point_segment_distance(b, c, d),
        point_segment_distance(c, a, b),
        point_segment_distance(d, a, b),
    )


def _edges(path: Sequence[Point]) -> list[tuple[Point, Point]]:
    if len(path) < 2:
        raise ValueError("路径至少需要两个坐标点")
    return [(path[i], path[i + 1]) for i in range(len(path) - 1)]


def polyline_distance(first: Sequence[Point], second: Sequence[Point]) -> float:
    """两条折线之间的最短水平距离。"""
    return min(
        segment_distance(a, b, c, d)
        for a, b in _edges(first)
        for c, d in _edges(second)
    )


def depth_ranges_overlap(
    a_top: float, a_bottom: float, b_top: float, b_bottom: float, clearance: float = 0.0
) -> bool:
    """深度区间是否重叠；clearance 为要求的垂直净距（米）。"""
    if a_top > a_bottom or b_top > b_bottom:
        raise ValueError("深度区间上界不得大于下界")
    return a_top - clearance <= b_bottom and b_top - clearance <= a_bottom


def windows_overlap(start_a: datetime, end_a: datetime, start_b: datetime, end_b: datetime) -> bool:
    """两个时间窗是否相交（闭区间）。"""
    if end_a < start_a or end_b < start_b:
        raise ValueError("时间窗结束不得早于开始")
    return start_a <= end_b and start_b <= end_a


def fuzz_path(path: Sequence[Point], grid: float = 50.0) -> Path:
    """按网格取整坐标，用于对非授权角色模糊披露敏感位置。"""
    if grid <= 0:
        raise ValueError("模糊网格必须为正数")
    return tuple((round(x / grid) * grid, round(y / grid) * grid) for x, y in path)


def fuzz_depth(value: float, step: float = 1.0) -> float:
    """按步长取整深度，配合坐标模糊披露。"""
    return round(value / step) * step
