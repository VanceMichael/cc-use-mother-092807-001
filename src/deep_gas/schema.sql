-- 深层气井产能承诺与复产决策后端
-- 所有业务表只追加（append-only）；更新通过版本表/事件表衔接，历史行永不物理覆盖。

PRAGMA foreign_keys = ON;

-- 参与方（井队、地质、维护、输气、调度、供气计划、商业用户）
CREATE TABLE IF NOT EXISTS actors (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    department  TEXT NOT NULL CHECK (department IN
                ('drilling','geology','maintenance','pipeline','dispatch','planning','commercial')),
    created_at  TEXT NOT NULL
);

-- 区块
CREATE TABLE IF NOT EXISTS blocks (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

-- 井（归属区块；大修后复用同一井，不新建井）
CREATE TABLE IF NOT EXISTS wells (
    id          TEXT PRIMARY KEY,
    block_id    TEXT NOT NULL REFERENCES blocks(id),
    code        TEXT NOT NULL UNIQUE,           -- 井号
    name        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'drilling'
                CHECK (status IN ('drilling','testing','trial','stable','suspended','abandoned')),
    created_at  TEXT NOT NULL
);

-- 储层解释版本：同一井同一储层版本号只能递增
CREATE TABLE IF NOT EXISTS reservoir_versions (
    id              TEXT PRIMARY KEY,
    well_id         TEXT NOT NULL REFERENCES wells(id),
    formation       TEXT NOT NULL,             -- 层系
    version_no      INTEGER NOT NULL,
    interpretation TEXT NOT NULL,              -- 解释结论
    submitted_by    TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (well_id, formation, version_no),
    CHECK (version_no >= 1)
);

-- 测试批次（一个批次汇聚同次测试的多条回执）
CREATE TABLE IF NOT EXISTS tests (
    id              TEXT PRIMARY KEY,
    well_id         TEXT NOT NULL REFERENCES wells(id),
    reservoir_id    TEXT REFERENCES reservoir_versions(id),
    batch_no        TEXT NOT NULL UNIQUE,      -- 测试批次编号
    test_date       TEXT NOT NULL,
    aof             REAL,                      -- 无阻流量 万方/日
    flow_rate       REAL,                      -- 测试日产 万方/日
    tubing_pressure REAL,
    status          TEXT NOT NULL DEFAULT 'pending_review'
                    CHECK (status IN ('pending_review','reviewed','rejected','converted')),
    reviewed_by     TEXT REFERENCES actors(id),
    reviewed_at     TEXT,
    created_at      TEXT NOT NULL
);

-- 产能谱系头：每个来源（测试批次/复产事件/例外/基线）最多一条谱系，杜绝重复累计。
CREATE TABLE IF NOT EXISTS capacity_claims (
    id              TEXT PRIMARY KEY,
    well_id         TEXT NOT NULL REFERENCES wells(id),
    reservoir_id    TEXT REFERENCES reservoir_versions(id),
    origin_kind     TEXT NOT NULL CHECK (origin_kind IN
                    ('test_review','workover_revival','exception','manual_baseline')),
    origin_id       TEXT NOT NULL,             -- tests.id / work_plan_events.id / exception_requests.id
    current_version INTEGER NOT NULL DEFAULT 0,
    created_by      TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (origin_kind, origin_id)
);

-- 产能声明版本：测试→试采→稳产是同一谱系上的接替版本；
-- 晋级时旧版本收口（valid_to），新版本 supersedes 旧版本，任一供气日至多一个有效版本。
CREATE TABLE IF NOT EXISTS capacity_claim_versions (
    id              TEXT PRIMARY KEY,
    claim_id        TEXT NOT NULL REFERENCES capacity_claims(id),
    version_no      INTEGER NOT NULL,
    basis_kind      TEXT NOT NULL CHECK (basis_kind IN
                    ('testing','trial','stable','workover','exception','baseline')),
    rate            REAL NOT NULL CHECK (rate >= 0),  -- 万方/日
    valid_from      TEXT NOT NULL,
    valid_to        TEXT,                      -- NULL 表示持续有效；被接替时收口
    confidence      TEXT NOT NULL CHECK (confidence IN ('low','medium','high')),
    confidence_note TEXT NOT NULL,             -- 置信依据（测试批次/解释版本/作业回执编号）
    supersedes      TEXT REFERENCES capacity_claim_versions(id),
    created_by      TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (claim_id, version_no)
);
CREATE INDEX IF NOT EXISTS idx_ccv_claim ON capacity_claim_versions(claim_id, valid_from, valid_to);

-- 作业方案（压裂调整 / 酸化解堵 / 大修复产 …）
CREATE TABLE IF NOT EXISTS work_plans (
    id          TEXT PRIMARY KEY,
    well_id     TEXT NOT NULL REFERENCES wells(id),
    plan_no     TEXT NOT NULL UNIQUE,
    kind        TEXT NOT NULL CHECK (kind IN
                ('workover','acidizing','fracturing','other')),
    status      TEXT NOT NULL DEFAULT 'proposed'
                CHECK (status IN ('proposed','approved','executed','cancelled')),
    proposed_by TEXT NOT NULL REFERENCES actors(id),
    approved_by TEXT REFERENCES actors(id),
    approved_at TEXT,
    created_at  TEXT NOT NULL
);

-- 方案事件流：只追加。停井与复产只改变尚未履行的安排，已形成的日报/决定不改写。
CREATE TABLE IF NOT EXISTS work_plan_events (
    id                  TEXT PRIMARY KEY,
    plan_id             TEXT NOT NULL REFERENCES work_plans(id),
    kind                TEXT NOT NULL CHECK (kind IN
                        ('approved','shutdown','acidizing','fracturing','revival','cancel')),
    effective_from      TEXT NOT NULL,         -- 生效日（停井开始/复产开始）
    effective_to        TEXT,                  -- 停井截止；NULL 表示尚未解除
    expected_gain       REAL,                  -- 复产/措施预期增量 万方/日
    confidence          TEXT CHECK (confidence IN ('low','medium','high')),
    note                TEXT NOT NULL DEFAULT '',
    recorded_by         TEXT NOT NULL REFERENCES actors(id),
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wpe_well_range ON work_plan_events(plan_id, effective_from, effective_to);

-- 检修锁：设备/井在窗口内不可用；解除需具备维护部门身份。
CREATE TABLE IF NOT EXISTS maintenance_locks (
    id          TEXT PRIMARY KEY,
    well_id     TEXT REFERENCES wells(id),
    channel_id  TEXT,                         -- 约束外输通道时填写
    reason      TEXT NOT NULL,
    lock_from   TEXT NOT NULL,
    lock_to     TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'locked'
                CHECK (status IN ('locked','unlocked')),
    created_by  TEXT NOT NULL REFERENCES actors(id),
    unlocked_by TEXT REFERENCES actors(id),
    unlocked_at TEXT,
    created_at  TEXT NOT NULL,
    CHECK (lock_to >= lock_from),
    CHECK (well_id IS NOT NULL OR channel_id IS NOT NULL)
);

-- 外输通道
CREATE TABLE IF NOT EXISTS export_channels (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    capacity    REAL NOT NULL CHECK (capacity >= 0),  -- 万方/日
    created_at  TEXT NOT NULL
);

-- 井 → 通道的外输路由
CREATE TABLE IF NOT EXISTS export_routes (
    id          TEXT PRIMARY KEY,
    well_id     TEXT NOT NULL REFERENCES wells(id),
    channel_id  TEXT NOT NULL REFERENCES export_channels(id),
    share       REAL NOT NULL DEFAULT 1.0 CHECK (share > 0 AND share <= 1),
    UNIQUE (well_id, channel_id)
);

-- 管网限输：只追加窗口；限输只影响尚未履行的供气安排。
CREATE TABLE IF NOT EXISTS channel_restrictions (
    id          TEXT PRIMARY KEY,
    channel_id  TEXT NOT NULL REFERENCES export_channels(id),
    reason      TEXT NOT NULL,
    cap_rate    REAL NOT NULL CHECK (cap_rate >= 0),    -- 窗口内通道上限 万方/日
    valid_from  TEXT NOT NULL,
    valid_to    TEXT NOT NULL,
    created_by  TEXT NOT NULL REFERENCES actors(id),
    created_at  TEXT NOT NULL,
    CHECK (valid_to >= valid_from)
);
CREATE INDEX IF NOT EXISTS idx_cr_channel_range ON channel_restrictions(channel_id, valid_from, valid_to);

-- 商业合同
CREATE TABLE IF NOT EXISTS contracts (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    customer    TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
-- 商业用户只能看到被授权合同的供气信息
CREATE TABLE IF NOT EXISTS contract_audience (
    contract_id TEXT NOT NULL REFERENCES contracts(id),
    actor_id    TEXT NOT NULL REFERENCES actors(id),
    PRIMARY KEY (contract_id, actor_id)
);

-- 供气承诺（合同年度/月度目标量，按日折算或给定日量）
CREATE TABLE IF NOT EXISTS commitments (
    id              TEXT PRIMARY KEY,
    contract_id     TEXT NOT NULL REFERENCES contracts(id),
    code            TEXT NOT NULL UNIQUE,
    daily_volume    REAL NOT NULL CHECK (daily_volume >= 0),  -- 万方/日
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    created_by      TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    CHECK (valid_to >= valid_from)
);

-- 供气提名（计划侧逐日申报的需求量）
CREATE TABLE IF NOT EXISTS nominations (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    gas_date        TEXT NOT NULL,
    volume          REAL NOT NULL CHECK (volume >= 0),
    created_by      TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (commitment_id, gas_date)
);

-- 承诺供应组合：承诺由哪些井的产能支撑（追溯链路的组成部分）
CREATE TABLE IF NOT EXISTS commitment_sources (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    well_id         TEXT NOT NULL REFERENCES wells(id),
    share           REAL NOT NULL DEFAULT 1.0 CHECK (share > 0 AND share <= 1),
    UNIQUE (commitment_id, well_id)
);

-- 供气决定：某供气日对承诺给出的可靠供应安排。只追加，调整产生新版本。
CREATE TABLE IF NOT EXISTS supply_decisions (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    gas_date        TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 0,
    UNIQUE (commitment_id, gas_date)
);
CREATE TABLE IF NOT EXISTS supply_decision_versions (
    id              TEXT PRIMARY KEY,
    decision_id     TEXT NOT NULL REFERENCES supply_decisions(id),
    version_no      INTEGER NOT NULL,
    committed_rate  REAL NOT NULL,             -- 本轮承诺可供应量 万方/日
    status          TEXT NOT NULL CHECK (status IN ('firm','conditional','shortfall')),
    reason          TEXT NOT NULL,
    evidence_json   TEXT NOT NULL,             -- 证据快照：井/产能声明/通道/检修/作业来源
    created_by      TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (decision_id, version_no)
);

-- 日报：同一供气日只追加更正版本；v1 为原始版，后续版本必须引用前驱。
CREATE TABLE IF NOT EXISTS daily_reports (
    id                  TEXT PRIMARY KEY,
    well_id             TEXT NOT NULL REFERENCES wells(id),
    gas_date            TEXT NOT NULL,
    current_version     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (well_id, gas_date)
);
CREATE TABLE IF NOT EXISTS daily_report_versions (
    id              TEXT PRIMARY KEY,
    report_id       TEXT NOT NULL REFERENCES daily_reports(id),
    version_no      INTEGER NOT NULL,
    actual_rate     REAL NOT NULL CHECK (actual_rate >= 0),
    note            TEXT NOT NULL DEFAULT '',
    correction_of   TEXT,                      -- 前驱版本 id
    recorded_by     TEXT NOT NULL REFERENCES actors(id),
    created_at      TEXT NOT NULL,
    UNIQUE (report_id, version_no)
);

-- 产能例外申请：提交人不得审批本人申请（地质人员不能自批）。
CREATE TABLE IF NOT EXISTS exception_requests (
    id              TEXT PRIMARY KEY,
    well_id         TEXT NOT NULL REFERENCES wells(id),
    requested_rate  REAL NOT NULL CHECK (requested_rate > 0),
    valid_from      TEXT NOT NULL,
    valid_to        TEXT NOT NULL,
    justification   TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','approved','rejected')),
    submitted_by    TEXT NOT NULL REFERENCES actors(id),
    reviewed_by     TEXT REFERENCES actors(id),
    reviewed_at     TEXT,
    review_note     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    CHECK (valid_to >= valid_from)
);

-- 承诺缺口提醒：跨重启保留，直至关闭。
CREATE TABLE IF NOT EXISTS gap_alerts (
    id              TEXT PRIMARY KEY,
    commitment_id   TEXT NOT NULL REFERENCES commitments(id),
    gas_date        TEXT NOT NULL,
    gap_volume      REAL NOT NULL,
    detail          TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open','resolved','dismissed')),
    created_at      TEXT NOT NULL,
    closed_by       TEXT REFERENCES actors(id),
    closed_at       TEXT,
    UNIQUE (commitment_id, gas_date)
);

-- 回执台账：同一回执编号只能成功登记一次（幂等）。
CREATE TABLE IF NOT EXISTS receipts (
    id              TEXT PRIMARY KEY,
    receipt_no      TEXT NOT NULL UNIQUE,
    source_kind     TEXT NOT NULL CHECK (source_kind IN
                    ('well_log','production_receipt','test_slip','work_report','meter_reading')),
    payload_json    TEXT NOT NULL,
    received_at     TEXT NOT NULL,
    processed_test_id    TEXT,                -- 已并入的测试批次
    processed_resource   TEXT                 -- 其他已处理资源标识
);

-- 隔离区：回执编号相同但载荷哈希不同，先隔离人工裁决，绝不累计。
CREATE TABLE IF NOT EXISTS quarantined_materials (
    id              TEXT PRIMARY KEY,
    receipt_no      TEXT NOT NULL,
    source_kind     TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    payload_hash    TEXT NOT NULL,
    reason          TEXT NOT NULL,
    received_by     TEXT REFERENCES actors(id),
    received_at     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'quarantined'
                    CHECK (status IN ('quarantined','accepted','discarded')),
    UNIQUE (receipt_no, payload_hash)
);
