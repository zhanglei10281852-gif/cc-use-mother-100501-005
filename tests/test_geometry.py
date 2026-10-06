"""几何与区间工具测试。"""

import unittest
from datetime import datetime, timedelta, timezone

from utility_coordination.geometry import (
    depth_ranges_overlap,
    fuzz_path,
    point_segment_distance,
    polyline_distance,
    segment_distance,
    windows_overlap,
)


class GeometryTests(unittest.TestCase):
    def test_point_segment_distance(self) -> None:
        self.assertAlmostEqual(point_segment_distance((0, 5), (-10, 0), (10, 0)), 5.0)
        self.assertAlmostEqual(point_segment_distance((30, 0), (-10, 0), (10, 0)), 20.0)
        self.assertAlmostEqual(point_segment_distance((3, 4), (0, 0), (0, 0)), 5.0)

    def test_segment_distance_intersecting(self) -> None:
        self.assertEqual(segment_distance((0, 0), (10, 10), (0, 10), (10, 0)), 0.0)

    def test_segment_distance_parallel(self) -> None:
        self.assertAlmostEqual(segment_distance((0, 0), (10, 0), (0, 4), (10, 4)), 4.0)

    def test_polyline_distance(self) -> None:
        first = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
        second = [(0.0, 3.0), (10.0, 3.0)]
        self.assertAlmostEqual(polyline_distance(first, second), 3.0)
        crossing = [(0.0, -1.0), (0.0, 1.0)]
        self.assertAlmostEqual(polyline_distance(first, crossing), 0.0)

    def test_depth_overlap_with_clearance(self) -> None:
        self.assertTrue(depth_ranges_overlap(1.0, 2.0, 1.5, 3.0))
        self.assertFalse(depth_ranges_overlap(1.0, 2.0, 3.0, 4.0))
        self.assertTrue(depth_ranges_overlap(1.0, 2.0, 2.4, 4.0, clearance=0.5))
        self.assertFalse(depth_ranges_overlap(1.0, 2.0, 2.6, 4.0, clearance=0.5))

    def test_windows_overlap(self) -> None:
        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t1 = t0 + timedelta(days=5)
        t2 = t0 + timedelta(days=3)
        t3 = t0 + timedelta(days=8)
        self.assertTrue(windows_overlap(t0, t1, t2, t3))
        self.assertFalse(windows_overlap(t0, t2, t1, t3))
        self.assertFalse(windows_overlap(t0, t0 + timedelta(days=1), t2, t3))

    def test_fuzz_path_rounds_to_grid(self) -> None:
        fuzzed = fuzz_path([(13.0, 27.0), (61.0, 99.0)], grid=50.0)
        self.assertEqual(fuzzed, ((0.0, 50.0), (50.0, 100.0)))


if __name__ == "__main__":
    unittest.main()
