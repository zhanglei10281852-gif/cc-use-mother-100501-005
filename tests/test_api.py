"""HTTP API 端到端测试：真实启动服务器，用 urllib 调用。"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError

from utility_coordination.api import make_handler
from utility_coordination.service import CoordinationService
from utility_coordination.events import EventLog
from utility_coordination.timeutil import MutableClock


class ApiClient:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, actor: str | None = None, body: dict | None = None):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urlrequest.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor-Id", actor)
        try:
            with urlrequest.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = MutableClock("2026-09-01T00:00:00Z")
        self.service = CoordinationService(EventLog(None), clock=self.clock.now)
        handler = make_handler(self.service)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.api = ApiClient(f"http://127.0.0.1:{self.port}")

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def _register_all(self) -> None:
        for actor in [
            {"actor_id": "coord", "name": "协调员", "role": "coordinator"},
            {"actor_id": "gas", "name": "燃气", "role": "owner", "owner_id": "gas-bureau", "clearance": 3},
            {"actor_id": "ca", "name": "承包商", "role": "contractor"},
        ]:
            code, _ = self.api.call("POST", "/actors", body=actor)
            self.assertEqual(code, 201)

    def test_full_flow_over_http(self) -> None:
        self._register_all()
        # 无身份头 → 403
        code, err = self.api.call("POST", "/snapshots",
                                  body={"pipe_code": "g1", "utility_type": "gas", "classification": 3,
                                        "risk_level": "high", "geometry": [[0, 50], [80, 50]],
                                        "diameter_mm": 200, "pressure": "0.4",
                                        "effective_from": "2026-01-01T00:00:00Z"})
        self.assertEqual(code, 403)
        # 权属提交快照
        code, snap = self.api.call("POST", "/snapshots", actor="gas",
                                   body={"pipe_code": "g1", "utility_type": "gas", "classification": 3,
                                         "risk_level": "high", "geometry": [[0, 50], [80, 50]],
                                         "diameter_mm": 200, "pressure": "0.4",
                                         "effective_from": "2026-01-01T00:00:00Z"})
        self.assertEqual(code, 201)
        # 工程方申请
        code, resp = self.api.call("POST", "/applications", actor="ca", body={
            "project_code": "road-1",
            "work_area": [[30, 30], [60, 30], [60, 70], [30, 70]],
            "method": "open_cut",
            "window_start": "2026-10-10T08:00:00Z",
            "window_end": "2026-10-20T18:00:00Z",
            "impact_radius_m": 10.0,
        })
        self.assertEqual(code, 201)
        app_id = resp["application"]["application_id"]
        self.assertEqual(resp["application"]["status"], "in_review")
        # 工程方视角：敏感坐标与权属已掩码
        pc = resp["assessment"]["pipe_conflicts"][0]
        self.assertTrue(pc["geometry_redacted"])
        self.assertIsNone(pc["owner_id"])
        # 未会签直接签发 → 422
        code, err = self.api.call("POST", f"/applications/{app_id}/issue-permit", actor="coord", body={})
        self.assertEqual(code, 422)
        self.assertIn("高风险", err["error"])
        # 燃气会签
        code, _ = self.api.call("POST", f"/applications/{app_id}/respond", actor="gas",
                                body={"snapshot_id": snap["snapshot_id"], "decision": "approve"})
        self.assertEqual(code, 200)
        # 签发
        code, permit = self.api.call("POST", f"/applications/{app_id}/issue-permit", actor="coord", body={})
        self.assertEqual(code, 201)
        # 审计校验 + 事故还原
        code, verify = self.api.call("GET", "/audit/verify")
        self.assertEqual(code, 200)
        self.assertTrue(verify["ok"])
        code, timeline = self.api.call("GET", f"/replay/applications/{app_id}")
        self.assertEqual(code, 200)
        self.assertEqual(len(timeline["permits"]), 1)
        self.assertEqual(len(timeline["assessments"]), 1)

    def test_concurrent_requests_single_placeholder(self) -> None:
        self._register_all()
        self.api.call("POST", "/snapshots", actor="gas", body={
            "pipe_code": "g1", "utility_type": "gas", "classification": 1, "risk_level": "low",
            "geometry": [[0, 50], [80, 50]], "diameter_mm": 200, "pressure": "0.4",
            "effective_from": "2026-01-01T00:00:00Z"})
        results = []

        def submit(actor: str, x_offset: float) -> None:
            code, resp = self.api.call("POST", "/applications", actor=actor, body={
                "project_code": f"p-{actor}",
                "work_area": [[30 + x_offset, 30], [60, 30], [60, 70], [30 + x_offset, 70]],
                "method": "open_cut",
                "window_start": "2026-10-10T08:00:00Z",
                "window_end": "2026-10-20T18:00:00Z",
                "impact_radius_m": 10.0,
            })
            results.append((code, resp))

        # 第二个承包商也需要注册
        self.api.call("POST", "/actors",
                      body={"actor_id": "cb", "name": "承包商B", "role": "contractor"})
        t1 = threading.Thread(target=submit, args=("ca", 0.0))
        t2 = threading.Thread(target=submit, args=("cb", 2.0))
        t1.start(); t2.start()
        t1.join(); t2.join()
        statuses = sorted(resp["application"]["status"] for code, resp in results if code == 201)
        self.assertIn("in_review", statuses)
        self.assertIn("occupancy_blocked", statuses)


if __name__ == "__main__":
    unittest.main()
