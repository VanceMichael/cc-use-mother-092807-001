"""合成引导数据：角色账号、合同、区块/井、通道与若干在途业务。

所有日期相对运行当日生成，确保重启后值班队列里能看到
待复核测试、待解锁检修与承诺缺口；不含任何真实个人信息。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from .workflow import ServiceHub


def seed(hub: ServiceHub) -> dict:
    conn = hub.conn
    already = conn.execute("select 1 from users limit 1").fetchone()
    if already:
        return {"seeded": False, "reason": "数据库已有数据，跳过引导"}

    auth = hub.auth
    tokens: dict[str, str] = {}
    users: dict[str, str] = {}

    def user(key: str, name: str, role: str) -> str:
        uid, token = auth.create_user(name, role)
        users[key] = uid
        tokens[key] = token
        return uid

    user("rig_lead", "井队值班长（示例）", "rig")
    user("geo_lead", "地质责任师（示例）", "geology")
    user("geo_other", "地质复核员甲（示例）", "geology")
    user("maint_lead", "设备维护班长（示例）", "maintenance")
    user("pipe_op", "输气运行值班（示例）", "pipeline")
    user("dispatcher", "生产调度值班（示例）", "dispatch")
    user("buyer_a", "城市燃气甲客户（示例）", "commercial")
    user("buyer_b", "工业乙客户（示例）", "commercial")

    cat = hub.catalog
    block = cat.create_block("川西北深层区块（示例）")
    block2 = cat.create_block("川东超深层区块（示例）")

    w1 = cat.create_well(block["id"], "老井-西1（大修复产示例）")
    w2 = cat.create_well(block["id"], "新井-西2（飞仙关组测试示例）")
    w3 = cat.create_well(block2["id"], "新井-东3（栖霞组试采示例）")

    chn1 = cat.create_channel("西干线（示例）", 200.0)
    chn2 = cat.create_channel("东干线（示例）", 150.0)
    for w in (w1, w2):
        cat.attach_route(w["id"], chn1["id"], 1.0)
    cat.attach_route(w3["id"], chn2["id"], 1.0)

    contract_a = auth.create_contract(
        "城市燃气甲供气合同（示例）", users["buyer_a"], chn1["id"])
    contract_b = auth.create_contract(
        "工业乙供气合同（示例）", users["buyer_b"], chn2["id"])

    today = date.today()

    # 西1：已有稳产口径（老井）
    geo = hub.geology
    geo_actor = _actor(conn, users["geo_lead"])
    disp = _actor(conn, users["dispatcher"])
    rig = _actor(conn, users["rig_lead"])
    pipe = _actor(conn, users["pipe_op"])
    maint = _actor(conn, users["maint_lead"])

    i1 = geo.submit_interpretation(
        geo_actor, w1["id"], "须家河组",
        {"porosity": 0.06, "pressure_mpa": 72.0, "note": "老井复查解释v1"})
    t1 = geo.submit_test_batch(
        rig, w1["id"], 40.0, (today - timedelta(days=30)).isoformat(),
        "大修后复产一点法稳定试井，制度稳定8小时", "high", i1["id"])
    geo.review_test_batch(disp, t1["id"], True, "曲线稳定，同意登记")
    o1 = hub.capacity.publish_from_test(
        disp, t1["id"],
        (today - timedelta(days=20)).isoformat(),
        (today + timedelta(days=180)).isoformat())
    hub.capacity.promote(
        disp, o1["id"],
        (today - timedelta(days=10)).isoformat(),
        (today + timedelta(days=180)).isoformat(),
        38.0, "high", "连续10日稳产36~39万方，转稳产口径")

    # 西2：新层系测试，复核通过并已登记 tested 产能
    i2 = geo.submit_interpretation(
        geo_actor, w2["id"], "飞仙关组",
        {"porosity": 0.09, "pressure_mpa": 88.0, "note": "新层系解释v1"})
    t2 = geo.submit_test_batch(
        geo_actor, w2["id"], 105.0,
        (today - timedelta(days=2)).isoformat(),
        "百万方级放喷测试，油压45MPa，持续6小时", "medium", i2["id"])
    geo.review_test_batch(disp, t2["id"], True, "测试产能成立，先按试采口径观察")
    o2 = hub.capacity.publish_from_test(
        disp, t2["id"], today.isoformat(),
        (today + timedelta(days=90)).isoformat())

    # 东3：待复核测试（重启后仍在队列中）
    i3 = geo.submit_interpretation(
        _actor(conn, users["geo_other"]), w3["id"], "栖霞组",
        {"porosity": 0.075, "pressure_mpa": 95.0})
    geo.submit_test_batch(
        _actor(conn, users["geo_other"]), w3["id"], 80.0,
        (today - timedelta(days=1)).isoformat(),
        "新井完井测试，制度待核", "medium", i3["id"])

    # 东3 检修窗口：应解锁日为昨天 -> 重启后进入待解锁
    hub.operations.create_window(
        maint, w3["id"],
        (today - timedelta(days=5)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        "压缩机年度检修（示例）")

    # 西干线未来限输
    hub.operations.create_curtailment(
        pipe, chn1["id"], 100.0,
        (today + timedelta(days=3)).isoformat(),
        (today + timedelta(days=5)).isoformat(),
        "西干线连头作业限输（示例）")

    # 供气承诺：甲客户今天 120 万方（限输日前可满足），5 天后 160 万方（将产生缺口）
    hub.commitments.create_commitment(
        disp, contract_a, today.isoformat(), 120.0, "城市燃气日用气（示例）")
    hub.commitments.create_commitment(
        disp, contract_a,
        (today + timedelta(days=4)).isoformat(), 160.0,
        "限输窗内高峰用气（示例，将提示缺口）")
    hub.commitments.create_commitment(
        disp, contract_b,
        (today + timedelta(days=2)).isoformat(), 30.0,
        "工业客户用气（东干线检修中，将提示缺口）")

    return {
        "seeded": True,
        "users": users,
        "tokens": tokens,
        "contracts": {"a": contract_a, "b": contract_b},
        "blocks": [block["id"], block2["id"]],
        "wells": [w1["id"], w2["id"], w3["id"]],
        "channels": [chn1["id"], chn2["id"]],
    }


def _actor(conn, user_id: str) -> dict:
    row = conn.execute("select * from users where id=?", (user_id,)).fetchone()
    actor = dict(row)
    actor["contract_ids"] = frozenset(
        r["id"]
        for r in conn.execute("select id from contracts where user_id=?", (user_id,))
    )
    return actor


def write_token_file(path: str | Path, result: dict) -> None:
    Path(path).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
