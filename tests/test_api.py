"""HTTP API 测试（标准库实现，随机端口）。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from utility_coordination import CoordinationService
from utility_coordination.api import make_handler


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.service = CoordinationService()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.service))
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, method: str, path: str, body: dict | None = None,
             actor: str = "coord-1", role: str = "coordinator", owner: str | None = None) -> tuple[int, dict]:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("X-Actor-Id", actor)
        request.add_header("X-Actor-Role", role)
        if owner:
            request.add_header("X-Owner-Id", owner)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        status, seg = self.call("POST", "/segments", {
            "segment_id": "gas-api-1", "utility": "gas", "confidentiality": "internal",
            "path": [[0, 0], [100, 0]], "depth_top": 1.2, "depth_bottom": 1.8,
            "effective_from": "2026-11-01T00:00:00+00:00", "risk": "high",
        }, actor="gas-ops", role="utility_owner", owner="gas-corp")
        self.assertEqual(status, 200, seg)
        self.assertEqual(seg["revision"], 1)

        status, payload = self.call("POST", "/applications", {
            "request_id": "req-api-1", "project_code": "road-7",
            "path": [[40, -3], [60, 3]], "impact_radius": 2.0, "method": "open_cut",
            "depth_top": 0.5, "depth_bottom": 2.0,
            "window_start": "2026-11-02T08:00:00+00:00",
            "window_end": "2026-11-06T18:00:00+00:00",
        }, actor="builder-1", role="contractor")
        self.assertEqual(status, 200, payload)
        app_id = payload["application"]["application_id"]
        self.assertEqual(payload["application"]["state"], "confirming")
        self.assertEqual(payload["assessment"]["hits"][0]["ref_id"], "gas-api-1")

        # 高风险未确认：不得签发
        status, blocked = self.call("POST", f"/applications/{app_id}/issue", {})
        self.assertEqual(status, 409)
        self.assertIn("不得默认为安全", blocked["message"])

        status, _ = self.call("POST", f"/applications/{app_id}/confirmations",
                              {"segment_id": "gas-api-1", "accept": True},
                              actor="gas-ops", role="utility_owner", owner="gas-corp")
        self.assertEqual(status, 200)
        status, _ = self.call("POST", f"/applications/{app_id}/cosign", {"approve": True})
        self.assertEqual(status, 200)
        status, permit = self.call("POST", f"/applications/{app_id}/issue", {})
        self.assertEqual(status, 200, permit)
        self.assertEqual(permit["state"], "active")

        status, view = self.call("GET", f"/audit/participants/builder-1", actor="audit-1", role="auditor")
        self.assertEqual(status, 200)
        self.assertEqual(view["participant"]["actor_id"], "builder-1")
        self.assertEqual(len(view["decisions"]["permits"]), 1)

        status, chain = self.call("GET", "/audit/verify", actor="audit-1", role="auditor")
        self.assertEqual(status, 200)
        self.assertTrue(chain["chain_valid"])

    def test_missing_identity_headers_rejected(self) -> None:
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/segments", method="GET")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(request)
        self.assertEqual(ctx.exception.code, 403)

    def test_unknown_route_is_404(self) -> None:
        status, _ = self.call("GET", "/no-such-route")
        self.assertEqual(status, 404)

    def test_bad_body_is_400(self) -> None:
        status, _ = self.call("POST", "/applications", {"request_id": "x"})
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
