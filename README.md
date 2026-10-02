# 深层气井产能承诺与复产决策后端

管理深层气井**测试 → 试采 → 稳产**的口径演进、大修/酸化/压裂复产、设备检修、
管网限输与对外供气承诺，保证每一方被承诺的产量都能说明：**适用什么时间、
置信依据是什么、由哪口井经哪条通道交付、谁批准、出了问题如何追溯更正**。

纯 Python 标准库实现（Python ≥ 3.11，sqlite3 + http.server），无外部依赖。

## 解决的核心问题

- **口径不重复承诺**：测试、试采、稳产是同一条产能链的版本更替（superseded），
  不是三份新增产量；同一测试批次/例外只能生成一条产能声明。
- **任何产能都带时间窗与置信依据**：valid_from / valid_to、high|medium|low、
  测试制度、解释版本、复核人；low 置信产能不计入可担保的可靠供应。
- **事件只改尚未履行的安排**：维修停井、酸化/压裂、管网限输自动重排未来
  （或当日尚无日报的）供气安排；已经形成的日报与供气决定不动，只通过
  **更正版本**衔接，每次重算留痕。
- **回执幂等与隔离**：同一测井/生产回执重复到达不累计；编号相同数值不同
  先整组隔离，调度选择以哪个变体为准后才能作为业务依据。
- **职责分离**：地质人员不能批准自己提交的测试或产能例外；井队、维护、
  输气、调度各有动作边界。
- **商业可见性**：商业用户只能看到与自己合同有关的供气信息，快照中
  无关通道/井的测算数据被脱敏。
- **重启继续值班**：待复核测试、到期待解锁检修、开放承诺缺口、隔离材料、
  待审批例外全部落盘，进程重启即恢复。
- **端到端追溯**：从某天某合同的供气数字，可下钻到逐井安排、产能血缘、
  储层解释版本、测试批次与回执、作业/检修/限输决定和各环节责任人。

## 领域对象

| 模块 | 对象 |
|---|---|
| `catalog` | 区块 blocks、井 wells、外输通道 export_channels、井-通道路由 well_routes |
| `materials` | 外来材料 inbound_materials（测井/生产回执）、同号异值变体、隔离与处置 |
| `geology` | 储层解释版本 reservoir_interpretations（按井只增版本号）、测试批次 test_batches |
| `capacity` | 产能声明 capacity_offers（tested/trial/stable/exception）、产能例外 capacity_exceptions |
| `operations` | 作业方案 work_programs（大修/酸化/压裂）、检修窗口 maintenance_windows、管网限输 curtailments |
| `projection` | 可靠供应测算：时间窗内 active 产能 × 停井阻断 × 通道限输系数 |
| `commitments` | 供气合同（绑定交付通道）、供气承诺、供气决定（版本+快照）、逐井安排、承诺缺口 |
| `reports` | 单井日报：定稿不可变，更正生成 version+1 并链回旧版 |
| `trace` | 供气日/承诺/单井三级追溯与责任人组装 |
| `workflow` | 服务编排、事件钩子、启动恢复、值班队列 |

## 目录

```
src/dgpc/
  db.py            sqlite schema（全部状态落盘）
  errors.py        领域错误与 HTTP 状态码
  util.py          日期/哈希/编号工具
  auth.py          令牌、六种角色、动作权限、合同可见性
  catalog.py       区块/井/通道/路由
  materials.py     回执幂等、同号异值隔离与处置
  geology.py       解释版本、测试批次复核
  capacity.py      产能声明、转正更替、例外审批
  operations.py    作业/检修/限输及当日阻断判定
  projection.py    可靠供应测算（停井/置信/通道限输）
  commitments.py   承诺、决定版本、逐日逐通道联合分配、缺口
  reports.py       日报定稿与更正链
  trace.py         追溯与责任人
  workflow.py      ServiceHub 编排、事件钩子、recover()
  api.py           表驱动 JSON HTTP API
  server.py        HTTP 服务装配
  bootstrap.py     合成引导数据（不含真实身份信息）
tests/dgpc/        48 个领域与 HTTP 端到端测试
```

## 运行

初始化并启动（合成演示数据）：

```bash
python3 -m src.dgpc --host 127.0.0.1 --port 8080 \
  --db data/dgpc.sqlite3 --seed --print-tokens data/tokens.json
```

`--seed` 仅在空库时写入演示数据；角色令牌只在创建时返回一次。
生产首启时，系统在**尚无任何用户**的状态下允许无令牌创建第一个调度账号，
之后所有账号管理都需要调度权限。

不带 `--seed` 重启即自动恢复值班队列：

```bash
python3 -m src.dgpc --db data/dgpc.sqlite3
```

## 测试与检查

```bash
python3 -m unittest discover -s tests -v   # 48 个测试
python3 -m compileall -q src               # 编译检查
```

## HTTP API 摘要

认证：`Authorization: Bearer <令牌>`；除首启建号外全部端点需要认证。

| 方法 | 路径 | 说明 | 角色 |
|---|---|---|---|
| POST | `/admin/users` `/admin/contracts` | 账号、合同（合同绑定交付通道） | 调度 |
| GET/POST | `/blocks` `/wells` `/channels` | 主数据；`POST /blocks/{id}/wells`、`POST /wells/{id}/routes` | 调度/输气 |
| POST/GET | `/materials`，`POST /materials/{id}/resolve` | 回执登记（幂等/隔离）、隔离处置 | 井队·地质·维护 / 调度 |
| POST | `/wells/{id}/interpretations` | 储层解释（版本自增） | 地质 |
| POST | `/wells/{id}/tests`，`POST /tests/{id}/review` | 测试提交、复核（提交人不可复核） | 地质·井队 / 调度 |
| POST | `/tests/{id}/offer` | 复核通过后登记 tested 产能 | 调度 |
| POST | `/offers/{id}/promote` `/withdraw` | 转正（tested→trial→stable，旧版更替）/ 撤销 | 调度 |
| POST | `/wells/{id}/exceptions`，`POST /exceptions/{id}/review` | 产能例外提交 / 审批（提交人不可审批） | 地质 / 调度 |
| POST | `/wells/{id}/programs` + `/programs/{id}/reschedule|start|complete|cancel` | 大修/酸化/压裂 | 井队·维护·调度 |
| POST | `/wells/{id}/windows` + `/windows/{id}/reschedule|unlock` | 检修窗口与人工解锁 | 维护·调度 |
| POST | `/channels/{id}/curtailments` + `/curtailments/{id}/reschedule|lift` | 管网限输 | 输气·调度 |
| POST/GET | `/wells/{id}/reports`（`"correct": true` 为更正） | 日报定稿/更正 | 井队·维护·调度 |
| POST/GET | `/commitments`，`POST /commitments/{id}/decisions/correct` | 供气承诺、决定更正 | 调度 |
| GET | `/supply/reliable?date=YYYY-MM-DD` | 当日可靠供应测算 | 内部角色 |
| GET | `/duty/queue`，`POST /duty/recover` | 值班队列 / 启动恢复 | 内部角色 |
| GET | `/gaps`，`POST /gaps/{id}/close` | 缺口提醒与手工关闭 | 调度 |
| GET | `/trace?date=`、`/commitments/{id}/trace`、`/wells/{id}/trace?date=` | 追溯（商业用户自动按合同脱敏） | 按范围 |

## 关键规则

- **可靠供应** = 当日时间窗覆盖的唯一 active 产能；被作业/检修阻断的井为 0；
  low 置信产能只显示为参考量；逐通道汇总后超过通道能力（含生效限输）时
  按比例削减。
- **供气决定快照**：每次承诺登记或供应条件变化，把当日测算、逐井安排和
  井况理由完整存入决定版本；旧版 `superseded` 永不删除。
- **逐日逐通道联合分配**：同一交付通道、同一供气日的多个承诺按登记顺序
  从同一能力池分配，已被其他承诺（或已履行日报）占用的能力不会重复承诺。
- **日报即冻结**：某日某井日报定稿后，对应安排转为 fulfilled，之后任何
  停井/限输事件都不能改动它；日报数值修正只能走更正版本。

## 原有资料

`contracts/context.schema.json`、`fixtures/context.json`、
`src/news_context_001.py` 保留，用于读取和校验领域上下文资料
（参与方、已确认事实、业务约束）。
