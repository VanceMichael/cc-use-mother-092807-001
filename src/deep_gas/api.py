"""HTTP JSON API（标准库 http.server，无第三方依赖）。

鉴权：除参与方注册外，所有请求需带 X-Actor-Id 头；部门与数据范围在服务层强制。
启动：python -m src.deep_gas.api --db data/deep_gas.db --port 8080
"""

from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import auth, catalog, inbound, operations, reports, supply, testing
from .auth import Actor, get_actor
from .capacity_exceptions import pending_exceptions, review_exception, submit_exception
from .db import connect
from .errors import AuthorizationError, DomainError, NotFound, Quarantined
from .recovery import workbench


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DeepGasAPI/1.0"

    # --- 框架辅助 ---
    def _send(self, status: int, body: dict | list) -> None:
        data = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise DomainError(f"请求体不是合法 JSON: {exc}") from exc
        if not isinstance(value, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return value

    @property
    def db(self) -> sqlite3.Connection:
        return self.server.db  # type: ignore[attr-defined]

    def _actor(self) -> Actor:
        actor_id = self.headers.get("X-Actor-Id", "").strip()
        if not actor_id:
            raise AuthorizationError("缺少 X-Actor-Id 头")
        try:
            return get_actor(self.db, actor_id)
        except NotFound as exc:
            # 身份不合法与未注册统一返回 403，避免暴露参与方是否存在
            raise AuthorizationError("身份无效或未注册") from exc

    def _handle(self, fn):
        """统一异常映射；所有请求串行执行，保证共享连接安全。"""
        try:
            with self.server.lock:  # type: ignore[attr-defined]
                fn()
        except AuthorizationError as exc:
            self._send(403, {"error": "forbidden", "message": str(exc)})
        except NotFound as exc:
            self._send(404, {"error": "not_found", "message": str(exc)})
        except Quarantined as exc:
            self._send(409, {"error": "quarantined", "message": str(exc)})
        except DomainError as exc:
            self._send(400, {"error": "domain_violation", "message": str(exc)})

    def log_message(self, fmt: str, *args) -> None:  # 安静日志
        return

    # --- 路由 ---
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        qs = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        self._handle(lambda: self._route_get(path, qs))

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        self._handle(lambda: self._route_post(path))

    def _route_get(self, path: str, qs: dict[str, str]) -> None:
        actor = self._actor()
        if path == "/api/workbench":
            self._send(200, workbench(self.db, actor))
        elif path == "/api/tests/pending":
            self._send(200, testing.pending_tests(self.db, actor))
        elif path == "/api/claims":
            auth.reject_commercial(actor, "查看产能谱系")
            self._send(200, testing.list_claims(self.db, qs.get("well_code")))
        elif path == "/api/locks":
            self._send(200, operations.locked_locks(self.db, actor))
        elif path == "/api/quarantine":
            self._send(200, inbound.list_quarantine(self.db, actor))
        elif path == "/api/exceptions/pending":
            self._send(200, pending_exceptions(self.db, actor))
        elif path == "/api/gaps":
            self._send(200, supply.open_gap_alerts(self.db, actor))
        elif path == "/api/reports":
            auth.reject_commercial(actor, "查看日报")
            result = reports.get_well_report(self.db, qs["well_code"], qs["gas_date"])
            self._send(200 if result else 404, result or {"message": "当日无日报"})
        elif path == "/api/supply/decision":
            self._send(200, supply.get_decision(
                self.db, actor, qs["commitment_code"], qs["gas_date"],
                int(qs["version_no"]) if qs.get("version_no") else None))
        elif path == "/api/supply/trace":
            self._send(200, supply.trace_supply(
                self.db, actor, qs["commitment_code"], qs["gas_date"]))
        else:
            self._send(404, {"error": "not_found", "message": f"未知路径: {path}"})

    def _route_post(self, path: str) -> None:
        body = self._read_json()
        if path == "/api/actors":
            # 引导接口：登记参与方
            result = auth.register_actor(
                self.db, body["id"], body["name"], body["department"])
            self._send(201, {"id": result.id, "name": result.name,
                             "department": result.department})
            return
        actor = self._actor()
        db = self.db

        if path == "/api/blocks":
            self._send(201, catalog.create_block(db, actor, body["code"], body["name"]))
        elif path == "/api/wells":
            self._send(201, catalog.create_well(
                db, actor, body["block_code"], body["code"], body["name"]))
        elif path == "/api/reservoir-versions":
            self._send(201, catalog.submit_reservoir_version(
                db, actor, body["well_code"], body["formation"], body["interpretation"]))
        elif path == "/api/channels":
            self._send(201, catalog.create_channel(
                db, actor, body["code"], body["name"], body["capacity"]))
        elif path == "/api/routes":
            catalog.add_route(db, actor, body["well_code"], body["channel_code"],
                              body.get("share", 1.0))
            self._send(201, {"created": True})
        elif path == "/api/contracts":
            self._send(201, catalog.create_contract(db, actor, body["code"], body["customer"]))
        elif path == "/api/contracts/audience":
            auth.require_departments(actor, {"planning", "dispatch"}, "授权合同可见范围")
            auth.grant_contract_by_code(db, body["contract_code"], body["actor_id"])
            self._send(201, {"granted": True})

        elif path == "/api/receipts":
            self._send(201, inbound.ingest(
                db, actor, body["receipt_no"], body["source_kind"], body["payload"]))
        elif path == "/api/quarantine/resolve":
            self._send(200, inbound.resolve_quarantine(db, actor, body["id"], body["decision"]))

        elif path == "/api/tests":
            self._send(201, testing.register_test(
                db, actor, batch_no=body["batch_no"], well_code=body["well_code"],
                test_date=body["test_date"], flow_rate=body["flow_rate"],
                aof=body.get("aof"), tubing_pressure=body.get("tubing_pressure"),
                formation=body.get("formation"), receipt_no=body.get("receipt_no")))
        elif path == "/api/tests/review":
            self._send(200, testing.review_test(
                db, actor, body["batch_no"], bool(body["approve"])))
        elif path == "/api/capacity/from-test":
            self._send(201, testing.convert_to_capacity(
                db, actor, body["batch_no"], body["valid_from"],
                body.get("confidence", "low"), body.get("confidence_note", "")))
        elif path == "/api/capacity/promote":
            self._send(201, testing.promote_capacity(
                db, actor, body["claim_id"], body["basis"], body["valid_from"],
                body.get("rate"), body.get("confidence", "medium"),
                body.get("confidence_note", "")))
        elif path == "/api/baselines":
            self._send(201, testing.register_baseline(
                db, actor, body["well_code"], body["rate"], body["valid_from"],
                body.get("confidence", "high"), body.get("confidence_note", "历史稳产基线")))

        elif path == "/api/plans":
            self._send(201, operations.create_plan(
                db, actor, body["well_code"], body["kind"], body["plan_no"]))
        elif path == "/api/plans/approve":
            self._send(200, operations.approve_plan(db, actor, body["plan_no"]))
        elif path == "/api/plans/shutdown":
            self._send(201, operations.record_shutdown(
                db, actor, body["plan_no"], body["effective_from"],
                body.get("effective_to"), body.get("note", "")))
        elif path == "/api/plans/revival":
            self._send(201, operations.record_revival(
                db, actor, body["plan_no"], body["effective_from"],
                body["expected_gain"], body.get("confidence", "medium"),
                body.get("note", "")))

        elif path == "/api/locks":
            self._send(201, operations.create_lock(
                db, actor, body["lock_from"], body["lock_to"], body["reason"],
                well_code=body.get("well_code"), channel_code=body.get("channel_code")))
        elif path == "/api/locks/unlock":
            self._send(200, operations.unlock(db, actor, body["lock_id"]))

        elif path == "/api/restrictions":
            self._send(201, supply.add_restriction(
                db, actor, body["channel_code"], body["cap_rate"],
                body["valid_from"], body["valid_to"], body["reason"]))
        elif path == "/api/commitments":
            self._send(201, supply.create_commitment(
                db, actor, body["contract_code"], body["code"], body["daily_volume"],
                body["valid_from"], body["valid_to"]))
        elif path == "/api/commitments/sources":
            supply.add_commitment_source(
                db, actor, body["commitment_code"], body["well_code"], body.get("share", 1.0))
            self._send(201, {"created": True})
        elif path == "/api/nominations":
            supply.add_nomination(
                db, actor, body["commitment_code"], body["gas_date"], body["volume"])
            self._send(201, {"created": True})
        elif path == "/api/supply/evaluate":
            self._send(201, supply.evaluate_commitment(
                db, actor, body["commitment_code"], body["gas_date"],
                body.get("reason", "例行评估")))

        elif path == "/api/reports":
            self._send(201, reports.record_daily(
                db, actor, body["well_code"], body["gas_date"],
                body["actual_rate"], body.get("note", "")))
        elif path == "/api/exceptions":
            self._send(201, submit_exception(
                db, actor, body["well_code"], body["requested_rate"],
                body["valid_from"], body["valid_to"], body["justification"]))
        elif path == "/api/exceptions/review":
            self._send(200, review_exception(
                db, actor, body["exception_id"], bool(body["approve"]),
                body.get("review_note", "")))
        elif path == "/api/gaps/dismiss":
            supply.dismiss_gap(db, actor, body["alert_id"])
            self._send(200, {"closed": True})
        else:
            self._send(404, {"error": "not_found", "message": f"未知路径: {path}"})


def build_server(db_path: str, port: int = 8080) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", port), ApiHandler)
    server.db = connect(db_path, check_same_thread=False)  # type: ignore[attr-defined]
    server.lock = threading.Lock()  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="深层气井产能承诺与复产决策后端")
    parser.add_argument("--db", default="data/deep_gas.db")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    import os
    os.makedirs(os.path.dirname(os.path.abspath(args.db)) or ".", exist_ok=True)
    server = build_server(args.db, args.port)
    print(f"Deep Gas API listening on :{args.port} (db={args.db})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
