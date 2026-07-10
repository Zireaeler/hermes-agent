# Hermes Kanban Runtime Kernel 真实集成验证台账

本文档记录 Hermes Kanban Runtime Kernel 对真实模型源和真实 worker 的集成验证计划、
脱敏运行证据和阶段门槛。

它不是架构设计文档，也不替代各 Phase 的实现文档。各 Phase 文档描述能力边界和实现
状态；本文档只回答：某项能力是否真的在隔离环境中连接过真实 provider / worker，结果
是什么，哪些质量门槛仍未达到。

## 1. 记录原则

真实集成验证必须与默认单元测试分开。

- 默认 pytest 保持离线、deterministic，使用 fake provider、synthetic receipt 或 fixture；
- 真实验证必须显式 opt-in，使用隔离 `HERMES_HOME` 和隔离 Kanban DB；
- 主 `.codex` 只允许读取，真实运行优先使用隔离 `CODEX_HOME` 副本，不能修改主
  `config.toml` 或 `auth.json`；
- 文档、DB event summary、CLI output 和日志不得记录 API key、credential、完整 prompt 或完整 raw response；
- decision segment 的 provider raw output 仅作为隔离 DB 内的审计归档；它仍不得包含 credential，且不能作为默认 CLI 或文档内容展示；
- 每次运行必须记录代码 commit、provider alias、model、场景、结果、fallback、consistency 和结论；
- “调用路径已实现”、“真实调用成功”、“真实模型质量通过”是不同结论，不能混写。

## 2. 证据等级

| 等级 | 含义 | 默认 pytest 可替代吗 |
| --- | --- | --- |
| L0 | fake / deterministic 单元和集成测试 | 是 |
| L1 | 真实 provider transport、parse 和 validator dry-run | 否 |
| L2 | 真实 decision provider 经生产 `advance_runtime_job()` 完成一次 patch apply/reject 审计 | 否 |
| L3 | 真实 compaction provider 生成 candidate 并通过 checkpoint validator，无 fallback | 否 |
| L4 | 真实 decision provider 加 synthetic worker evidence 的 3-5 轮 bounded loop | 否 |
| L5 | 真实 decision provider、真实 worker lane、Kanban evidence ingest 的端到端 smoke | 否 |

L0 是发布前的基础保障，但不能证明模型源、模型输出质量、provider transport 或真实
worker lifecycle。L1-L5 都必须保留可复核的脱敏结果。

## 3. 隔离运行规范

每次真实验证使用一次性目录：

```bash
export HERMES_HOME="$(mktemp -d)"
```

保留真实用户 `HOME`，使 `--codex-config` 只读当前 `.codex` 模型源；不要把 `HOME`
改到临时目录，否则会错误地把不存在的临时 `.codex` 当成验证对象。

执行前后至少检查：

- runtime job、Kanban DB、artifact 和 report 位于隔离 `HERMES_HOME`；
- `~/.codex/config.toml`、`~/.codex/auth.json` 未被修改；
- 运行报告仅记录 provider alias、model 和 ref hash；
- 隔离 DB 不包含 API key；
- `check_runtime_consistency()` 通过，或 warning/violation 有明确解释。

推荐入口：

```bash
hermes kanban runtime real-smoke <job_id> --json
hermes kanban runtime real-smoke <job_id> --execute-decision --codex-config --json
hermes kanban runtime real-smoke <job_id> --apply-decision --codex-config --json
hermes kanban runtime real-smoke <job_id> --compact --codex-config --json
```

具体命令语义见 `docs/kanban-runtime-kernel-phase4g1.md`。本台账不重复完整 runbook。

## 4. 验证矩阵

| 场景 | 对应阶段 | 当前状态 | 通过条件 | 当前结论 |
| --- | --- | --- | --- | --- |
| decision dry-run | Phase 3 / 4G1 | 已验证 | 不调用模型、不写 decision/patch/revision | 通过 |
| real decision execute | Phase 3 / 4G1 | 已验证 | parsed 或明确 provider/parse error；validator dry-run 不 apply | 通过 |
| real decision one-step apply | Phase 3B / 4G1 | 已验证 | patch 只能经 validator apply/reject；完整 audit | 通过，包含一次 rejected 和一次 applied |
| real compaction transport + fallback | Phase 4A / 4G1 | 已验证 | candidate 经 validator；拒绝不污染 active segment；fallback 可审计 | 通过 |
| real compaction candidate quality | Phase 4G5 | 已验证 | 至少一次真实 candidate 无 fallback 通过 checkpoint validator | 通过：真实 candidate 自带 gap provenance，validator accepted，segment rollover |
| real compaction multi-cycle | Phase 4G6 | 已验证 | 至少三次真实 no-fallback compaction，覆盖多个 profile，checkpoint chain valid | 通过：3/3 accepted，三个 profile，fresh process chain valid，consistency 0/0 |
| real provider bounded loop | Phase 4G2 | 已验证 | 3-5 decision ticks + synthetic worker evidence + consistency passed | 通过：3 ticks、2 applied、1 rejected、synthetic receipt `failed -> succeeded`、最终 done |
| real worker lane smoke | Phase 4G3 | 已验证 | 真实 worker receipt 进入 runtime ingest，端到端 consistency passed | 通过：两个 dispatcher-started Codex worker receipt 写入 verified ledger，job done |
| delegation Profile v2 | Delegation Policy MVP | 已验证 | 单 coherent primary node、typed contract、无无理由 decomposition、validator dry-run | 通过：1 个 immediate `create_node`、contract 1/1、accepted、未 apply |
| delegation initialization | Delegation Initialization MVP | 已验证 | provider-first 空 graph、单 primary node、单 worker attempt、真实 receipt、job done | 通过：1 decision、1 patch、1 node、1 attempt、ledger full/verified、consistency 0/0 |

“real compaction transport + fallback 通过”不等于“真实 compaction 质量通过”。如果模型
candidate 被 validator 拒绝，fallback 成功只能证明安全边界和恢复路径正确。

Phase 4G5 已补齐单次真实 candidate L3，但这仍不等于多轮长任务 compaction 稳定性通过。
后者必须由多 segment、stale rejection、fallback degradation 和恢复组合的 soak 单独证明。

## 5. 当前真实运行记录

### 2026-07-10 Phase 4G1 isolated smoke

运行代码：`24ad81a feat(kanban): add runtime real provider smoke`

环境：隔离 `HERMES_HOME` 和 Kanban DB；当前 `.codex` 模型源只读；未启动真实 worker
lane。

脱敏模型标识：`codex:MySub2api` / `gpt-5.6-terra`。

结果：

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| real-smoke dry-run | 通过 | `no_tools=true`、`single_shot=true`；graph revision、kernel decisions、graph patches 均未改变 |
| real decision execute | 通过 | response parsed；validator dry-run `accepted`；不 apply |
| first one-step apply | 安全拒绝 | `strategy_update.changes_from_previous_attempts` 被模型输出为字符串，validator 拒绝；graph revision 保持 0 |
| second one-step apply | 通过 | 模型看到了 rejection feedback，返回合法 patch；graph revision 从 0 增至 1；decision / patch audit 完整 |
| real compaction | fallback 通过 | 模型 candidate parsed，但 `open_goal_gaps` 缺 provenance；checkpoint validator 拒绝，deterministic fallback 创建 accepted checkpoint 并 rollover segment |
| consistency | 通过 | 最终 0 violations、0 warnings |
| credential scan | 通过 | 隔离 DB 的 runtime tables 未发现 API key |

本次结果证明真实模型源可通过 decision 和 compaction 的 runtime 边界，但不证明真实
checkpoint candidate 已具备稳定质量。compaction provenance 是当前最明确的模型输出质量
缺口。

### 2026-07-10 Phase 4G1 repeat 与 Phase 4G2 isolated bounded loop

运行代码：`17df67a feat(kanban): add real provider bounded loop`

环境：新的隔离 `HERMES_HOME` 和 Kanban DB；当前 `.codex` 模型源只读；未启动真实 worker
lane。

脱敏模型标识：`codex:MySub2api` / `gpt-5.6-terra`。

结果：

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| G1 repeat decision execute | 安全拒绝 | response parsed；validator dry-run rejected；未 apply，consistency passed |
| G1 repeat one-step apply | 安全拒绝 | response 只经 production advance；patch rejected；graph revision 保持 0，consistency passed |
| G2 bounded loop | 通过 | 3 个真实 decision tick；2 个 patch applied、1 个 rejected；每次图变更均经过 parser、validator 和 patch audit |
| synthetic evidence | 通过 | 两个 synthetic receipt 按 `failed -> succeeded` 经 Kanban task / runtime ingest 写入 ledger；不是真实 worker evidence |
| final state | 通过 | required goal 由 ledger 满足，job 为 `done` |
| consistency | 通过 | 0 violations、0 warnings |
| credential scan | 通过 | report 和完整隔离 DB 均未发现 API key |

本次 L4 证明多轮真实 decision provider 与 synthetic evidence 的 runtime 闭环可用，并同时
保留 rejected patch 的审计事实。它不证明真实 worker lane L5，也不证明真实 compaction
candidate quality L3；在本次运行时，后者仍受 `open_goal_gaps` provenance 缺失限制，随后
由 Phase 4G5 补齐并验证。

### 2026-07-10 Phase 4G3 isolated real worker lane smoke

运行代码：`70a422c feat(kanban): add runtime worker lane smoke`，并包含默认 worker lane
materialization 修复。

环境：新的隔离 `HERMES_HOME`、Kanban DB 和 Git workspace；当前 `.codex` 模型源只读；
专属 `codex-runtime-smoke` lane 的 `max_concurrency=1`。

脱敏模型标识：`codex:MySub2api` / `gpt-5.6-terra`。

结果：

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| initial worker node | 通过 | dispatcher 启动 Hermes Codex worker wrapper；`understand-scope` receipt 经 parser/ingest 满足 `scope-understood` |
| real decision proposal | 通过 | provider patch 经 validator apply，创建 goal-linked `worker-smoke-result` node |
| default worker lane | 通过 | proposal 未显式 assignee 时，materialization 使用 job 保存的 `codex-runtime-smoke` 默认 lane；lane/capability 检查仍生效 |
| implementation worker node | 通过 | dispatcher 启动第二个真实 worker；receipt 声明并本地字节验证 `runtime_worker_smoke.txt` |
| ledger / completion | 通过 | 两个 required goal item 均为 full + verified，job 为 `done` |
| consistency | 通过 | 0 violations、0 warnings |
| credential scan | 通过 | 完整隔离 DB 未发现 API key |

本次 L5 证明单 worker 的真实 provider -> validator -> materialization -> dispatcher ->
Codex wrapper -> runtime receipt -> ingest -> ledger 路径可用。它不证明多 worker 并发、
review/test workflow 或 worker crash long-run recovery；真实 compaction candidate quality
后续由 Phase 4G5 单独验证。

### 2026-07-10 Delegation Policy Profile v2 isolated smoke

运行代码：`a95e128 feat(kanban): enforce worker delegation policy`，以及
`a344b15 fix(kanban): harden delegation provider smoke`。

环境：一次性隔离 `HERMES_HOME` 和 Kanban DB；当前 `.codex` 模型源只读；未启动真实
worker；只执行 validate-without-apply。

脱敏模型标识：`codex:MySub2api` / `gpt-5.6-sol`。

测试目标刻意把同一仓库内的代码调研、必要外部文档查阅、前后端实现、测试、debug 和最终
验证放入一个完整 OAuth 交付责任，用于检查 provider 是否仍按传统阶段拆分。

结果：

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| Profile load | 通过 | `graph_patch_decision` v2；profile hash 已进入 bounded report |
| real decision execute | 通过 | parsed；最终运行 `retry_count=0`；no-tools、single-shot |
| delegation shape | 通过 | 1 个 operation，类型为 `create_node`；execution node=1，immediate node=1 |
| typed contract | 通过 | contract 覆盖 1/1 |
| decomposition | 通过 | 未提供 decomposition；单 primary node 不需要拆分理由 |
| validator | 通过 | `accepted`、`would_apply=true` |
| no-apply boundary | 通过 | graph revision=0；graph patches=0；kernel decisions=0 |
| consistency | 带 warning 通过 | 0 violations、1 个 `illegal_idle` warning；execute-only smoke 不持久化 pending decision，job 仍等待实际 apply/operator action |
| credential/config | 通过 | 隔离目录 credential scan 0 命中；最终真实调用前后 `.codex/config.toml` 与 `auth.json` 哈希不变 |

本次结果证明 Profile v2 和 local validator 能在真实模型输出上实现 primary-node-first。它不
证明 OAuth 功能被实现，不启动真实 worker，也不证明大 primary node 的长期执行、checkpoint
或 crash recovery。`illegal_idle` warning 暴露的是 execute-only smoke 的非持久化等待语义，
不应被误写成零 warning；后续应在 liveness/observability 语义中单独处理。

其中 OAuth 只是历史 validate-without-apply 的假想任务文本，从未 apply、从未启动 worker、
从未实现任何 OAuth 代码。后续真实 worker 验证已改用下节的中性文本统计 CLI。

### 2026-07-10 Delegation Initialization isolated single-worker smoke

运行代码：

- `6313a58 feat(kanban): initialize runtime through decision provider`；
- `1f21e5c fix(kanban): validate canonical worker write scopes`；
- `5e4c2df test(kanban): report worker materialization attempts`；
- `286c5cb fix(kanban): extract runtime receipt from truncated tails`。

环境：全新隔离 `HERMES_HOME`、独立 Git workspace、专属 `codex-delegation-init` lane；
worker 使用隔离 `CODEX_HOME` 配置与认证副本；主 `.codex` 仅由 Decision Provider bridge
读取。脱敏模型标识为 `codex:MySub2api` / `gpt-5.6-sol`。

真实任务是在临时 Python 仓库中由一个连续 worker 实现文本统计 CLI，覆盖文件输入、标准
输入、unittest 和必要 debug。该任务不涉及 OAuth、网络、外部账户、凭证、部署或 Git
提交。

最终通过结果：

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| initialization | 通过 | state=`waiting_decision`；graph revision=0；0 nodes；`legal_wait=true`；`decision_requested=true` |
| real decision | 通过 | 1 tick；1 accepted patch；0 rejected patch；Profile v2 创建 `implement-text-stats-cli` |
| node contract | 通过 | 1 个 `implementation` node；typed contract；`declared_write_scope=["**"]`；无 decomposition |
| materialization | 通过 | 1 个 node、attempt-1、1 个 dispatcher-started Codex worker；`single_worker_attempt=true` |
| worker result | 通过 | receipt verdict=`pass`；临时 workspace 独立复跑 6 项 unittest 全部通过 |
| ledger / completion | 通过 | satisfaction=`full`、verification=`verified`、confidence=1.0；job=`done` |
| consistency | 通过 | 0 violations、0 warnings |
| credential/config | 通过 | HERMES_HOME/workspace 11 个文件 credential scan 0 命中；最终 run 前后主 `config.toml` / `auth.json` 哈希不变；Codex CLI 只改写隔离 `CODEX_HOME` 副本 |

达到最终结果前的隔离运行保留了三类有效失败证据：

- provider 曾用 `repository/**` 表示 whole workspace，worker 成功后被 scope check 正确标为
  failed；因此 validator 和 Profile 现在要求使用 canonical `**`；
- 180 秒 lane timeout 曾触发 `worker_timed_out -> receipt_invalid -> retry`，证明 recovery
  attempt history 生效，但该 run 不计入单 attempt 验收；
- 一个 exit-0 worker 输出包含合法 runtime envelope，却因 8 KB tail 中残留旧 closing fence
  被 parser 漏掉；修复后最终 run 在 attempt-1 正确提取 receipt。

本次结果证明 provider-first initialization 与单 coherent worker 的真实闭环成立。它仍不
证明多 worker 并发、persistent worker session、长任务 checkpoint/crash resume、backend
路径级 sandbox；真实 compaction L3 quality 后续由 Phase 4G5 单独验证。

### 2026-07-10 Phase 4G4 isolated worker continuity smoke

环境：全新隔离 `HERMES_HOME`、独立 Git workspace、隔离 `CODEX_HOME` 配置与认证副本；
专属 `codex-continuity-real` lane，`max_concurrency=1`。测试没有调用 Decision Provider，
而是通过 local validator apply 一个明确的单 primary node，用于只验证 worker execution
continuity，不把 delegation/model decomposition 质量混入本次结果。

真实任务先创建并精确验证 `partial.txt`，然后等待 runtime 在 `.git` 内创建 continuation
signal；attempt-1 由 30 秒 lane timeout 正常结束。runtime reconcile 将 task/run timeout
投影为 materialization 与 backend session 的 interrupted fact。attempt-2 在相同 workspace、
lane、capability fingerprint 和 node-contract fingerprint 下，通过
`codex exec resume <session-id>` 恢复同一 session，创建并精确验证两行 `result.txt`，最后
输出合法 `runtime_worker_receipt_v1`。

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| attempt-1 | 通过 | mode=`fresh`；status=`timed_out`；session ID 已从 `thread.started` 投影 |
| recovery | 通过 | `worker_run_timeout`；node 回到 ready；旧 task/run/materialization terminal fact 保留 |
| eligibility | 通过 | workspace/lane/capability/contract/revision 全部匹配；未发生权限扩大 |
| attempt-2 | 通过 | mode=`resume`；同一 backend session；status=`succeeded`；`resume_count=1` |
| context reuse | 通过 | context reacquisition=0；成功 resume turn 有显著 cached input |
| receipt / ledger | 通过 | receipt verdict=`pass`；ledger=`full/verified`；confidence=1.0 |
| completion | 通过 | node=`succeeded`；job=`done` |
| consistency | 通过 | 0 violations、0 warnings |
| isolation | 通过 | 主 `.codex/config.toml`、`auth.json` 哈希不变；credential scan 0 命中 |

首次真实尝试还暴露了一个有价值的本地 race：resume turn 已经输出完整 artifact、verification、
receipt 和 `turn.completed`，wrapper 却在 Codex 进程正常退出前先触发 wall timeout。实现现已在
terminal event 后提供有界退出宽限，并增加 deterministic regression；最终隔离运行通过。

本次结果证明单 node、单 backend session、两次 materialization 的 timeout/resume L5 路径。
它仍不证明 paused worker、跨机器 session migration、internal subagent session 观测、任意
长任务多次 resume soak 或路径级 sandbox；真实 compaction L3 quality 后续由 Phase 4G5
单独验证。

### 2026-07-10 Phase 4G5 isolated real compaction candidate quality

运行代码：Phase 4G5 实现提交（本记录随提交落地）。

环境：全新隔离 `HERMES_HOME`、独立 Git workspace、隔离 `CODEX_HOME` 配置与认证副本；
未启动 worker。主 `.codex` 只用于生成只读副本和前后哈希校验。

脱敏模型标识：`codex:MySub2api` / `gpt-5.6-sol`。

测试 job 使用 production 初始化，状态为 `waiting_decision`，包含一个 required goal item、
两个 open gap 和空 graph。真实 compaction 使用 `max_retries=0`、180 秒 timeout 和
`fallback_to_deterministic=false`。

| 步骤 | 结果 | 事实 |
| --- | --- | --- |
| provider input | 通过 | 含 bounded provenance catalog、checkpoint fact schema 和逐项 `source_refs` 约束 |
| provider output | 通过 | 首次 response parsed；未进入 parse retry 或 validator-aware repair |
| provenance | 通过 | 2 个 `open_goal_gaps` 和 1 个 `open_blocker` 均自带 catalog 中存在的 `gap_key` 引用 |
| checkpoint validator | 通过 | `provider_validation.status=accepted`，checkpoint `validator_status=accepted` |
| fallback | 未使用 | `fallback_used=false`，没有 deterministic provider 接管 |
| segment lifecycle | 通过 | source segment=`compacted`；新 segment=`active`；checkpoint ref 一致 |
| consistency | 通过 | 0 violations、0 warnings |
| isolation | 通过 | runtime DB/workspace credential scan 0 命中；主与隔离 `.codex` 文件哈希一致 |
| offline regression | 通过 | Runtime Kernel / CLI 239 项通过；Runtime observability API 定向测试 1 项通过 |

本次结果证明真实 compaction candidate 在 parser 不补 provenance、fallback 禁用的条件下达到
L3。该结论限于单 provider、单 profile、单次 compaction，不覆盖真实多轮长任务 compaction
soak。

### 2026-07-11 Phase 4G6 active long-run 与 real compaction multi-cycle

运行代码：Phase 4G6 实现提交（本记录随提交落地）。

Deterministic 环境使用 production provider-first initialization 和 synthetic worker
receipt，但所有状态变化均经过 production supervisor、patch validator、materialization、
evidence ingest、ledger、compaction 和 consistency 路径。

| 场景 | 结果 | 事实 |
| --- | --- | --- |
| active ticks | 通过 | 62 active、2 incidental noop、0 terminal padding |
| graph/decision | 通过 | graph revision 25；27 decisions；25 applied、2 rejected |
| worker lifecycle | 通过 | 25 coherent nodes；26 attempts；1 crash/retry；无 fixture node |
| compaction | 通过 | 7 attempts；6 accepted checkpoints；三个 profile |
| quality health | 通过 | fallback streak 2 产生 degraded；provider accepted 后 recovered |
| no-fallback rejection | 通过 | invalid provenance candidate rejected；source segment preserved |
| goal reopen | 通过 | temporary satisfied -> contradicted/reopened -> later verified/done |
| context isolation | 通过 | 7 个 historical sentinel 均不进入最新 provider input |
| restart validation | 通过 | fresh DB connection 检查 6-checkpoint chain valid |
| consistency | 通过 | 0 violations、0 warnings |
| offline regression | 通过 | Runtime/CLI 247 项；Runtime observability API 定向测试 1 项 |

真实环境使用全新隔离 `HERMES_HOME`、Git workspace 和 `CODEX_HOME` 配置/认证副本；未启动
worker。脱敏模型标识：`codex:MySub2api` / `gpt-5.6-sol`。

`real-compaction-soak` 连续执行 `token_budget_compaction`、
`validator_boundary_compaction` 和 `anti_stuck_compaction`。三轮全部 parsed、provider
validation accepted、`fallback_used=false`，每轮均完成 segment rollover 和 context chain
validation。最终 3 个 compacted segment、1 个 active segment，health healthy，fresh process
chain valid，consistency 0/0，credential scan 0 命中，主 `.codex` 哈希不变。

该结果证明单 provider 下的 bounded real multi-cycle compaction。它仍不等于数小时真实 worker
长跑、多 provider soak 或 daemon restart 验收。

## 6. 结果记录模板

每次新运行追加一条记录，格式如下：

```text
日期：
代码 commit：
验证等级：L1 / L2 / L3 / L4 / L5
环境：isolated HERMES_HOME=true，真实 worker=false/true
provider alias：
model：
场景：
decision 结果：parsed / provider_error / parse_failed / validator accepted/rejected
apply 结果：applied / rejected / skipped
compaction 结果：accepted / fallback / rejected / not_run
consistency：passed / warning / failed
credential scan：passed / failed
结论：
后续动作：
```

禁止在台账、CLI summary 或 event summary 中记录 API key、完整 base URL、完整 prompt、
完整 raw response、私有 artifact 内容。隔离 DB 中的 decision segment 归档遵循上一节
的 credential scan 和访问边界。

## 7. Phase 4G2 当时进入门槛

进入真实 provider bounded loop 前，至少需要：

- L1 和 L2 在当前 provider/model 上可重复完成；
- 至少一次 accepted patch 与至少一次 rejected patch 都有完整 audit；
- 真实 compaction fallback 路径通过；
- consistency checker 无未解释 violation；
- credential scan 通过；
- 当时已知的 compaction provenance 缺口被记录为风险，而不是误标为 L3 通过。

Phase 4G2 的目标不是立刻要求 L3，因此当时允许使用 deterministic compaction fallback，
同时收集真实 compaction candidate 的失败类型。该历史进入策略没有把 fallback 误写成
candidate quality；Phase 4G5 后续已独立达到单次 candidate L3。

## 8. Phase 4G3 进入门槛

在接真实 worker lane 前，必须完成至少一轮 L4，并确认：

- 真实 provider patch 在多轮 feedback 下不会绕过 validator；
- synthetic worker evidence 能推动 ledger、gap、liveness 和 consistency；
- capability block / human gate 不造成 silent idle；
- compaction fallback 或 rejection 不会污染 active decision session。

真实 worker lane smoke 仍必须使用独立 task/run，且只管理由 Kanban 启动的 worker
process，不能影响用户自己的 Codex 会话。

## 9. 关联文档

- `docs/kanban-runtime-kernel-phase3.md`：真实 decision provider 接入边界；
- `docs/kanban-runtime-kernel-phase4.md`：真实 compaction provider 与 fallback 边界；
- `docs/kanban-runtime-kernel-phase4g.md`：deterministic synthetic long-run baseline；
- `docs/kanban-runtime-kernel-phase4g1.md`：真实模型源 smoke runbook；
- `docs/kanban-runtime-kernel-phase4g2.md`：真实 decision provider bounded loop；
- `docs/kanban-runtime-kernel-worker-execution-continuity.md`：Phase 4G4 worker session resume；
- `docs/kanban-runtime-kernel-phase4g5.md`：真实 compaction candidate provenance 与 L3；
- `docs/kanban-runtime-kernel-phase4g6.md`：active long-run、compaction health 与 context chain；
- `docs/kanban-runtime-kernel-roadmap.md`：4G1 至 4G6 演进顺序。
