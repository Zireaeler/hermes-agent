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
- `.codex` 只允许读取，不能修改 `config.toml` 或 `auth.json`；
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
| real compaction candidate quality | Phase 4A / 4G1 | 未通过 | 至少一次真实 candidate 无 fallback 通过 checkpoint validator | 未通过，当前 candidate 缺 provenance |
| real provider bounded loop | Phase 4G2 | 已验证 | 3-5 decision ticks + synthetic worker evidence + consistency passed | 通过：3 ticks、2 applied、1 rejected、synthetic receipt `failed -> succeeded`、最终 done |
| real worker lane smoke | Phase 4G3 | 已验证 | 真实 worker receipt 进入 runtime ingest，端到端 consistency passed | 通过：两个 dispatcher-started Codex worker receipt 写入 verified ledger，job done |

“real compaction transport + fallback 通过”不等于“真实 compaction 质量通过”。如果模型
candidate 被 validator 拒绝，fallback 成功只能证明安全边界和恢复路径正确。

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
candidate quality L3；后者仍受 `open_goal_gaps` provenance 缺失限制。

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
review/test workflow、worker crash long-run recovery 或真实 compaction candidate quality。

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

## 7. Phase 4G2 进入门槛

进入真实 provider bounded loop 前，至少需要：

- L1 和 L2 在当前 provider/model 上可重复完成；
- 至少一次 accepted patch 与至少一次 rejected patch 都有完整 audit；
- 真实 compaction fallback 路径通过；
- consistency checker 无未解释 violation；
- credential scan 通过；
- 当前已知的 compaction provenance 缺口被记录为风险，而不是误标为 L3 通过。

Phase 4G2 的目标不是立刻要求 L3。它可以先使用 deterministic compaction fallback，
同时收集真实 compaction candidate 的失败类型。达到 L3 前，不应把真实 compaction
质量作为生产可靠能力宣称。

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
- `docs/kanban-runtime-kernel-roadmap.md`：4G1 / 4G2 / 4G3 演进顺序。
