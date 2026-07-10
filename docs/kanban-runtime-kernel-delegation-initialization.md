# Hermes Kanban Runtime Kernel Delegation Initialization

## 1. 目的

本文定义 Worker Delegation Policy 从 job 创建开始生效的初始化闭环。

Delegation Policy Enforcement MVP 已约束 Decision Provider：默认创建一个具备完整 outcome、
验收条件和证据要求的 coherent primary worker node，不得按 analysis、research、implementation、
testing 或 debugging 阶段进行 speculative decomposition。

但旧 production 初始化仍会无条件创建 `understand-scope` analysis node。该行为会在第一次真实
decision 之前制造一个细粒度 worker session，与 primary-node-first 原则冲突。本阶段负责移除
这项 production 依赖，同时保留 deterministic fixture 的显式兼容入口。

## 2. 核心状态流

Production job 的初始路径必须是：

```text
create/promote runtime job
        |
        v
goal contract + decision session
        |
        v
waiting_decision / initial_graph_required
        |
        v
Decision Provider proposal
        |
        v
validator
        |
        v
one coherent primary worker node
```

job 创建本身不得隐式调用模型。CLI、supervisor 或显式 `runtime advance --provider real` 决定
何时消费 durable decision request。没有 provider 时，job 保持可恢复的 `waiting_decision`，
不能回退创建 `understand-scope`。

## 3. Initialization Mode

`create_runtime_job()` 支持两个显式模式：

- `provider_first`：production 默认；不创建 execution node，写入初始 decision request；
- `fixture`：仅供 deterministic test、synthetic soak 和兼容场景；创建旧
  `understand-scope` node。

`create_runtime_job_from_objective()`、`promote_runtime_job()` 和 CLI `runtime create/promote`
必须使用 `provider_first`。直接调用底层 helper 若需要旧行为，必须显式传入 `fixture`，避免
新代码无意继承旧 workflow。

初始化模式写入 `runtime_jobs.metadata_json.initialization_mode`，用于审计、兼容和 validator
策略判断。它不是 provider 可修改字段。

## 4. Initial Decision Request

`provider_first` job 创建时必须持久化：

- job state：`waiting_decision`；
- decision profile：`graph_patch_decision`；
- event：`decision_requested`；
- request key：`initial_graph_required`；
- payload：goal item keys、当前 graph revision、初始化原因。

初始 graph revision 保持 0。Decision Provider 返回的 patch 仍必须经过 parser、typed node
contract validator、goal/gap linkage、capability policy 和 graph patch audit。初始化模式不能成为
绕过 validator 的特殊路径。

第一份 accepted patch 在没有结构性边界时只能创建一个 immediate execution node。模型返回
多个 execution node、独立 verifier 或并行 lane 时，仍必须提供合法 `decomposition`。

## 5. Waiting Decision 与 Liveness

`waiting_decision` 是一等合法等待状态。以下情况不能被标为 `illegal_idle`：

- job state 为 `waiting_decision`；
- goal 仍有 open gap；
- 当前没有 ready/running worker；
- 初始或后续 decision request 已由 reducer 持久化。

合法等待不代表可以永久静默。supervisor 可以根据 decision request age、provider unavailable、
lease、retry/backoff 或 operator policy 产生 `provider_unavailable`、
`operator_attention_required` 或 anti-stuck event。但 consistency checker 不能仅因为 provider
尚未开始调用就把正常 `waiting_decision` 判成 liveness violation。

Observability 至少应区分：

- `legal_wait=true`；
- `legal_waiting_reason=waiting_decision`；
- `pending_decision`：是否已有 started `kernel_decision`；
- `decision_requested`：是否处于 reducer 请求决策的状态。

## 6. Decomposition Expansion Predicate

Production job 中，当 patch 新建一个无 dependency 的 immediate execution node，且 graph 已有
任一 nonterminal execution node 时，必须提供 `decomposition`。nonterminal 状态包括：

- `planned`；
- `waiting_dependency`；
- `ready`；
- `running`。

这用于防止 Decision Provider 在已有责任仍活跃时静默增加第二个并行 worker。若新 node 明确
依赖已有 node，则不属于 immediate parallel expansion；独立 verifier 仍遵循固定 target 和
decomposition 规则。

旧 fixture job 保持兼容：其 `understand-scope` ready node 不应导致全部历史 patch fixture
突然需要 decomposition；fixture 仍只在已有 running node 时启用旧并行检查。该兼容分支不得
被 CLI production create/promote 使用。

## 7. Typed Primary Node

初始 Decision Provider patch 创建的 primary node 必须包含：

- `goal_item_keys` 或真实 `gap_keys`；
- `contract.outcome`；
- `contract.acceptance_criteria`；
- `contract.success_evidence`；
- `contract.declared_write_scope`；
- `contract.prohibited_actions`；
- 所需 `requested_capabilities`，若存在。

node 的责任可以包含 inspection、必要 research、local planning、implementation、testing、
debugging 和 local verification。它不应只负责“理解范围”或“编写计划”。

`declared_write_scope` 使用 workspace-relative canonical glob。整个 workspace 写为 `**`；
局部范围写为 `src/**`、`tests/**` 或具体文件。`repository/**`、`workspace/**`、绝对路径和
包含 `..` 的 scope 必须由 validator 拒绝，避免 worker 成功后才发现 scope 表达无法匹配。

## 8. 真实验证

真实 smoke 使用一次性 `HERMES_HOME`、独立 Git workspace、专属 Codex lane 和当前只读
`.codex` 模型源。测试目标必须是中性的小型仓库功能，不使用 OAuth、部署、凭证或外部账户
场景。

建议目标：在临时 Python 仓库中实现并验证一个文本统计 CLI。一个真实 worker 应在同一
session 内完成仓库检查、实现、测试、必要 debug 和最终 receipt。

验收要求：

- 初始 job 没有 `understand-scope` node；
- 第一次真实 decision 创建一个带 typed contract 的 primary node；
- 不创建独立 analysis/research/test/debug node；
- provider patch 经 validator apply；
- dispatcher 只启动一个 Hermes-owned worker；
- worker receipt 经 parser/ingest 满足 goal item；
- job 最终 `done`，consistency 无 violation；
- `.codex` 文件调用前后哈希不变，隔离 DB/report/log 无 API key。

## 9. 非目标

本阶段不实现：

- worker 内部 subagent 管理；
- persistent worker session checkpoint；
- 多 worker 并发；
- backend 路径级 sandbox；
- dashboard UI；
- OAuth 或其他业务认证功能。

## 10. 完成标准

本阶段完成时：

- production create/promote 默认进入 `provider_first`；
- fixture 模式只能显式启用；
- waiting decision 是合法、可观测、可恢复状态；
- production decomposition predicate 覆盖全部 nonterminal execution node；
- deterministic tests 覆盖初始化、兼容、liveness、validator 和 CLI；
- 一次隔离真实 provider + 单 worker 中性功能 smoke 通过；
- roadmap 和真实验证台账记录准确，改动已提交并推送。

## 11. 当前实现与验证结果

截至 2026-07-10，本阶段 MVP 已完成：

- `create_runtime_job()` 默认使用 `provider_first`；production create/promote 不创建
  `understand-scope`；deterministic fixture 必须显式声明 `fixture`；
- 初始 job 写入 `initial_graph_required` decision request，并以 `waiting_decision`、
  `legal_wait=true`、`decision_requested=true` 暴露；
- 正常 waiting decision 不再写入矛盾的 `liveness_violation`；
- provider-first job 强制 typed node contract；
- nonterminal expansion predicate 已覆盖 `planned`、`waiting_dependency`、`ready` 和
  `running`；
- write scope 已强制使用 canonical workspace-relative glob；
- worker-smoke report 已一等记录 materialization attempts、single primary node 和 single
  worker attempt；
- runtime receipt parser 已修复 8 KB tail 从旧 closing fence 中间截断时无法识别最终
  `json` envelope 的问题。

Deterministic runtime 组合回归为 `284 passed, 1 deselected`；runtime observability API 定向
测试为 `1 passed`。被排除项仍是既有 live-system guard 对 fake timeout `SIGTERM` 的拦截。

最终隔离真实运行使用中性文本统计 CLI：1 次真实 decision、1 个 accepted patch、1 个
`implementation` primary node、1 次 materialization、1 个真实 Codex worker receipt；临时
workspace 的 6 项 unittest 通过，ledger 为 full/verified，job 为 `done`，consistency 为
0 violations / 0 warnings。完整脱敏事实见
`docs/kanban-runtime-kernel-real-integration-validation.md`。
