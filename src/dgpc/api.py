"""JSON HTTP API（仅依赖标准库）。

认证：Authorization: Bearer <令牌>。
授权：路由表声明所需权限；标注 internal=True 的路由拒绝商业用户，
      商业用户只能访问与自己合同有关的供气信息。
"""

from __future__ import annotations

import json
import re
from datetime import date
from http.server import BaseHTTPRequestHandler
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from .errors import AuthError, DomainError, ValidationError
from .util import parse_date

# 路径编号段（如 WEL-0001）
_SEG_RE = re.compile(r"^[A-Z][A-Z0-9]*-\d+$")


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "DGPC/1.0"
    hub = None  # 由 make_server 注入

    actor: dict
    body: dict

    # -- HTTP 框架 -------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _send(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ok(self, value: Any = None) -> None:
        self._send(200, {"data": value if value is not None else {"ok": True}})

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValidationError("请求体不是合法 JSON") from exc
        if not isinstance(value, dict):
            raise ValidationError("请求体必须是 JSON 对象")
        return value

    def _day(self, qs: dict[str, list[str]]) -> date:
        raw = qs.get("date", [None])[0]
        if raw is None:
            return date.today()
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
            raise ValidationError("日期参数格式应为 YYYY-MM-DD")
        return parse_date(raw, "日期")

    def conn_for_bootstrap_check(self):
        return self.hub.conn

    # -- 分发 ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)
        try:
            if path == "/health":
                self._send(200, {"status": "ok", "service": "dgpc"})
                return
            header = self.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                # 首启引导：系统中尚无用户时，允许无令牌创建第一个账号
                if (
                    method == "POST"
                    and tuple(s for s in path.split("/") if s)
                    == ("admin", "users")
                    and self.conn_for_bootstrap_check().execute(
                        "select count(*) c from users"
                    ).fetchone()["c"] == 0
                ):
                    self.actor = {
                        "id": "BOOTSTRAP", "name": "首启引导", "role": "dispatch",
                        "contract_ids": frozenset(),
                    }
                else:
                    raise AuthError("缺少 Bearer 令牌")
            else:
                self.actor = self.hub.auth.authenticate(header[7:].strip())
            self.body = self._read_body() if method == "POST" else {}

            match = self._match(method, path)
            if match is None:
                self._send(404, {"error": "not_found", "message": "接口不存在"})
                return
            perm, internal, fn = match[1], match[2], match[3]
            if perm and self.actor["id"] != "BOOTSTRAP":
                self.hub.auth.require(self.actor, perm)
            if internal:
                self.hub.auth.assert_internal(self.actor)
            fn(qs)
        except DomainError as exc:
            self._send(exc.http_status, exc.to_dict())
        except Exception as exc:  # noqa: BLE001 - 兜底防止连接挂起
            self._send(500, {"error": "internal", "message": str(exc)})

    def _match(self, method: str, path: str):
        segments = tuple(s for s in path.split("/") if s)
        for m, pattern, perm, internal, fn in ROUTES:
            if m != method or len(pattern) != len(segments):
                continue
            kwargs: dict[str, str] = {}
            for pat, seg in zip(pattern, segments):
                if pat.startswith("{"):
                    if not _SEG_RE.match(seg):
                        return None
                    kwargs[pat[1:-1]] = seg
                elif pat != seg:
                    break
            else:
                return pattern, perm, internal, lambda qs, kw=kwargs, f=fn: f(
                    self, qs, **kw
                )
        return None


# =============================================================================
# 处理函数（签名固定：handler, qs, **路径编号参数）
# =============================================================================
H = ApiHandler  # 仅用于类型提示


# -- 身份与目录 -----------------------------------------------------------------
def me(h: H, qs) -> None:
    h._ok({
        "id": h.actor["id"], "name": h.actor["name"], "role": h.actor["role"],
        "contract_ids": sorted(h.actor.get("contract_ids", frozenset())),
    })


def create_user(h: H, qs) -> None:
    b = h.body
    uid, token = h.hub.auth.create_user(b.get("name", ""), b.get("role", ""))
    h._ok({"id": uid, "token": token, "name": b.get("name"), "role": b.get("role")})


def create_contract(h: H, qs) -> None:
    b = h.body
    cid = h.hub.auth.create_contract(
        b.get("name", ""), b.get("user_id", ""), b.get("channel_id", ""))
    h._ok({"id": cid})


def list_contracts(h: H, qs) -> None:
    h._ok(h.hub.auth.list_contracts(h.actor))


def list_blocks(h: H, qs) -> None:
    h._ok(h.hub.catalog.list_blocks())


def get_block(h: H, qs, block_id) -> None:
    h._ok(h.hub.catalog.get_block(block_id))


def create_block(h: H, qs) -> None:
    h._ok(h.hub.catalog.create_block(h.body.get("name", "")))


def create_well(h: H, qs, block_id) -> None:
    h._ok(h.hub.catalog.create_well(block_id, h.body.get("name", "")))


def list_wells(h: H, qs) -> None:
    h._ok(h.hub.catalog.list_wells())


def get_well(h: H, qs, well_id) -> None:
    h._ok(h.hub.catalog.get_well(well_id))


def list_channels(h: H, qs) -> None:
    h._ok(h.hub.catalog.list_channels())


def create_channel(h: H, qs) -> None:
    b = h.body
    h._ok(h.hub.catalog.create_channel(b.get("name", ""), b.get("capacity", 0)))


def attach_route(h: H, qs, well_id) -> None:
    b = h.body
    h.hub.catalog.attach_route(
        well_id, b.get("channel_id", ""), float(b.get("share", 1.0)), h.actor)
    h._ok({"well_id": well_id, "channel_id": b.get("channel_id"),
           "share": float(b.get("share", 1.0))})


# -- 材料 -----------------------------------------------------------------------
def receive_material(h: H, qs) -> None:
    b = h.body
    result = h.hub.materials.receive(
        h.actor, b.get("material_no", ""), b.get("kind", ""),
        b.get("payload", {}), b.get("well_id"))
    receipt = result.pop("receipt")
    h._send(200, {"data": result, "receipt": receipt})


def get_material(h: H, qs, material_id) -> None:
    h._ok(h.hub.materials.get_material(material_id))


def list_quarantined(h: H, qs) -> None:
    h._ok(h.hub.materials.list_quarantined())


def resolve_material(h: H, qs, material_id) -> None:
    b = h.body
    h._ok(h.hub.materials.resolve(
        h.actor, material_id, b.get("action", ""),
        b.get("note", ""), b.get("variant_id")))


# -- 地质：解释与测试 -------------------------------------------------------------
def submit_interpretation(h: H, qs, well_id) -> None:
    b = h.body
    h._ok(h.hub.geology.submit_interpretation(
        h.actor, well_id, b.get("formation", ""), b.get("payload", {})))


def list_interpretations(h: H, qs, well_id) -> None:
    h._ok(h.hub.geology.list_interpretations(well_id))


def submit_test(h: H, qs, well_id) -> None:
    b = h.body
    h._ok(h.hub.geology.submit_test_batch(
        h.actor, well_id, b.get("flow_rate", 0), b.get("test_date", ""),
        b.get("basis", ""), b.get("confidence", ""),
        b.get("interpretation_id"), b.get("material_id")))


def list_tests(h: H, qs, well_id=None) -> None:
    status = qs.get("status", [None])[0]
    rows = h.hub.geology.list_test_batches(status)
    if well_id:
        rows = [r for r in rows if r["well_id"] == well_id]
    h._ok(rows)


def get_test(h: H, qs, test_id) -> None:
    h._ok(h.hub.geology.get_test_batch(test_id))


def review_test(h: H, qs, test_id) -> None:
    b = h.body
    h._ok(h.hub.geology.review_test_batch(
        h.actor, test_id, bool(b.get("approve", False)), b.get("note", "")))


# -- 产能 ------------------------------------------------------------------------
def publish_offer(h: H, qs, test_id) -> None:
    b = h.body
    h._ok(h.hub.capacity.publish_from_test(
        h.actor, test_id, b.get("valid_from", ""), b.get("valid_to", ""),
        b.get("rate")))


def list_offers(h: H, qs) -> None:
    h._ok(h.hub.capacity.list_offers(qs.get("status", [None])[0]))


def get_offer(h: H, qs, offer_id) -> None:
    h._ok(h.hub.capacity.get_offer(offer_id))


def promote_offer(h: H, qs, offer_id) -> None:
    b = h.body
    h._ok(h.hub.capacity.promote(
        h.actor, offer_id, b.get("valid_from", ""), b.get("valid_to", ""),
        b.get("rate", 0), b.get("confidence", ""), b.get("basis", "")))


def withdraw_offer(h: H, qs, offer_id) -> None:
    h._ok(h.hub.capacity.withdraw(h.actor, offer_id, h.body.get("reason", "")))


def submit_exception(h: H, qs, well_id) -> None:
    b = h.body
    h._ok(h.hub.capacity.submit_exception(
        h.actor, well_id, b.get("rate", 0), b.get("valid_from", ""),
        b.get("valid_to", ""), b.get("confidence", ""), b.get("reason", "")))


def list_exceptions(h: H, qs, well_id=None) -> None:
    rows = h.hub.capacity.list_exceptions(qs.get("status", [None])[0])
    if well_id:
        rows = [r for r in rows if r["well_id"] == well_id]
    h._ok(rows)


def get_exception(h: H, qs, exception_id) -> None:
    h._ok(h.hub.capacity.get_exception(exception_id))


def review_exception(h: H, qs, exception_id) -> None:
    b = h.body
    h._ok(h.hub.capacity.review_exception(
        h.actor, exception_id, bool(b.get("approve", False)), b.get("note", "")))


# -- 作业 / 检修 / 限输 ------------------------------------------------------------
def create_program(h: H, qs, well_id) -> None:
    b = h.body
    h._ok(h.hub.operations.create_program(
        h.actor, well_id, b.get("kind", ""), b.get("planned_start", ""),
        b.get("planned_end", ""), b.get("note", "")))


def list_programs(h: H, qs, well_id=None) -> None:
    h._ok(h.hub.operations.list_programs(well_id))


def get_program(h: H, qs, program_id) -> None:
    h._ok(h.hub.operations.get_program(program_id))


def reschedule_program(h: H, qs, program_id) -> None:
    b = h.body
    h._ok(h.hub.operations.reschedule_program(
        h.actor, program_id, b.get("new_start", ""), b.get("new_end", ""),
        b.get("reason", "")))


def start_program(h: H, qs, program_id) -> None:
    h._ok(h.hub.operations.start_program(h.actor, program_id))


def complete_program(h: H, qs, program_id) -> None:
    h._ok(h.hub.operations.complete_program(h.actor, program_id))


def cancel_program(h: H, qs, program_id) -> None:
    h._ok(h.hub.operations.cancel_program(
        h.actor, program_id, h.body.get("reason", "")))


def create_window(h: H, qs, well_id) -> None:
    b = h.body
    h._ok(h.hub.operations.create_window(
        h.actor, well_id, b.get("lock_from", ""), b.get("unlock_due", ""),
        b.get("note", "")))


def list_windows(h: H, qs) -> None:
    h._ok(h.hub.operations.list_windows(qs.get("status", [None])[0]))


def get_window(h: H, qs, window_id) -> None:
    h._ok(h.hub.operations.get_window(window_id))


def reschedule_window(h: H, qs, window_id) -> None:
    b = h.body
    h._ok(h.hub.operations.reschedule_unlock(
        h.actor, window_id, b.get("new_due", ""), b.get("reason", "")))


def unlock_window(h: H, qs, window_id) -> None:
    h._ok(h.hub.operations.unlock(
        h.actor, window_id, h.body.get("note", "")))


def create_curtailment(h: H, qs, channel_id) -> None:
    b = h.body
    h._ok(h.hub.operations.create_curtailment(
        h.actor, channel_id, b.get("limit_rate", 0), b.get("valid_from", ""),
        b.get("valid_to", ""), b.get("note", "")))


def list_curtailments(h: H, qs) -> None:
    h._ok(h.hub.operations.list_curtailments())


def get_curtailment(h: H, qs, curtailment_id) -> None:
    h._ok(h.hub.operations.get_curtailment(curtailment_id))


def reschedule_curtailment(h: H, qs, curtailment_id) -> None:
    b = h.body
    h._ok(h.hub.operations.reschedule_curtailment(
        h.actor, curtailment_id, b.get("new_from", ""), b.get("new_to", ""),
        b.get("reason", "")))


def lift_curtailment(h: H, qs, curtailment_id) -> None:
    h._ok(h.hub.operations.lift_curtailment(h.actor, curtailment_id))


# -- 日报 -------------------------------------------------------------------------
def submit_report(h: H, qs, well_id) -> None:
    b = h.body
    if b.get("correct"):
        h.hub.auth.require(h.actor, "report:correct")
        result = h.hub.reports.correct_report(
            h.actor, well_id, b.get("gas_date", ""), b.get("rate", 0),
            b.get("note", ""), b.get("material_id"))
    else:
        result = h.hub.reports.submit_report(
            h.actor, well_id, b.get("gas_date", ""), b.get("rate", 0),
            b.get("material_id"))
    h._ok(result)


def get_report(h: H, qs, report_id) -> None:
    h._ok(h.hub.reports.get_report(report_id))


def latest_report(h: H, qs, well_id) -> None:
    h._ok(h.hub.reports.latest_report(well_id, h._day(qs).isoformat()))


# -- 承诺 / 决定 / 缺口 --------------------------------------------------------------
def create_commitment(h: H, qs) -> None:
    b = h.body
    h._ok(h.hub.commitments.create_commitment(
        h.actor, b.get("contract_id", ""), b.get("gas_date", ""),
        b.get("volume", 0), b.get("note", "")))


def list_commitments(h: H, qs) -> None:
    h._ok(h.hub.commitments.list_commitments(
        h.actor, contract_id=qs.get("contract", [None])[0]))


def get_commitment(h: H, qs, commitment_id) -> None:
    # 服务层按合同归属对商业用户做可见性校验
    h._ok(h.hub.commitments.get_commitment(h.actor, commitment_id))


def cancel_commitment(h: H, qs, commitment_id) -> None:
    h._ok(h.hub.commitments.cancel_commitment(
        h.actor, commitment_id, h.body.get("note", "")))


def correct_decision(h: H, qs, commitment_id) -> None:
    h._ok(h.hub.commitments.correct_decision(
        h.actor, commitment_id, h.body.get("note", "")))


def list_gaps(h: H, qs) -> None:
    h._ok(h.hub.commitments.list_open_gaps())


def close_gap(h: H, qs, gap_id) -> None:
    h._ok(h.hub.commitments.close_gap(h.actor, gap_id, h.body.get("note", "")))


# -- 测算 / 追溯 / 值班 ---------------------------------------------------------------
def reliable_supply(h: H, qs) -> None:
    h._ok(h.hub.projection.reliable_supply(h._day(qs)))


def duty_queue(h: H, qs) -> None:
    h._ok(h.hub.duty_queue(h._day(qs)))


def recover(h: H, qs) -> None:
    h._ok(h.hub.recover(h._day(qs)))


def trace_day(h: H, qs) -> None:
    h._ok(h.hub.trace.trace_gas_day(h.actor, h._day(qs).isoformat()))


def trace_commitment(h: H, qs, commitment_id) -> None:
    h._ok(h.hub.trace.trace_commitment(h.actor, commitment_id))


def trace_well(h: H, qs, well_id) -> None:
    h._ok(h.hub.trace.trace_well_day(well_id, h._day(qs).isoformat()))


# (方法, 路径模式, 权限动作, 仅内部可见, 处理函数)
Route = tuple[str, tuple, str | None, bool, Callable]
ROUTES: list[Route] = [
    ("GET",  ("actors", "me"), None, False, me),
    ("POST", ("admin", "users"), "catalog:manage", True, create_user),
    ("POST", ("admin", "contracts"), "catalog:manage", True, create_contract),
    ("GET",  ("contracts",), None, False, list_contracts),

    ("GET",  ("blocks",), None, True, list_blocks),
    ("POST", ("blocks",), "catalog:manage", True, create_block),
    ("GET",  ("blocks", "{block_id}"), None, True, get_block),
    ("POST", ("blocks", "{block_id}", "wells"), "catalog:manage", True, create_well),
    ("GET",  ("wells",), None, True, list_wells),
    ("GET",  ("wells", "{well_id}"), None, True, get_well),
    ("POST", ("wells", "{well_id}", "routes"), "channel:manage", True, attach_route),
    ("GET",  ("channels",), None, True, list_channels),
    ("POST", ("channels",), "channel:manage", True, create_channel),

    ("POST", ("materials",), "materials:receive", True, receive_material),
    ("GET",  ("materials",), "queue:view", True, list_quarantined),
    ("GET",  ("materials", "{material_id}"), None, True, get_material),
    ("POST", ("materials", "{material_id}", "resolve"), "materials:resolve",
     True, resolve_material),

    ("POST", ("wells", "{well_id}", "interpretations"),
     "interpretation:submit", True, submit_interpretation),
    ("GET",  ("wells", "{well_id}", "interpretations"), None, True,
     list_interpretations),
    ("POST", ("wells", "{well_id}", "tests"), "test:submit", True, submit_test),
    ("GET",  ("wells", "{well_id}", "tests"), None, True, list_tests),
    ("GET",  ("tests",), None, True, lambda h, qs: list_tests(h, qs)),
    ("GET",  ("tests", "{test_id}"), None, True, get_test),
    ("POST", ("tests", "{test_id}", "review"), "test:review", True, review_test),
    ("POST", ("tests", "{test_id}", "offer"), "offer:publish", True, publish_offer),

    ("GET",  ("offers",), None, True, list_offers),
    ("GET",  ("offers", "{offer_id}"), None, True, get_offer),
    ("POST", ("offers", "{offer_id}", "promote"), "offer:publish", True,
     promote_offer),
    ("POST", ("offers", "{offer_id}", "withdraw"), "offer:publish", True,
     withdraw_offer),
    ("POST", ("wells", "{well_id}", "exceptions"), "exception:submit", True,
     submit_exception),
    ("GET",  ("wells", "{well_id}", "exceptions"), None, True, list_exceptions),
    ("GET",  ("exceptions",), None, True, lambda h, qs: list_exceptions(h, qs)),
    ("GET",  ("exceptions", "{exception_id}"), None, True, get_exception),
    ("POST", ("exceptions", "{exception_id}", "review"), "exception:review",
     True, review_exception),

    ("POST", ("wells", "{well_id}", "programs"), "program:manage", True,
     create_program),
    ("GET",  ("wells", "{well_id}", "programs"), None, True,
     lambda h, qs, well_id: list_programs(h, qs, well_id)),
    ("GET",  ("programs",), None, True, lambda h, qs: list_programs(h, qs)),
    ("GET",  ("programs", "{program_id}"), None, True, get_program),
    ("POST", ("programs", "{program_id}", "reschedule"), "program:manage",
     True, reschedule_program),
    ("POST", ("programs", "{program_id}", "start"), "program:manage", True,
     start_program),
    ("POST", ("programs", "{program_id}", "complete"), "program:manage", True,
     complete_program),
    ("POST", ("programs", "{program_id}", "cancel"), "program:manage", True,
     cancel_program),

    ("POST", ("wells", "{well_id}", "windows"), "window:manage", True,
     create_window),
    ("GET",  ("windows",), None, True, list_windows),
    ("GET",  ("windows", "{window_id}"), None, True, get_window),
    ("POST", ("windows", "{window_id}", "reschedule"), "window:manage", True,
     reschedule_window),
    ("POST", ("windows", "{window_id}", "unlock"), "window:unlock", True,
     unlock_window),

    ("POST", ("channels", "{channel_id}", "curtailments"), "curtailment:manage",
     True, create_curtailment),
    ("GET",  ("curtailments",), None, True, list_curtailments),
    ("GET",  ("curtailments", "{curtailment_id}"), None, True, get_curtailment),
    ("POST", ("curtailments", "{curtailment_id}", "reschedule"),
     "curtailment:manage", True, reschedule_curtailment),
    ("POST", ("curtailments", "{curtailment_id}", "lift"), "curtailment:manage",
     True, lift_curtailment),

    ("POST", ("wells", "{well_id}", "reports"), "report:submit", True,
     submit_report),
    ("GET",  ("reports", "{report_id}"), None, True, get_report),
    ("GET",  ("wells", "{well_id}", "reports", "latest"), None, True,
     latest_report),

    ("POST", ("commitments",), "commitment:manage", False, create_commitment),
    ("GET",  ("commitments",), None, False, list_commitments),
    ("GET",  ("commitments", "{commitment_id}"), None, False, get_commitment),
    ("POST", ("commitments", "{commitment_id}", "cancel"), "commitment:manage",
     False, cancel_commitment),
    ("POST", ("commitments", "{commitment_id}", "decisions", "correct"),
     "decision:make", False, correct_decision),

    ("GET",  ("gaps",), "queue:view", True, list_gaps),
    ("POST", ("gaps", "{gap_id}", "close"), "commitment:manage", True, close_gap),

    ("GET",  ("supply", "reliable"), None, True, reliable_supply),
    ("GET",  ("duty", "queue"), "queue:view", True, duty_queue),
    ("POST", ("duty", "recover"), "queue:view", True, recover),
    ("GET",  ("trace",), None, False, trace_day),
    ("GET",  ("commitments", "{commitment_id}", "trace"), None, False,
     trace_commitment),
    ("GET",  ("wells", "{well_id}", "trace"), None, True, trace_well),
]
