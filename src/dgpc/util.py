"""通用工具：编号、时间、哈希与 JSON 规范化。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime
from typing import Any

from .errors import ValidationError


def now_iso() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def parse_date(value: Any, field: str = "日期") -> date:
    if not isinstance(value, str):
        raise ValidationError(f"{field}必须是 YYYY-MM-DD 字符串")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field}格式应为 YYYY-MM-DD") from exc


def daterange(start: date, days: int) -> list[date]:
    if days < 0:
        raise ValidationError("天数不能为负")
    from datetime import timedelta

    return [start + timedelta(days=i) for i in range(days)]


def canonical_hash(payload: Any) -> str:
    """对任意 JSON 兼容材料计算稳定哈希（键序无关）。"""
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    """基于 meta 计数器生成形如 PREFIX-0001 的稳定编号。"""
    key = f"seq:{prefix}"
    with conn:
        row = conn.execute("select value from meta where key=?", (key,)).fetchone()
        value = (row[0] + 1) if row else 1
        conn.execute(
            "insert into meta(key,value) values(?,?) "
            "on conflict(key) do update set value=excluded.value",
            (key, value),
        )
    return f"{prefix}-{value:04d}"


def require_fields(body: Any, fields: tuple[str, ...]) -> dict:
    if not isinstance(body, dict):
        raise ValidationError("请求体必须是 JSON 对象")
    missing = [f for f in fields if f not in body]
    if missing:
        raise ValidationError("缺少必填字段：" + "、".join(missing))
    return body


def positive_rate(value: Any, field: str = "产量") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{field}必须是非负数字（万方/日）")
    if value < 0:
        raise ValidationError(f"{field}不能为负")
    return round(float(value), 4)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def json_loads(value: str | None) -> Any:
    return json.loads(value) if value else None
