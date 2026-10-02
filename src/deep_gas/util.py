"""通用辅助：时间、标识与行序列化。"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any

from sqlite3 import Row

from .errors import ValidationError


def now() -> str:
    """UTC ISO-8601 秒级时间戳，作为所有事件的登记时刻。"""
    return datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def today() -> str:
    return date.today().isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def row_to_dict(row: Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def require_date(value: str, field: str) -> str:
    """校验并归一化 YYYY-MM-DD 供气日。"""
    try:
        date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} 不是有效日期(YYYY-MM-DD)") from exc
    return value
