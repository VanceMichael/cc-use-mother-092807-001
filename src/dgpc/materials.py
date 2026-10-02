"""外来材料（测井/生产回执）接收：幂等去重与同号异值隔离。

规则：
- 同一材料编号、同一内容哈希重复到达 -> 记为 duplicate，绝不再次累计；
- 同一编号、不同内容 -> 首件与后续变体全部保留，状态置为 quarantined，
  在调度处置（放行某个变体或整组丢弃）之前不得被任何下游业务引用；
- 已处置编号若又出现不同内容，拒绝复用编号（保护既有决定的依据）。
"""

from __future__ import annotations

import json
import sqlite3

from .errors import ConflictError, NotFoundError, QuarantineError, ValidationError
from .util import canonical_hash, next_id, now_iso

MATERIAL_KINDS = frozenset({"well_log", "production_receipt"})
USABLE_STATUSES = frozenset({"accepted", "released"})


class MaterialService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def receive(
        self,
        actor: dict,
        material_no: str,
        kind: str,
        payload: dict,
        well_id: str | None = None,
    ) -> dict:
        if not isinstance(material_no, str) or not material_no.strip():
            raise ValidationError("材料编号不能为空")
        if kind not in MATERIAL_KINDS:
            raise ValidationError("材料类型必须是 well_log 或 production_receipt")
        if not isinstance(payload, dict) or not payload:
            raise ValidationError("材料内容必须是非空 JSON 对象")
        if well_id is not None and self.conn.execute(
            "select 1 from wells where id=?", (well_id,)
        ).fetchone() is None:
            raise NotFoundError("关联井不存在")

        digest = canonical_hash(payload)
        with self.conn:
            existing = self.conn.execute(
                "select * from inbound_materials where material_no=?",
                (material_no.strip(),),
            ).fetchone()

            if existing is None:
                mid = next_id(self.conn, "MAT")
                self.conn.execute(
                    "insert into inbound_materials(id,material_no,kind,well_id,"
                    "payload_hash,payload,received_at,status) "
                    "values(?,?,?,?,?,?,?,?)",
                    (
                        mid,
                        material_no.strip(),
                        kind,
                        well_id,
                        digest,
                        canonical_json(payload),
                        now_iso(),
                        "accepted",
                    ),
                )
                result = self.get_material(mid)
                result["receipt"] = "new"
                return result

            # 编号已存在
            if existing["payload_hash"] == digest:
                # 完全相同的重复件：不累计
                result = self.get_material(existing["id"])
                result["receipt"] = "duplicate"
                return result

            if existing["status"] in ("released", "discarded"):
                raise ConflictError(
                    "材料编号已按既有内容处置，数值不同的新材料不得复用该编号",
                    details={
                        "material_no": material_no,
                        "existing_status": existing["status"],
                    },
                )

            # 同号不同值 -> 隔离，并把本次变体留痕
            known = self.conn.execute(
                "select 1 from inbound_material_variants "
                "where material_id=? and payload_hash=?",
                (existing["id"], digest),
            ).fetchone()
            if known is not None:
                # 与已留痕变体完全相同的重复件：不累计
                result = self.get_material(existing["id"])
                result["receipt"] = "duplicate"
                return result
            seq_row = self.conn.execute(
                "select coalesce(max(seq),0)+1 next_seq from inbound_material_variants "
                "where material_id=?",
                (existing["id"],),
            ).fetchone()
            variant_id = next_id(self.conn, "VAR")
            self.conn.execute(
                "insert into inbound_material_variants(id,material_id,seq,"
                "payload_hash,payload,received_by,received_at) "
                "values(?,?,?,?,?,?,?)",
                (
                    variant_id,
                    existing["id"],
                    seq_row["next_seq"],
                    digest,
                    canonical_json(payload),
                    actor["id"],
                    now_iso(),
                ),
            )
            self.conn.execute(
                "update inbound_materials set status='quarantined' where id=?",
                (existing["id"],),
            )
        result = self.get_material(existing["id"])
        result["receipt"] = "quarantined"
        return result

    # -- 隔离处置 ------------------------------------------------------------
    def resolve(
        self,
        actor: dict,
        material_id: str,
        action: str,
        note: str = "",
        variant_id: str | None = None,
    ) -> dict:
        material = self.conn.execute(
            "select * from inbound_materials where id=?", (material_id,)
        ).fetchone()
        if material is None:
            raise NotFoundError("材料不存在")
        if material["status"] != "quarantined":
            raise ConflictError("只有隔离中的材料需要处置")
        if action not in ("accept", "discard"):
            raise ValidationError("处置动作必须是 accept 或 discard")

        chosen: str | None = None
        if action == "accept":
            if variant_id is None:
                chosen = material["id"]  # 放行首件
            else:
                if variant_id == material["id"]:
                    chosen = material["id"]
                else:
                    vr = self.conn.execute(
                        "select id from inbound_material_variants where id=? "
                        "and material_id=?",
                        (variant_id, material_id),
                    ).fetchone()
                    if vr is None:
                        raise NotFoundError("被放行的变体不存在于该隔离组")
                    chosen = variant_id
        with self.conn:
            self.conn.execute(
                "update inbound_materials set status=?, resolution_by=?, "
                "resolution_at=?, resolution_note=?, chosen_variant_id=? where id=?",
                (
                    "released" if action == "accept" else "discarded",
                    actor["id"],
                    now_iso(),
                    note or None,
                    chosen,
                    material_id,
                ),
            )
        return self.get_material(material_id)

    # -- 查询 ----------------------------------------------------------------
    def get_material(self, material_id: str) -> dict:
        row = self.conn.execute(
            "select * from inbound_materials where id=?", (material_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        result = dict(row)
        result.pop("payload", None)
        variants = [
            dict(r)
            for r in self.conn.execute(
                "select id,seq,payload_hash,received_by,received_at "
                "from inbound_material_variants where material_id=? order by seq",
                (material_id,),
            )
        ]
        result["variants"] = variants
        return result

    def effective_payload(self, material_id: str) -> dict:
        """返回放行后实际生效的材料内容；不可用材料直接拒绝。"""
        row = self.conn.execute(
            "select * from inbound_materials where id=?", (material_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        if row["status"] not in USABLE_STATUSES:
            raise QuarantineError(
                "材料尚不可用（未接收/隔离中/已丢弃），不能作为业务依据",
                details={"material_id": material_id, "status": row["status"]},
            )
        chosen = row["chosen_variant_id"]
        if chosen and chosen != row["id"]:
            vr = self.conn.execute(
                "select payload from inbound_material_variants where id=?",
                (chosen,),
            ).fetchone()
            return json.loads(vr["payload"])
        return json.loads(row["payload"])

    def assert_usable(self, material_id: str) -> None:
        row = self.conn.execute(
            "select status from inbound_materials where id=?", (material_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("材料不存在")
        if row["status"] not in USABLE_STATUSES:
            raise QuarantineError(
                "材料尚不可用，不能作为业务依据",
                details={"material_id": material_id, "status": row["status"]},
            )

    def list_quarantined(self) -> list[dict]:
        return [
            self.get_material(r["id"])
            for r in self.conn.execute(
                "select id from inbound_materials where status='quarantined' order by id"
            )
        ]


def canonical_json(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)
