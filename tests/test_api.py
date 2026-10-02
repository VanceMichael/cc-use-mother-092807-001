"""HTTP API 冒烟测试：在随机端口启动真实服务，走 HTTP 完成关键链路。"""

import json
import threading
import unittest
import urllib.error
import urllib.request
from http.client import RemoteDisconnected  # noqa: F401  (兼容旧文档引用)

from src.deep_gas.api import build_server
from src.deep_gas.db import connect  # noqa: F401  (确保包可导入)


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ApiSmokeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.port = _free_port()
        self.server = build_server(":memory:", self.port)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def request(self, method: str, path: str, body: dict | None = None,
                actor: str | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if actor:
            req.add_header("X-Actor-Id", actor)
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self) -> None:
        # 引导：登记岗位
        for aid, name, dept in (
            ("disp", "调度", "dispatch"), ("drill", "井队", "drilling"),
            ("geo", "地质", "geology"), ("maint", "维护", "maintenance"),
            ("pipe", "输气", "pipeline"), ("plan", "计划", "planning"),
            ("buyer_a", "甲方采购", "commercial")):
            status, _ = self.request("POST", "/api/actors",
                                     {"id": aid, "name": name, "department": dept})
            self.assertEqual(status, 201)

        # 无身份头被拒绝
        status, body = self.request("GET", "/api/workbench")
        self.assertEqual(status, 403)

        def ok(status: int) -> None:
            self.assertEqual(status, 201)

        ok(self.request("POST", "/api/blocks", {"code": "B1", "name": "区块"}, "disp")[0])
        ok(self.request("POST", "/api/wells",
                        {"block_code": "B1", "code": "W1", "name": "井一"}, "disp")[0])
        ok(self.request("POST", "/api/channels",
                        {"code": "C1", "name": "干线", "capacity": 200.0}, "pipe")[0])
        ok(self.request("POST", "/api/routes",
                        {"well_code": "W1", "channel_code": "C1"}, "pipe")[0])
        ok(self.request("POST", "/api/contracts", {"code": "K1", "customer": "甲方"}, "plan")[0])
        self.request("POST", "/api/contracts/audience",
                     {"contract_code": "K1", "actor_id": "buyer_a"}, "plan")
        ok(self.request("POST", "/api/commitments",
                        {"contract_code": "K1", "code": "CM1", "daily_volume": 60.0,
                         "valid_from": "2026-10-01", "valid_to": "2026-10-31"}, "plan")[0])
        self.request("POST", "/api/commitments/sources",
                     {"commitment_code": "CM1", "well_code": "W1"}, "plan")

        # 基线 + 测试批次 + 复核 + 转换
        ok(self.request("POST", "/api/baselines",
                        {"well_code": "W1", "rate": 30.0, "valid_from": "2026-09-01"}, "disp")[0])
        ok(self.request("POST", "/api/tests",
                        {"batch_no": "T1", "well_code": "W1", "test_date": "2026-09-20",
                         "flow_rate": 45.0}, "drill")[0])
        self.assertEqual(
            self.request("POST", "/api/tests/review",
                         {"batch_no": "T1", "approve": True}, "disp")[0], 200)
        self.assertEqual(
            self.request("POST", "/api/capacity/from-test",
                         {"batch_no": "T1", "valid_from": "2026-09-25",
                          "confidence": "low"}, "disp")[0], 201)

        # 回执幂等：第二次返回 duplicate
        payload = {"receipt_no": "R9", "source_kind": "well_log",
                   "payload": {"v": 1}}
        self.assertEqual(self.request("POST", "/api/receipts", payload, "drill")[0], 201)
        status, body = self.request("POST", "/api/receipts", payload, "drill")
        self.assertEqual(body["outcome"], "duplicate")
        # 同号异值 → 409 quarantined
        status, _ = self.request("POST", "/api/receipts",
                                 {"receipt_no": "R9", "source_kind": "well_log",
                                  "payload": {"v": 2}}, "drill")
        self.assertEqual(status, 409)

        # 评估供气并读取（商业用户授权可见）
        status, ev = self.request("POST", "/api/supply/evaluate",
                                  {"commitment_code": "CM1", "gas_date": "2026-10-05"}, "disp")
        self.assertEqual(status, 201)
        self.assertIn(ev["status"], {"firm", "conditional", "shortfall"})
        status, decision = self.request(
            "GET", "/api/supply/decision?commitment_code=CM1&gas_date=2026-10-05",
            actor="buyer_a")
        self.assertEqual(status, 200)
        self.assertEqual(decision["evidence"]["commitment_code"], "CM1")

        # 地质自批例外被拒（403）
        status, ex = self.request("POST", "/api/exceptions",
                                  {"well_code": "W1", "requested_rate": 50.0,
                                   "valid_from": "2026-10-20", "valid_to": "2026-10-21",
                                   "justification": "窗口上调"}, "geo")
        self.assertEqual(status, 201)
        status, body = self.request("POST", "/api/exceptions/review",
                                    {"exception_id": ex["id"], "approve": True}, "geo")
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
