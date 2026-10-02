"""HTTP API 端到端测试（真实 socket，随机端口）。"""

from __future__ import annotations

import http.client
import json
import os
import tempfile
import threading
import unittest
from datetime import date, timedelta
from http.server import HTTPServer

from src.dgpc.api import ApiHandler
from src.dgpc.db import connect, init_db
from src.dgpc.workflow import ServiceHub


class ApiCase(unittest.TestCase):
    def setUp(self) -> None:
        self._db_fd, self._db_path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self._db_fd)
        self.conn = connect(self._db_path, check_same_thread=False)
        init_db(self.conn)
        self.hub = ServiceHub(self.conn)

        class Handler(ApiHandler):
            hub = self.hub

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.today = date.today()
        self.iso = lambda offset: (
            self.today + timedelta(days=offset)).isoformat()
        self.tokens: dict[str, str] = {}
        self.users: dict[str, str] = {}
        # 首个账号（调度）经首启引导无令牌创建，其余账号由调度创建
        status, body = self.call(
            "POST", "/admin/users",
            {"name": "用户disp", "role": "dispatch"}, token=None)
        self.assertEqual(status, 200, body)
        self.users["disp"] = body["data"]["id"]
        self.tokens["disp"] = body["data"]["token"]
        for key, role in (
            ("rig", "rig"), ("geo", "geology"), ("maint", "maintenance"),
            ("pipe", "pipeline"), ("buyer", "commercial"),
        ):
            status, body = self.call(
                "POST", "/admin/users",
                {"name": f"用户{key}", "role": role},
                token=self.tokens["disp"])
            self.assertEqual(status, 200, body)
            self.users[key] = body["data"]["id"]
            self.tokens[key] = body["data"]["token"]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.conn.close()
        if os.path.exists(self._db_path):
            os.remove(self._db_path)

    def call(self, method: str, path: str, payload=None, *, token: str | None = ""):
        client = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        raw_body = json.dumps(payload, ensure_ascii=False).encode("utf-8") \
            if payload is not None else None
        client.request(method, path, body=raw_body, headers=headers)
        response = client.getresponse()
        raw = response.read().decode("utf-8")
        client.close()
        return response.status, json.loads(raw) if raw else {}

    def auth(self, key: str) -> str:
        return self.tokens[key]

    # -- 用例 ------------------------------------------------------------------
    def test_health_and_auth(self) -> None:
        status, body = self.call("GET", "/health", token=None)
        self.assertEqual(status, 200)
        status, _ = self.call("GET", "/wells", token=None)
        self.assertEqual(status, 401)

    def test_full_flow_over_http(self) -> None:
        # 区块、井、通道、路由
        _, body = self.call("POST", "/blocks", {"name": "区块甲"},
                            token=self.auth("disp"))
        block_id = body["data"]["id"]
        _, body = self.call("POST", f"/blocks/{block_id}/wells",
                            {"name": "西1井"}, token=self.auth("disp"))
        well_id = body["data"]["id"]
        _, body = self.call("POST", "/channels",
                            {"name": "西干线", "capacity": 100.0},
                            token=self.auth("pipe"))
        channel_id = body["data"]["id"]
        status, body = self.call(
            "POST", f"/wells/{well_id}/routes",
            {"channel_id": channel_id, "share": 1.0}, token=self.auth("pipe"))
        self.assertEqual(status, 200, body)

        # 解释 + 测试（地质提交）
        _, body = self.call(
            "POST", f"/wells/{well_id}/interpretations",
            {"formation": "栖霞组", "payload": {"porosity": 0.08}},
            token=self.auth("geo"))
        interp_id = body["data"]["id"]
        _, body = self.call(
            "POST", f"/wells/{well_id}/tests",
            {"flow_rate": 80.0, "test_date": self.iso(-2),
             "basis": "放喷测试6小时", "confidence": "high",
             "interpretation_id": interp_id},
            token=self.auth("geo"))
        test_id = body["data"]["id"]
        self.assertEqual(body["data"]["status"], "pending_review")

        # 提交人不能自批
        status, body = self.call(
            "POST", f"/tests/{test_id}/review", {"approve": True},
            token=self.auth("geo"))
        self.assertEqual(status, 403)
        # 调度复核
        status, body = self.call(
            "POST", f"/tests/{test_id}/review",
            {"approve": True, "note": "同意"}, token=self.auth("disp"))
        self.assertEqual(status, 200, body)
        # 登记产能
        status, body = self.call(
            "POST", f"/tests/{test_id}/offer",
            {"valid_from": self.iso(-2), "valid_to": self.iso(300)},
            token=self.auth("disp"))
        self.assertEqual(status, 200, body)
        offer_id = body["data"]["id"]

        # 可靠供应
        status, body = self.call(
            "GET", f"/supply/reliable?date={self.iso(8)}",
            token=self.auth("disp"))
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["data"]["firm_total"], 80.0, places=3)

        # 合同与承诺
        _, body = self.call(
            "POST", "/admin/contracts",
            {"name": "甲合同", "user_id": self.users["buyer"],
             "channel_id": channel_id}, token=self.auth("disp"))
        contract_id = body["data"]["id"]
        _, body = self.call(
            "POST", "/commitments",
            {"contract_id": contract_id, "gas_date": self.iso(8),
             "volume": 100.0}, token=self.auth("disp"))
        commitment_id = body["data"]["id"]
        self.assertAlmostEqual(
            body["data"]["open_gap"]["shortage"], 20.0, places=3)

        # 商业用户只看到自己的承诺，看不到内部数据
        status, body = self.call("GET", "/commitments",
                                 token=self.auth("buyer"))
        self.assertEqual([c["id"] for c in body["data"]], [commitment_id])
        status, _ = self.call("GET", "/supply/reliable",
                              token=self.auth("buyer"))
        self.assertEqual(status, 403)
        status, _ = self.call(
            "GET", f"/wells/{well_id}/trace?date={self.iso(8)}",
            token=self.auth("buyer"))
        self.assertEqual(status, 403)

        # 追溯：商业用户可从自己的承诺追到井和测试责任人
        status, body = self.call(
            "GET", f"/commitments/{commitment_id}/trace",
            token=self.auth("buyer"))
        self.assertEqual(status, 200)
        arr = body["data"]["arrangements"][0]
        self.assertEqual(arr["effective_offer"]["id"], offer_id)
        self.assertEqual(
            arr["effective_offer"]["lineage"][0]["test_batch"]["id"], test_id)

    def test_material_idempotency_over_http(self) -> None:
        _, block = self.call("POST", "/blocks", {"name": "B"},
                             token=self.auth("disp"))
        _, well = self.call("POST", f"/blocks/{block['data']['id']}/wells",
                            {"name": "W"}, token=self.auth("disp"))
        payload = {"material_no": "LOG-9", "kind": "well_log",
                   "well_id": well["data"]["id"], "payload": {"q": 1}}
        s1, b1 = self.call("POST", "/materials", payload,
                           token=self.auth("rig"))
        s2, b2 = self.call("POST", "/materials", payload,
                           token=self.auth("rig"))
        self.assertEqual((s1, b1["receipt"], b2["receipt"]),
                         (200, "new", "duplicate"))
        # 同号异值 -> 隔离
        payload["payload"] = {"q": 2}
        s3, b3 = self.call("POST", "/materials", payload,
                           token=self.auth("rig"))
        self.assertEqual((s3, b3["receipt"], b3["data"]["status"]),
                         (200, "quarantined", "quarantined"))
        # 井队无权处置，调度放行变体
        status, _ = self.call(
            "POST", f"/materials/{b3['data']['id']}/resolve",
            {"action": "accept", "variant_id": b3["data"]["variants"][0]["id"],
             "note": "校核后取新值"}, token=self.auth("rig"))
        self.assertEqual(status, 403)
        status, body = self.call(
            "POST", f"/materials/{b3['data']['id']}/resolve",
            {"action": "accept", "variant_id": b3["data"]["variants"][0]["id"],
             "note": "校核后取新值"}, token=self.auth("disp"))
        self.assertEqual(status, 200, body)

    def test_duty_queue_reports_open_gaps(self) -> None:
        _, body = self.call("GET", "/duty/queue", token=self.auth("disp"))
        self.assertEqual(body["data"]["as_of"][:4], str(self.today.year))
        self.assertIn("pending_test_reviews", body["data"])
