#!/usr/bin/env python3
"""端到端演示：老井复产、新井测试晋级、回执幂等/隔离、限输与停井、
日报更正、产能例外职责分离、商业用户隔离，以及重启后的工作台恢复。

运行：python3 scripts/demo.py
仅使用仓库内 data/demo.db，不连接任何外部系统。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.deep_gas import auth, catalog, inbound, operations, reports, supply, testing
from src.deep_gas.capacity_exceptions import review_exception, submit_exception
from src.deep_gas.db import connect
from src.deep_gas.errors import AuthorizationError, Conflict, Quarantined
from src.deep_gas.recovery import workbench

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "demo.db"


def step(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def show(label: str, value) -> None:
    print(f"[{label}] {value}")


def main() -> None:
    DB_PATH.parent.mkdir(exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()
    conn = connect(DB_PATH)

    step("1. 岗位登记（井队/地质/维护/输气/调度/计划/商业用户）")
    auth.register_actor(conn, "dispatcher", "值班调度周岩", "dispatch")
    auth.register_actor(conn, "geologist", "地质工程师林巍", "geology")
    auth.register_actor(conn, "driller", "井队长郑海", "drilling")
    auth.register_actor(conn, "maintainer", "维修技师高越", "maintenance")
    auth.register_actor(conn, "pipeline_op", "输气运行何川", "pipeline")
    auth.register_actor(conn, "planner", "供气计划许晴", "planning")
    auth.register_actor(conn, "buyer_a", "川北化工采购员", "commercial")
    auth.register_actor(conn, "buyer_b", "渝西电厂采购员", "commercial")
    actors = {aid: auth.get_actor(conn, aid) for aid in (
        "dispatcher", "geologist", "driller", "maintainer",
        "pipeline_op", "planner", "buyer_a", "buyer_b")}

    step("2. 区块、井、储层解释版本、外输通道")
    catalog.create_block(conn, actors["dispatcher"], "BLK-D1", "深层一号区块")
    catalog.create_well(conn, actors["dispatcher"], "BLK-D1", "X201", "新井西201")
    catalog.create_well(conn, actors["dispatcher"], "BLK-D1", "X302", "新井西302")
    catalog.create_well(conn, actors["dispatcher"], "BLK-D1", "L001", "老井罗001")
    v1 = catalog.submit_reservoir_version(conn, actors["geologist"], "X201", "栖霞组",
                                          "初步解释：孔隙型储层，测试产能待复核")
    v2 = catalog.submit_reservoir_version(conn, actors["geologist"], "X201", "栖霞组",
                                          "复试解释：缝洞连通，稳产口径可上调")
    show("储层解释版本", f"v{v1['version_no']} -> v{v2['version_no']}（只能递增）")

    catalog.create_channel(conn, actors["pipeline_op"], "CN-01", "北线外输干线", 300.0)
    catalog.add_route(conn, actors["pipeline_op"], "X201", "CN-01")
    catalog.add_route(conn, actors["pipeline_op"], "X302", "CN-01")
    catalog.add_route(conn, actors["pipeline_op"], "L001", "CN-01")

    catalog.create_contract(conn, actors["planner"], "CT-A", "川北化工")
    auth.grant_contract_by_code(conn, "CT-A", "buyer_a")  # 仅 buyer_a 被授权
    supply.create_commitment(conn, actors["planner"], "CT-A", "CM-A-10", 120.0,
                             "2026-10-01", "2026-10-31")
    supply.add_commitment_source(conn, actors["planner"], "CM-A-10", "X201")
    supply.add_commitment_source(conn, actors["planner"], "CM-A-10", "L001")
    supply.add_nomination(conn, actors["planner"], "CM-A-10", "2026-10-05", 120.0)

    step("3. 老井基线 + 新井测试批次（待复核 → 产能谱系 → 试采 → 稳产）")
    testing.register_baseline(conn, actors["dispatcher"], "L001", 20.0, "2026-09-01")
    testing.register_test(conn, actors["driller"], "T-2026-001", "X201",
                          "2026-09-20", 105.0, aof=180.0, formation="栖霞组")
    testing.review_test(conn, actors["dispatcher"], "T-2026-001", True)
    cap = testing.convert_to_capacity(conn, actors["dispatcher"], "T-2026-001",
                                      "2026-09-26", "low", "百万方级测试，短期试采确认")
    claim_id = cap["claim_id"]
    show("测试产能谱系", f"{claim_id} v1 testing 105（低置信）")
    testing.promote_capacity(conn, actors["dispatcher"], claim_id, "trial",
                             "2026-10-03", rate=102.0, confidence="medium",
                             confidence_note="试采一周压力稳定")
    testing.promote_capacity(conn, actors["dispatcher"], claim_id, "stable",
                             "2026-10-08", rate=100.0, confidence="high",
                             confidence_note="稳产十日复核+栖霞组解释v2")
    show("口径晋级", "testing -> trial -> stable 是同一谱系版本，不重复累计")

    step("4. 同一测试不能二次计入；口径不可跨级")
    try:
        testing.convert_to_capacity(conn, actors["dispatcher"], "T-2026-001", "2026-10-08")
    except Conflict as exc:
        show("重复计入被拒绝", exc)
    try:
        testing.promote_capacity(conn, actors["dispatcher"], claim_id, "stable", "2026-10-20")
    except Conflict as exc:
        show("跨级晋级被拒绝", exc)

    step("5. 来件回执：重复件不累计；同号异值先隔离")
    r1 = inbound.ingest(conn, actors["driller"], "RCP-7788", "well_log",
                        {"well": "X302", "flow_rate": 66.5})
    r2 = inbound.ingest(conn, actors["driller"], "RCP-7788", "well_log",
                        {"well": "X302", "flow_rate": 66.5})
    show("首次/重复", f"{r1['outcome']} / {r2['outcome']}")
    try:
        inbound.ingest(conn, actors["driller"], "RCP-7788", "well_log",
                       {"well": "X302", "flow_rate": 71.0})
    except Quarantined as exc:
        show("同号异值被隔离", exc)

    step("6. 供气评估：不同供气日命中不同口径版本，只计一次")
    d1001 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-01")
    show("10-01（测试口径低置信）",
         f"firm={d1001['firm_available']} conditional={d1001['conditional_available']} "
         f"-> {d1001['status']}，需求 {d1001['demand_volume']}")
    d1005 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-05")
    show("10-05（试采口径，按提名120）",
         f"firm={d1005['firm_available']} conditional={d1005['conditional_available']} "
         f"-> {d1005['status']}")
    d1009 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-09")
    show("10-09（稳产口径高置信）",
         f"firm={d1009['firm_available']} -> {d1009['status']}")

    step("7. 老井酸化解堵：停井只影响未来安排，历史决定保留")
    operations.create_plan(conn, actors["maintainer"], "L001", "acidizing", "WP-55")
    operations.approve_plan(conn, actors["dispatcher"], "WP-55")
    operations.record_shutdown(conn, actors["maintainer"], "WP-55",
                               "2026-10-10", "2026-10-12", "酸化解堵停井")
    d1011 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-11",
                                       reason="酸化解堵窗口")
    show("10-11（L001停井）",
         f"可供={d1011['total_available']} 缺口={d1011['gap']} -> {d1011['status']}")
    operations.record_revival(conn, actors["maintainer"], "WP-55", "2026-10-13",
                              expected_gain=8.0, confidence="high",
                              note="解堵后复产，套压恢复")
    d1013 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-13")
    show("10-13（复产增量叠加）",
         f"firm={d1013['firm_available']} -> {d1013['status']}，旧版 10-11 决定未被改写")

    step("8. 管网限输：只压减窗口内尚未履行的安排")
    supply.add_restriction(conn, actors["pipeline_op"], "CN-01", 110.0,
                           "2026-10-15", "2026-10-15", "下游检修，北线限输")
    d1015 = supply.evaluate_commitment(conn, actors["dispatcher"], "CM-A-10", "2026-10-15",
                                       reason="北线限输110")
    show("10-15", f"可供={d1015['total_available']} 缺口={d1015['gap']} -> {d1015['status']}；"
                  f"通道证据={d1015['channels'][0]['effective_capacity']}")

    step("9. 日报只追加：v1 原始值 + v2 更正版本衔接")
    reports.record_daily(conn, actors["driller"], "X201", "2026-10-05", 118.0, "初报")
    rpt = reports.record_daily(conn, actors["driller"], "X201", "2026-10-05", 121.0, "计量更正")
    show("日报版本", f"v{rpt['versions'][0]['version_no']}={rpt['versions'][0]['actual_rate']}"
                    f" -> v{rpt['versions'][1]['version_no']}={rpt['versions'][1]['actual_rate']}"
                    f"（correction_of 指向前驱，历史值保留）")

    step("10. 产能例外：地质不能批准自己提交的申请")
    ex = submit_exception(conn, actors["geologist"], "X201", 110.0,
                          "2026-10-20", "2026-10-25", "邻井压裂波及，窗口期上调")
    try:
        review_exception(conn, actors["geologist"], ex["id"], True)
    except AuthorizationError as exc:
        show("地质自批被拒绝", exc)
    review_exception(conn, actors["planner"], ex["id"], True, "计划岗独立复核同意")
    show("独立审批", f"例外 {ex['id']} 已由供气计划批准，窗口内替代基数口径")

    step("11. 商业用户只能看到与本合同有关的供气信息")
    visible = supply.get_decision(conn, actors["buyer_a"], "CM-A-10", "2026-10-09")
    show("buyer_a（已授权）", f"可读取 CM-A-10 v{visible['version_no']}，状态 {visible['status']}")
    try:
        supply.get_decision(conn, actors["buyer_b"], "CM-A-10", "2026-10-09")
    except AuthorizationError as exc:
        show("buyer_b（未授权）被拒绝", exc)
    try:
        supply.add_restriction(
            conn, actors["buyer_a"], "CN-01", 1.0, "2026-10-20", "2026-10-20", "x")
    except AuthorizationError as exc:
        show("商业用户内部写操作被拒绝", exc)

    step("12. 追溯：从供气数字到井况、作业决定、限输与责任人")
    trace = supply.trace_supply(conn, actors["dispatcher"], "CM-A-10", "2026-10-11")
    for party in trace["responsible_parties"]:
        show("责任人", f"{party.get('name')}({party.get('department')}): "
                      + "；".join(party["actions"]))

    step("13. 制造跨重启待办：待复核测试 + 检修锁（缺口提醒与隔离件已存在）")
    testing.register_test(conn, actors["driller"], "T-2026-002", "X302",
                          "2026-10-01", 88.0)
    operations.create_lock(conn, actors["maintainer"], "2026-10-25", "2026-10-26",
                           "CN-01 收球筒检修", channel_code="CN-01")
    gaps = supply.open_gap_alerts(conn, actors["dispatcher"])
    show("重启前开放缺口", [(g["commitment_code"], g["gas_date"], g["gap_volume"]) for g in gaps])

    # 模拟进程重启：关闭连接，重新打开同一个文件库
    conn.close()
    conn = connect(DB_PATH)
    step("14. 系统重启后的值班工作台（待复核/检修解锁/缺口/例外/隔离全部保留）")
    wb = workbench(conn, auth.get_actor(conn, "dispatcher"))
    print("- 待复核测试:", [(t["batch_no"], t["well_code"], t["status"]) for t in wb["pending_tests"]])
    print("- 检修锁:", [(l.get("channel_code") or l.get("well_code"), l["lock_from"], l["reason"])
                       for l in wb["maintenance_locks"]])
    print("- 缺口提醒:", [(g["commitment_code"], g["gas_date"], g["gap_volume"]) for g in wb["gap_alerts"]])
    print("- 隔离材料:", [(q["receipt_no"], q["reason"]) for q in wb["quarantined_materials"]])
    print("- 待审批例外:", wb["pending_exceptions"])

    step("15. 重启后完成待办：维护解锁、复核测试、重新评估收口缺口")
    lock_id = conn.execute(
        "SELECT id FROM maintenance_locks WHERE status='locked' AND channel_id IS NOT NULL"
    ).fetchone()["id"]
    operations.unlock(conn, auth.get_actor(conn, "maintainer"), lock_id)
    testing.review_test(conn, auth.get_actor(conn, "dispatcher"), "T-2026-002", True)
    show("解锁与复核", "完成，历史记录仍可追溯")

    conn.close()
    print(f"\n演示完成，数据库保留在 {DB_PATH}（删除即可重跑）")


if __name__ == "__main__":
    main()
