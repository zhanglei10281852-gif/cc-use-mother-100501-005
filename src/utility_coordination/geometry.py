"""二维几何计算（局部投影坐标系，单位：米）。

业务上只需要回答三类问题，因此不引入任何外部几何库：

1. 管段折线到施工范围多边形的距离；
2. 两个施工范围多边形之间的距离（安全缓冲/封路冲突）；
3. 点是否位于多边形内。
"""

from __future__ import annotations

from typing import Sequence

Point = tuple[float, float]
Line = tuple[Point, ...]
Polygon = tuple[Point, ...]


def _point_segment_distance(p: Point, a: Point, b: Point) -> float:
    px, py = p
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy
    if length_sq == 0.0:
        return ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = max(0.0, min(1.0, t))
    qx, qy = ax + t * dx, ay + t * dy
    return ((px - qx) ** 2 + (py - qy) ** 2) ** 0.5


def _orientation(a: Point, b: Point, c: Point) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a: Point, b: Point, c: Point) -> bool:
    return (
        _orientation(a, b, c) == 0.0
        and min(a[0], b[0]) <= c[0] <= max(a[0], b[0])
        and min(a[1], b[1]) <= c[1] <= max(a[1], b[1])
    )


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """两段线段是否相交，含共线重叠。"""
    o1, o2 = _orientation(a, b, c), _orientation(a, b, d)
    o3, o4 = _orientation(c, d, a), _orientation(c, d, b)
    if o1 == 0.0 and _on_segment(a, b, c):
        return True
    if o2 == 0.0 and _on_segment(a, b, d):
        return True
    if o3 == 0.0 and _on_segment(c, d, a):
        return True
    if o4 == 0.0 and _on_segment(c, d, b):
        return True
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def point_in_polygon(p: Point, polygon: Sequence[Point]) -> bool:
    """射线法判断点是否在多边形内（边界点算在内）。"""
    x, y = p
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        if _on_segment((ax, ay), (bx, by), (x, y)):
            return True
        if (ay > y) != (by > y):
            cross_x = ax + (y - ay) * (bx - ax) / (by - ay)
            if cross_x > x:
                inside = not inside
    return inside


def _edge_pairs_intersect_or_distance(line_a: Sequence[Point], line_b: Sequence[Point]) -> float:
    best = float("inf")
    for i in range(len(line_a) - 1):
        a, b = line_a[i], line_a[i + 1]
        for j in range(len(line_b) - 1):
            c, d = line_b[j], line_b[j + 1]
            if segments_intersect(a, b, c, d):
                return 0.0
            best = min(
                best,
                _point_segment_distance(a, c, d),
                _point_segment_distance(b, c, d),
                _point_segment_distance(c, a, b),
                _point_segment_distance(d, a, b),
            )
    return best


def linestring_polygon_distance(line: Sequence[Point], polygon: Sequence[Point]) -> float:
    """折线与简单多边形之间的最短距离，相交或穿入时为 0。"""
    if len(line) < 2 or len(polygon) < 3:
        raise ValueError("折线至少需要 2 个点，多边形至少需要 3 个点")
    if any(point_in_polygon(p, polygon) for p in line):
        return 0.0
    closed = list(polygon) + [polygon[0]]
    # 多边形顶点落在折线上也算相交
    for v in polygon:
        for i in range(len(line) - 1):
            if _on_segment(line[i], line[i + 1], v):
                return 0.0
    return _edge_pairs_intersect_or_distance(line, closed)


def polygons_distance(polygon_a: Sequence[Point], polygon_b: Sequence[Point]) -> float:
    """两个简单多边形之间的最短距离。"""
    if len(polygon_a) < 3 or len(polygon_b) < 3:
        raise ValueError("多边形至少需要 3 个点")
    if any(point_in_polygon(p, polygon_b) for p in polygon_a):
        return 0.0
    if any(point_in_polygon(p, polygon_a) for p in polygon_b):
        return 0.0
    closed_a = list(polygon_a) + [polygon_a[0]]
    closed_b = list(polygon_b) + [polygon_b[0]]
    return _edge_pairs_intersect_or_distance(closed_a, closed_b)
