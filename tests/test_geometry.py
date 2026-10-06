"""几何工具测试。"""

import unittest

from utility_coordination.geometry import (
    linestring_polygon_distance,
    point_in_polygon,
    polygons_distance,
    segments_intersect,
)

SQUARE = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]


class GeometryTests(unittest.TestCase):
    def test_point_in_polygon(self) -> None:
        self.assertTrue(point_in_polygon((5.0, 5.0), SQUARE))
        self.assertFalse(point_in_polygon((15.0, 5.0), SQUARE))
        self.assertTrue(point_in_polygon((0.0, 5.0), SQUARE))  # 边界

    def test_segments_intersect(self) -> None:
        self.assertTrue(segments_intersect((0, 0), (10, 10), (0, 10), (10, 0)))
        self.assertFalse(segments_intersect((0, 0), (1, 1), (5, 5), (6, 6)))
        self.assertTrue(segments_intersect((0, 0), (10, 0), (5, -5), (5, 5)))

    def test_closed_edge_counts(self) -> None:
        # 只与多边形闭合边（末点→首点）相交的折线必须被识别
        line = [(-5.0, 0.0), (5.0, 0.0)]
        self.assertEqual(linestring_polygon_distance(line, SQUARE), 0.0)

    def test_polyline_outside_returns_gap(self) -> None:
        line = [(-10.0, 5.0), (-3.0, 5.0)]
        self.assertAlmostEqual(linestring_polygon_distance(line, SQUARE), 3.0, places=6)

    def test_polyline_through_polygon(self) -> None:
        line = [(-5.0, 5.0), (15.0, 5.0)]
        self.assertEqual(linestring_polygon_distance(line, SQUARE), 0.0)

    def test_polygons_overlap_via_closed_edge(self) -> None:
        other = [(-5.0, -5.0), (0.0, -5.0), (0.0, 5.0), (-5.0, 5.0)]
        self.assertEqual(polygons_distance(SQUARE, other), 0.0)

    def test_disjoint_polygons_distance(self) -> None:
        other = [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)]
        self.assertAlmostEqual(polygons_distance(SQUARE, other), 10.0, places=6)

    def test_bad_inputs(self) -> None:
        with self.assertRaises(ValueError):
            linestring_polygon_distance([(0.0, 0.0)], SQUARE)
        with self.assertRaises(ValueError):
            polygons_distance([(0.0, 0.0), (1.0, 0.0)], SQUARE)


if __name__ == "__main__":
    unittest.main()
