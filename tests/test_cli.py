"""CLI 测试：通过 JSON 状态文件串联完整流程。"""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from utility_coordination.cli import main as cli_main


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = str(Path(self.tmp.name) / "state.json")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def run_cli(self, *argv: str) -> tuple[int, dict]:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = cli_main(["--state", self.state, *argv])
        output = buffer.getvalue().strip()
        return code, json.loads(output) if output else {}

    def test_full_flow_via_cli(self) -> None:
        code, seg = self.run_cli(
            "--actor", "gas-ops", "--role", "utility_owner", "--owner", "gas-corp",
            "submit-segment", "--segment-id", "gas-cli-1", "--utility", "gas",
            "--confidentiality", "internal", "--path", "0,0;100,0",
            "--depth-top", "1.2", "--depth-bottom", "1.8",
            "--effective-from", "2026-11-01T00:00:00+00:00", "--risk", "high",
        )
        self.assertEqual(code, 0)
        self.assertEqual(seg["revision"], 1)

        code, payload = self.run_cli(
            "--actor", "builder-1", "--role", "contractor",
            "apply", "--request-id", "req-cli-1", "--project", "road-7",
            "--path", "40,-3;60,3", "--impact-radius", "2", "--method", "open_cut",
            "--depth-top", "0.5", "--depth-bottom", "2",
            "--window-start", "2026-11-02T08:00:00+00:00",
            "--window-end", "2026-11-06T18:00:00+00:00",
        )
        self.assertEqual(code, 0)
        app_id = payload["application"]["application_id"]
        self.assertEqual(payload["application"]["state"], "confirming")

        # 高风险未确认，签发必须失败
        code, _ = self.run_cli("--actor", "coord-1", "--role", "coordinator",
                               "issue", "--application", app_id)
        self.assertEqual(code, 2)

        code, _ = self.run_cli(
            "--actor", "gas-ops", "--role", "utility_owner", "--owner", "gas-corp",
            "confirm", "--application", app_id, "--segment-id", "gas-cli-1", "--accept", "yes",
        )
        self.assertEqual(code, 0)
        code, _ = self.run_cli("--actor", "coord-1", "--role", "coordinator",
                               "cosign", "--application", app_id, "--approve", "yes")
        self.assertEqual(code, 0)
        code, permit = self.run_cli("--actor", "coord-1", "--role", "coordinator",
                                    "issue", "--application", app_id)
        self.assertEqual(code, 0)
        self.assertEqual(permit["state"], "active")

        # 审计员通过 CLI 还原工程方视角
        code, view = self.run_cli("--actor", "audit-1", "--role", "auditor",
                                  "audit-view", "--participant", "builder-1")
        self.assertEqual(code, 0)
        self.assertEqual(view["participant"]["actor_id"], "builder-1")
        self.assertEqual(len(view["decisions"]["permits"]), 1)
        secret_like = [s for s in view["segments"] if s["segment_id"] == "gas-cli-1"]
        self.assertEqual(secret_like[0]["disclosure"], "fuzzed")  # 工程方只见模糊坐标

        code, chain = self.run_cli("--actor", "audit-1", "--role", "auditor", "audit-verify")
        self.assertEqual(code, 0)
        self.assertTrue(chain["chain_valid"])

    def test_idempotent_apply_via_cli(self) -> None:
        args = [
            "--actor", "builder-1", "--role", "contractor",
            "apply", "--request-id", "req-cli-2", "--project", "road-8",
            "--path", "0,0;10,0", "--impact-radius", "1", "--method", "manual",
            "--depth-top", "0.5", "--depth-bottom", "1",
            "--window-start", "2026-11-02T08:00:00+00:00",
            "--window-end", "2026-11-03T18:00:00+00:00",
        ]
        _, first = self.run_cli(*args)
        _, second = self.run_cli(*args)
        self.assertEqual(first["application"]["application_id"],
                         second["application"]["application_id"])


if __name__ == "__main__":
    unittest.main()
