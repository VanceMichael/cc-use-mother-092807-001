# 深层气井产能承诺与复产决策后端

把**区块、井、储层解释版本、测试批次、作业方案、设备检修、日产曲线、外输通道和供气承诺**关联为
一套只追加、可追溯、重启可恢复的生产承诺与复产决策后端。纯 Python 3.11 标准库实现，
持久化使用 SQLite，无第三方依赖、不连接外部系统。

## 要解决的问题

井队、地质、维护、输气各自报数时：

- 一口井从测试、试采到稳产的口径变化被当成新增产量重复承诺；
- 同一测井/生产回执重复到达被再次累计；编号相同、数值不同的材料无人发现；
- 维修停井、酸化解堵、压裂调整、管网限输被拿去回改已经形成的日报和供气决定；
- 地质人员自己批准自己提交的产能例外；商业用户能看到与本合同无关的供气信息；
- 系统重启后，待复核测试、检修解锁、承诺缺口提醒全部丢失。

## 领域规则落地

| 规则 | 实现 |
| --- | --- |
| 任何可用产能必须说明**适用时间与置信依据** | `capacity_claims` + `capacity_claim_versions`：rate、valid_from/valid_to、confidence(low/medium/high)、confidence_note（引用测试批次/储层版本/作业/例外） |
| 测试→试采→稳产不是三笔新增产量 | 同一产能谱系上的**版本接替**：旧版本在新版本生效前一日收口（valid_to），任一供气日恰好命中一个版本；只允许逐级晋级 |
| 同一测试/复产事件/例外不重复累计 | 谱系头对 `(origin_kind, origin_id)` 唯一；同井同层系生效日重叠的并行测试谱系被拒绝 |
| 回执重复到达不再累计 | `receipts.receipt_no` 唯一 + 载荷哈希；完全重复返回 `duplicate` |
| 编号相同、数值不同先隔离 | 进入 `quarantined_materials`，不覆盖、不累计，仅调度可裁决 accept/discard |
| 维修停井/酸化/压裂/限输只改尚未履行的安排 | 以生效窗口表达，评估时按供气日实时折减；**历史供气决定版本与日报永不回改** |
| 已形成的日报与供气决定通过更正版本衔接 | 日报 v1→v2（correction_of 前驱）；供气决定重评生成新版本并冻结证据快照 evidence_json |
| 地质不能批准自己的产能例外 | 审批岗位集合不含 geology，且提交人本人（即使换岗）不可审批，方案提出人也不能自批方案 |
| 商业用户只看本合同 | `contract_audience` 合同级授权；供气决定与缺口按合同过滤，内部写操作一律拒绝 |
| 重启后继续待办 | 待复核测试、检修锁、缺口提醒、待审批例外、隔离材料全部持久化，工作台聚合恢复 |
| 从供气数字追溯到井况与责任人 | `GET /api/supply/trace` 返回逐井贡献、停阻原因、限输证据及责任岗位/姓名 |

## 可靠供应是怎样算出来的

对每个承诺 × 供气日：

1. 取供应组合中每口井当日有效的产能版本；例外窗口内例外**替代**基数（不叠加），
   复产/措施产能作为**增量**叠加；有测试口径时基线让位（老井复测不双算）。
2. 井处于停井/措施窗口或检修锁内 → 当日不可用，记录阻停原因与登记人。
3. 按路由把产量送入外输通道；通道基础能力与当日限输窗口取小，通道检修锁令其为 0；
   firm（高置信）优先通过，剩余能力给 conditional。
4. 与当日提名（无提名则用承诺日量）比较：firm 足量= `firm`，加上条件量才够=`conditional`，
   否则=`shortfall` 并开缺口提醒；重评不再短欠时自动收口。
5. 冻结一个只追加的供气决定版本，证据快照包含全部井况、作业、通道证据。

## 代码结构

```
src/deep_gas/
  schema.sql            全部表结构（只追加 + 版本/事件表）
  db.py                 SQLite 连接与幂等建库
  auth.py               参与方、部门 RBAC、合同可见范围
  catalog.py            区块/井/储层解释版本(单调递增)/通道/路由/合同
  inbound.py            回执幂等登记与同号异值隔离/裁决
  testing.py            测试批次(待复核)、产能谱系、逐级晋级、稳产基线
  operations.py         作业方案、停井/酸化/压裂/复产事件、检修锁与解锁
  reports.py            井日报只追加与更正版本链
  capacity_exceptions.py 产能例外申请与独立审批（职责分离）
  supply.py             限输窗口、承诺/提名/供应组合、可靠供应评估、
                        版本化供气决定、缺口提醒、端到端追溯
  recovery.py           值班工作台聚合（重启恢复）
  api.py                http.server JSON API（X-Actor-Id 鉴权）
scripts/demo.py         15 步端到端演示（含关闭进程重开文件库）
```

## 开发命令

```bash
# 全部测试（30 项）
python3 -m unittest discover -s tests -v

# 编译检查
python3 -m compileall -q src scripts tests

# 端到端演示（自动重建 data/demo.db）
python3 scripts/demo.py

# 启动 HTTP 服务
python3 -m src.deep_gas.api --db data/deep_gas.db --port 8080
```

## HTTP API 摘要

所有写接口与绝大多数读接口需要 `X-Actor-Id` 头。领域违规返回
`400`，越权 `403`，不存在 `404`，回执冲突隔离 `409`。

- 引导：`POST /api/actors`
- 目录：`POST /api/blocks` `/api/wells` `/api/reservoir-versions`
  `/api/channels` `/api/routes` `/api/contracts` `/api/contracts/audience`
- 来件：`POST /api/receipts` `/api/quarantine/resolve`，`GET /api/quarantine`
- 测试与产能：`POST /api/tests` `/api/tests/review` `/api/capacity/from-test`
  `/api/capacity/promote` `/api/baselines`，`GET /api/tests/pending` `/api/claims`
- 作业与检修：`POST /api/plans` `/api/plans/approve` `/api/plans/shutdown`
  `/api/plans/revival` `/api/locks` `/api/locks/unlock`，`GET /api/locks`
- 外输与承诺：`POST /api/restrictions` `/api/commitments`
  `/api/commitments/sources` `/api/nominations` `/api/supply/evaluate`
- 日报与例外：`POST /api/reports` `/api/exceptions` `/api/exceptions/review`，
  `GET /api/reports` `/api/exceptions/pending`
- 决定、追溯与工作台：`GET /api/supply/decision` `/api/supply/trace`
  `/api/gaps` `/api/workbench`，`POST /api/gaps/dismiss`

示例：

```bash
curl -s localhost:8080/api/supply/trace \
  -H 'X-Actor-Id: disp' \
  -G --data-urlencode commitment_code=CM-A-10 --data-urlencode gas_date=2026-10-11
```

> 原领域上下文骨架（`src/news_context_001.py`、`fixtures/`、`contracts/`）保持可用，
> 其测试继续通过。
