"""HTTP 服务端 API 的端到端测试。"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from scenario_lab.server import create_server  # noqa: E402
from scenario_lab.service import ScenarioLabService  # noqa: E402

NOW = "2026-10-04T00:00:00+00:00"

SNAPSHOT_PAYLOAD = {
    "snapshot_id": "snap-2024",
    "period": "2024",
    "source": "正式账户台账",
    "copied_at": NOW,
    "records": [
        {"enterprise_id": "E001", "name": "钢一", "industry": "steel",
         "verified_emissions": 120000, "allowance_balance": 10000, "output": 60000},
        {"enterprise_id": "E002", "name": "造纸一", "industry": "paper",
         "verified_emissions": 8000, "allowance_balance": 500, "output": 6000},
        {"enterprise_id": "E003", "name": "电力一", "industry": "power",
         "verified_emissions": 200000, "allowance_balance": 30000, "output": 150000},
    ],
}


def params_payload(version_id: str, carry_ratio: float) -> dict:
    return {
        "version_id": version_id,
        "label": f"方案{version_id}",
        "coefficients": {"steel": 1.05, "power": 1.1, "default": 1.0},
        "adjustment_factor": 1.0,
        "carry_ratio": carry_ratio,
        "exemption_threshold": 10000,
        "exemption_enterprise_ids": [],
        "created_at": NOW,
    }


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmp = tempfile.TemporaryDirectory()
        cls.server = create_server(ScenarioLabService(cls._tmp.name))
        cls.port = cls.server.server_address[1]
        cls._thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls._thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls._thread.join()
        cls._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None) -> tuple[int, dict]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_full_flow_over_http(self) -> None:
        status, _ = self.call("GET", "/health")
        self.assertEqual(status, 200)

        status, snapshot = self.call("POST", "/snapshots", SNAPSHOT_PAYLOAD)
        self.assertEqual(status, 200, snapshot)
        for version_id, carry in (("http-v1", 0.5), ("http-v2", 0.2)):
            status, _ = self.call("POST", "/parameters", params_payload(version_id, carry))
            self.assertEqual(status, 200)

        runs = {}
        for name, version_id in (("基准", "http-v1"), ("紧结转", "http-v2")):
            status, scenario = self.call(
                "POST", "/scenarios",
                {"name": name, "snapshot_id": "snap-2024", "created_by": "核算专员"},
            )
            self.assertEqual(status, 200, scenario)
            scenario_id = scenario["scenario_id"]
            self.assertEqual(scenario["state"], "草稿")

            status, bound = self.call("POST", f"/scenarios/{scenario_id}/bind", {"parameter_version_id": version_id})
            self.assertEqual(status, 200, bound)
            self.assertEqual(bound["state"], "待核算")

            # 中断 → 恢复，验证可恢复批次在 API 层面可用
            status, paused = self.call(
                "POST", f"/scenarios/{scenario_id}/runs",
                {"chunk_size": 2, "interrupt_after_chunks": 1},
            )
            self.assertEqual(status, 200, paused)
            self.assertEqual(paused["status"], "已暂停")

            status, run = self.call("POST", f"/runs/{paused['run_id']}/resume", {})
            self.assertEqual(status, 200, run)
            self.assertEqual(run["status"], "已完成")
            runs[name] = run["run_id"]

            status, result = self.call("GET", f"/runs/{run['run_id']}/result")
            self.assertEqual(status, 200, result)
            self.assertEqual(result["metrics"]["enterprise_count"], 3)

        # 重放：指纹一致
        status, replay = self.call("POST", f"/runs/{runs['基准']}/replay", {})
        self.assertEqual(status, 200, replay)
        self.assertTrue(replay["identical"])

        # 比较：结转比例收紧后缺口与价格压力上升
        status, comparison = self.call("GET", f"/compare?left={runs['基准']}&right={runs['紧结转']}")
        self.assertEqual(status, 200, comparison)
        self.assertGreater(comparison["metric_delta"]["total_gap"]["delta"], 0)
        self.assertGreater(comparison["metric_delta"]["price_pressure_index"]["delta"], 0)

        # 未复核直接发布 → 409
        status, scenario = self.call("POST", "/scenarios", {"name": "未复核", "snapshot_id": "snap-2024", "created_by": "核算专员"})
        scenario_id = scenario["scenario_id"]
        self.call("POST", f"/scenarios/{scenario_id}/bind", {"parameter_version_id": "http-v1"})
        self.call("POST", f"/scenarios/{scenario_id}/runs", {"chunk_size": 2})
        status, error = self.call("POST", f"/scenarios/{scenario_id}/release", {"published_by": "交易运营员"})
        self.assertEqual(status, 409)

        # 复核 → 发布 → 已封存
        status, reviewed = self.call("POST", f"/scenarios/{scenario_id}/review", {"reviewer": "监管审计员"})
        self.assertEqual(status, 200, reviewed)
        self.assertEqual(reviewed["state"], "已确认")
        status, release = self.call("POST", f"/scenarios/{scenario_id}/release", {"published_by": "交易运营员"})
        self.assertEqual(status, 200, release)
        self.assertTrue(release["reference_only"])
        self.assertNotIn("outcomes", release)
        status, sealed = self.call("GET", f"/scenarios/{scenario_id}")
        self.assertEqual(sealed["state"], "已封存")

        # 取消：清理临时结果
        status, scenario = self.call("POST", "/scenarios", {"name": "待取消", "snapshot_id": "snap-2024", "created_by": "核算专员"})
        scenario_id = scenario["scenario_id"]
        self.call("POST", f"/scenarios/{scenario_id}/bind", {"parameter_version_id": "http-v1"})
        status, paused = self.call("POST", f"/scenarios/{scenario_id}/runs", {"chunk_size": 1, "interrupt_after_chunks": 1})
        status, cancelled = self.call("POST", f"/runs/{paused['run_id']}/cancel", {})
        self.assertEqual(status, 200, cancelled)
        self.assertEqual(cancelled["status"], "已取消")
        status, _ = self.call("GET", f"/runs/{paused['run_id']}/result")
        self.assertEqual(status, 404)

    def test_unknown_route_returns_404(self) -> None:
        status, _ = self.call("GET", "/no-such-thing")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
