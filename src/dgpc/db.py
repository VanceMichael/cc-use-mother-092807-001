"""SQLite schema 与连接管理。

所有业务状态都落盘，进程重启后待复核测试、检修解锁与承诺缺口提醒
均从下列表恢复。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
-- 元信息与计数器 ----------------------------------------------------------
create table if not exists meta (
    key   text primary key,
    value any not null
);

-- 参与方 -------------------------------------------------------------------
-- role: rig(井队) / geology(地质) / maintenance(维护) /
--       pipeline(输气) / dispatch(调度) / commercial(商业用户)
create table if not exists users (
    id          text primary key,
    name        text not null,
    role        text not null,
    token_hash  text not null unique,
    active      integer not null default 1,
    created_at  text not null
);

-- 商业用户与供气合同的可见范围；合同绑定交付外输通道
create table if not exists contracts (
    id          text primary key,
    name        text not null,
    user_id     text not null references users(id),
    channel_id  text not null references export_channels(id),
    created_at  text not null
);

-- 区块 / 井 / 储层解释 ------------------------------------------------------
create table if not exists blocks (
    id     text primary key,
    name   text not null
);

create table if not exists wells (
    id        text primary key,
    block_id  text not null references blocks(id),
    name      text not null,
    status    text not null default 'planned',
        -- planned/testing/trial/stable/shut_in
    unique(block_id, name)
);

-- 储层解释版本：同一口井只递增，永不覆盖
create table if not exists reservoir_interpretations (
    id              text primary key,
    well_id         text not null references wells(id),
    version         integer not null,
    submitted_by    text not null references users(id),
    submitted_at    text not null,
    formation       text not null,
    payload         text not null,   -- 解释细节（JSON）
    unique(well_id, version)
);

-- 外来材料（测井/生产回执）---------------------------------------------------
-- status: accepted(已采用) / duplicate(重复到达,不累计) /
--         quarantined(同号不同值,隔离) / released(隔离处置后放行采用) /
--         discarded(隔离处置后丢弃)
create table if not exists inbound_materials (
    id                 text primary key,
    material_no        text not null unique,
    kind               text not null,          -- well_log / production_receipt
    well_id            text references wells(id),
    payload_hash       text not null,
    payload            text not null,
    received_at        text not null,
    status             text not null,
    first_id           text,                   -- 重复件指向首件
    resolution_by      text references users(id),
    resolution_at      text,
    resolution_note    text,
    chosen_variant_id  text
);

-- 同号不同值的隔离变体（首件之外的每一次到达都留痕，绝不覆盖首件）
create table if not exists inbound_material_variants (
    id            text primary key,
    material_id   text not null references inbound_materials(id),
    seq           integer not null,
    payload_hash  text not null,
    payload       text not null,
    received_by   text not null references users(id),
    received_at   text not null,
    unique(material_id, seq)
);

-- 测试批次 ------------------------------------------------------------------
-- status: pending_review / approved / rejected
create table if not exists test_batches (
    id                 text primary key,
    well_id            text not null references wells(id),
    interpretation_id  text not null references reservoir_interpretations(id),
    material_id        text references inbound_materials(id),
    submitted_by       text not null references users(id),
    submitted_at       text not null,
    flow_rate          real not null,          -- 万方/日
    test_date          text not null,
    basis              text not null,          -- 测试方法/制度等置信依据
    confidence         text not null,          -- high / medium / low
    status             text not null default 'pending_review',
    reviewed_by        text references users(id),
    reviewed_at        text,
    review_note        text
);

-- 产能声明 ------------------------------------------------------------------
-- stage: tested / trial / stable；同一口径只允许一条 active。
-- status: active / superseded / withdrawn
create table if not exists capacity_offers (
    id                text primary key,
    well_id           text not null references wells(id),
    stage             text not null,
    valid_from        text not null,
    valid_to          text not null,
    rate              real not null,          -- 万方/日
    confidence        text not null,
    basis             text not null,
    source_type       text not null,          -- test_batch / exception / promotion
    source_id         text not null,
    created_by        text not null references users(id),
    created_at        text not null,
    status            text not null default 'active',
    superseded_by     text,
    withdrawn_at      text,
    withdraw_reason   text,
    -- 同一来源（测试批次/例外/被转正的旧产能）只能产生一条产能声明：
    -- 测试、试采、稳产之间的口径变化以更替衔接，不得当作新增产量重复承诺
    unique(source_type, source_id)
);

-- 产能例外（地质发起，调度审批；提交人不得审批）------------------------------
-- status: pending / approved / rejected
create table if not exists capacity_exceptions (
    id           text primary key,
    well_id      text not null references wells(id),
    rate         real not null,
    valid_from   text not null,
    valid_to     text not null,
    confidence   text not null,
    reason       text not null,
    submitted_by text not null references users(id),
    submitted_at text not null,
    status       text not null default 'pending',
    reviewed_by  text references users(id),
    reviewed_at  text,
    review_note  text
);

-- 作业方案（大修/酸化解堵/压裂调整）------------------------------------------
-- status: scheduled / in_progress / done / cancelled
create table if not exists work_programs (
    id           text primary key,
    well_id      text not null references wells(id),
    kind         text not null,   -- workover / acidizing / fracturing
    planned_start text not null,
    planned_end  text not null,
    status       text not null default 'scheduled',
    created_by   text not null references users(id),
    created_at   text not null,
    note         text
);

-- 作业方案的时间变更（只允许改动尚未履行的安排）-------------------------------
create table if not exists work_program_revisions (
    id          text primary key,
    program_id  text not null references work_programs(id),
    old_start   text not null,
    old_end     text not null,
    new_start   text not null,
    new_end     text not null,
    reason      text not null,
    changed_by  text not null references users(id),
    changed_at  text not null
);

-- 设备检修窗口 ---------------------------------------------------------------
-- status: await_unlock(到期待解锁) / locked(停井中) /
--         unlocked(已复工) / cancelled
create table if not exists maintenance_windows (
    id           text primary key,
    well_id      text not null references wells(id),
    lock_from    text not null,
    unlock_due   text not null,
    status       text not null default 'locked',
    created_by   text not null references users(id),
    created_at   text not null,
    unlocked_by  text references users(id),
    unlocked_at  text,
    note         text
);

create table if not exists maintenance_revisions (
    id          text primary key,
    window_id   text not null references maintenance_windows(id),
    old_due     text not null,
    new_due     text not null,
    reason      text not null,
    changed_by  text not null references users(id),
    changed_at  text not null
);

-- 外输通道与限输 -------------------------------------------------------------
create table if not exists export_channels (
    id          text primary key,
    name        text not null,
    capacity    real not null           -- 万方/日
);

create table if not exists well_routes (
    well_id     text not null references wells(id),
    channel_id  text not null references export_channels(id),
    share       real not null default 1.0,  -- 该井经此通道外输的比例
    primary key (well_id, channel_id)
);

-- status: scheduled(限输尚未生效) / active / lifted / cancelled
create table if not exists curtailments (
    id          text primary key,
    channel_id  text not null references export_channels(id),
    limit_rate  real not null,          -- 受限后的通道能力
    valid_from  text not null,
    valid_to    text not null,
    status      text not null default 'scheduled',
    created_by  text not null references users(id),
    created_at  text not null,
    note        text
);

create table if not exists curtailment_revisions (
    id             text primary key,
    curtailment_id text not null references curtailments(id),
    old_valid_from text not null,
    old_valid_to   text not null,
    new_valid_from text not null,
    new_valid_to   text not null,
    reason         text not null,
    changed_by     text not null references users(id),
    changed_at     text not null
);

-- 供气安排（按承诺/日/井的细项，事件只改尚未履行部分）-------------------------
-- status: planned / fulfilled / revised / cancelled
create table if not exists supply_arrangements (
    id              text primary key,
    commitment_id   text not null,       -- FK 在承诺表创建后逻辑保证
    gas_date        text not null,
    well_id         text not null references wells(id),
    planned_rate    real not null,
    actual_rate     real,
    status          text not null default 'planned',
    created_at      text not null,
    revised_at      text,
    revise_reason   text,
    unique(commitment_id, gas_date, well_id)
);

-- 安排每次随作业/检修/限输重规划的留痕（只允许改尚未履行的安排）
create table if not exists arrangement_revisions (
    id              text primary key,
    arrangement_id  text not null references supply_arrangements(id),
    old_rate        real not null,
    new_rate        real not null,
    reason          text not null,
    changed_by      text references users(id),
    changed_at      text not null
);

-- 日报（定稿不可变；更正以新版本衔接）----------------------------------------
create table if not exists daily_reports (
    id                text primary key,
    well_id           text not null references wells(id),
    gas_date          text not null,
    version           integer not null,
    rate              real not null,
    status            text not null default 'draft',  -- draft / finalized / corrected
    submitted_by      text not null references users(id),
    submitted_at      text not null,
    basis_material_id text references inbound_materials(id),
    corrects_id       text references daily_reports(id),
    correction_note   text,
    unique(well_id, gas_date, version)
);

-- 供气承诺 -------------------------------------------------------------------
create table if not exists supply_commitments (
    id           text primary key,
    contract_id  text not null references contracts(id),
    gas_date     text not null,
    volume       real not null,          -- 万方/日承诺量
    status       text not null default 'committed',
        -- committed / revised / fulfilled / cancelled
    created_by   text not null references users(id),
    created_at   text not null,
    note         text
);

-- 供气决定（承诺当日的调度决定；可更正，旧版 superseded）----------------------
-- status: effective / superseded
create table if not exists supply_decisions (
    id               text primary key,
    commitment_id    text not null references supply_commitments(id),
    version          integer not null,
    decided_by       text references users(id),
    decided_at       text not null,
    reliable_total   real not null,      -- 决定时测算的可靠供应
    allocated_total  real not null,      -- 实际对该承诺的分配
    snapshot         text not null,      -- 井况/作业/检修/通道快照 JSON
    status           text not null default 'effective',
    superseded_by    text,
    note             text,
    unique(commitment_id, version)
);

-- 承诺缺口提醒（重启后继续存在，直到消除或关闭）--------------------------------
-- status: open / resolved / closed
create table if not exists commitment_gaps (
    id            text primary key,
    commitment_id text not null references supply_commitments(id),
    gas_date      text not null,
    shortage      real not null,
    reason        text not null,
    status        text not null default 'open',
    detected_at   text not null,
    resolved_at   text,
    note          text
);

create index if not exists idx_offers_well on capacity_offers(well_id, status, stage);
create index if not exists idx_test_status on test_batches(status);
create index if not exists idx_window_status on maintenance_windows(status, unlock_due);
create index if not exists idx_gaps_status on commitment_gaps(status);
create index if not exists idx_arrangements on supply_arrangements(commitment_id, gas_date, status);
create index if not exists idx_decisions_commitment on supply_decisions(commitment_id, status);
"""


def connect(
    path: str | Path, *, check_same_thread: bool = True
) -> sqlite3.Connection:
    path = str(path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("pragma foreign_keys=on")
    conn.execute("pragma journal_mode=wal")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    existing = conn.execute(
        "select value from meta where key='schema_version'"
    ).fetchone()
    if existing is None:
        conn.execute(
            "insert into meta(key,value) values('schema_version',?)",
            (SCHEMA_VERSION,),
        )
    conn.commit()
