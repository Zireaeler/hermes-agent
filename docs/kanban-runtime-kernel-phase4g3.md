# Hermes Kanban Runtime Kernel Phase 4G3：真实 Worker Lane Smoke

Phase 4G3 是 Runtime Kernel 的 L5 集成验证阶段。它在 Phase 4G2 已证明真实 decision
provider 与 synthetic worker evidence 可以多轮协作之后，首次把 synthetic receipt 替换为
由 Hermes Kanban dispatcher 启动的真实 worker lane receipt。

本阶段验证的是受控端到端边界，不是长任务生产验收：真实模型只能 proposal，真实 worker
只执行一个 runtime node，DB、validator、reducer 和 progress ledger 仍是唯一的事实与状态
裁决方。

## 1. 目标

Phase 4G3 MVP 必须验证如下路径：

```text
real decision provider
        |
        v
parser / graph patch validator
        |
        v
execution node ready
        |
        v
runtime materialization -> Kanban task/run
        |
        v
dispatcher -> Hermes-started Codex CLI worker process
        |
        v
runtime receipt envelope -> task evidence
        |
        v
runtime ingest -> ledger / reducer / consistency
```

验收重点：

- 真实 provider 仍只产生 patch proposal；
- worker 由 `dispatch_once()` 和已注册的 `codex_cli` lane 启动，而非 smoke helper 直接伪造
  task completion；
- worker receipt 经过结构化解析后才可进入 runtime ingest；
- 进程 exit code 为 0 不能自动等价为 node 成功或 goal 满足；
- runtime ingest 只能基于 receipt 中显式、可关联的 goal item 和 verification 写入 ledger；
- task/run/materialization/node/event/ledger/job state 必须通过 consistency checker；
- 所有运行都在独立 `HERMES_HOME`、独立 workspace 和独立 task/run 中进行。

## 2. 非目标

Phase 4G3 不运行多 worker 并发，不运行真实 review/test workflow，不验证真实代码仓库的
复杂变更，也不替代长期 soak。

它不允许通过 `complete_task()` 直接写 synthetic metadata 来冒充真实 worker evidence；测试中
可以用 fake Codex binary 验证 wrapper，但真实 L5 必须启动由 dispatcher 创建的 Codex worker
wrapper 和其子进程。

它不把 worker 的内部 context compression、完整 stdout、完整 prompt 或完整 raw model
response 写入报告或验证台账。

## 3. Runtime Receipt Envelope

现有 `codex_cli` lane 的通用 Markdown receipt 服务于 Kanban review。它包含 verdict、进度、
变更文件和 verification 段，但不天然携带 runtime goal linkage。因此 runtime node 必须在
worker prompt 中额外要求最终输出一个独立 JSON envelope：

```json
{
  "schema": "runtime_worker_receipt_v1",
  "verdict": "pass",
  "summary": "完成受限 smoke 任务并验证结果。",
  "claimed_goal_items": ["worker-smoke-result"],
  "unmet_goal_items": [],
  "verification": {
    "passed": true,
    "summary": "python3 verification command passed"
  },
  "artifacts": []
}
```

envelope 必须位于最终输出的 fenced `json` code block 中，并带 `schema`。解析器只接受与当前
runtime node footer 的 `goal_item_keys` 相交的 claimed/partial/unmet item；未知 item、非法
schema、缺少 verdict 或 verification 均视为无效 receipt。

若 Codex 只返回普通 Markdown receipt、exit 0 但缺失 envelope，task 仍会结束或进入现有
review policy，但 runtime reconcile 必须生成 `receipt_missing` 或 `receipt_invalid`，而不能把
node 或 job 标记成功。

## 4. 隔离与安全

真实运行必须：

- 使用新建临时 `HERMES_HOME`、Kanban DB 和 Git workspace；
- 使用专属 lane 名称和仅一个 task/run，`max_concurrency=1`；
- 通过现有 `.codex` 配置读取模型源，但不得写入 `.codex`；
- 仅等待、检查和在必要时终止由该 smoke task 的 dispatcher PID 创建的 worker；
- 设置严格 timeout；超时、crash、receipt missing 必须走 runtime recovery/reconcile，不得
  通过测试脚本覆盖状态；
- 只在临时 workspace 创建一个 smoke artifact，不修改 Hermes checkout；
- 扫描隔离 DB、报告和日志摘要，确保 API key 不存在。

worker 的 capability policy 继续生效。MVP smoke 使用不需要网络、凭证、workspace 外写入、
Git 提交或付费 API 的 node；Codex lane 使用 `workspace-write` 和 `approval=never`，但任务
正文必须限制为临时 workspace 内的单一文件与本地验证。

## 5. 实现范围

新增一个 bounded worker-lane smoke runner 与 CLI。它应复用：

- `RuntimeDecisionProvider` 与 `advance_runtime_job()`；
- `materialize_runtime_node()`；
- `kanban_db.dispatch_once()`；
- 已有 `codex_cli` worker lane 与 task/run/heartbeat/evidence 机制；
- `ingest_runtime_node_evidence()`、`reconcile_runtime_materializations()` 与
  `check_runtime_consistency()`。

runner 只做 orchestration、轮询和脱敏 report，不复制 dispatcher 或 worker lifecycle。真实
运行最多进行少量 decision 尝试和一个 worker node；若真实 proposal 未被 validator 接受，报告
应返回 `waiting_decision` 或预算耗尽，而不是注入 fallback graph patch 伪造 L5 成功。

decision provider input 应携带当前已注册 worker lane 的名称、类型、说明和并发上限，作为
`available_execution_backends`。它只是 provider 选择 assignee 的 execution constraint，
不授予 capability、不替代 lane physical capability 检查，也不允许 provider 创建未注册 lane。

为保持离线测试，fake-real provider 与测试用 Codex binary 可替代网络调用，但它们必须穿过
真实 worker wrapper、task/run 和 receipt parser，不能直接调用 runtime ingest。

## 6. 验收标准

Phase 4G3 MVP 完成时：

- 默认 pytest 离线覆盖 runtime receipt parser、未知 goal item 拒绝、缺 receipt recovery、
  fake provider + fake Codex binary 的 dispatcher 到 ingest 全链路；
- CLI 必须要求显式模型源和显式真实 worker opt-in；
- 一次隔离 L5 运行实际启动 Hermes 创建的 Codex worker wrapper；
- 至少一个真实 provider patch 经 validator apply，至少一个 worker receipt 经 parser 和
  ingest 更新 node/materialization/ledger；
- worker 成功进程但无 receipt envelope 的路径不会误完成 job；
- consistency 无未解释 violation，credential scan 通过；
- 结果以脱敏形式追加到
  `docs/kanban-runtime-kernel-real-integration-validation.md`。

## 7. 后续关系

G3 通过只证明单 worker 的真实端到端边界，不证明多 worker、review/test 交接或长期恢复。
后续 worker long-run/soak 应在本阶段的 receipt contract 和隔离规则之上扩展。

## 8. 当前真实验证结果

2026-07-10 已在新的隔离 `HERMES_HOME`、独立 Git workspace 和专属
`codex-runtime-smoke` lane 中完成 L5。运行使用当前 `.codex` 模型源，只启动由该
smoke task materialize 后经 dispatcher 创建的 Codex worker wrapper。

第一个真实 worker 返回 `scope-understood` 的 `runtime_worker_receipt_v1`；真实 decision
provider 的 proposal 经 validator apply 后创建 `worker-smoke-result`。provider 没有显式
填写 assignee 时，materialization 使用 job 创建时保存的默认 worker lane，仍经过 lane 与
capability 检查。第二个真实 worker 在隔离 workspace 写入并验证
`runtime_worker_smoke.txt`，其 receipt 满足 `worker-smoke-result`。

最终两个 node 均为 `succeeded`，required goal items 均由 verified ledger evidence 满足，job
为 `done`；consistency 为 `passed`，0 violations、0 warnings；完整隔离 DB credential scan
通过。此结果是单 worker L5 smoke，不等价于多 worker 长任务、review/test 交接或真实
compaction quality 的生产验证。完整脱敏记录见
`docs/kanban-runtime-kernel-real-integration-validation.md`。

## 9. Delegation Initialization 后续验证

2026-07-10 的后续 smoke 已移除旧 `understand-scope -> implementation` 两节点路径。
Production job 从空 graph 进入 `waiting_decision`，真实 provider 创建一个带 typed contract 的
`implementation` primary node；dispatcher 只 materialize attempt-1，一个真实 Codex worker
在同一 session 内完成文本统计 CLI 的 inspection、implementation、unittest、debug 和 local
verification。

最终 1 次 decision、1 个 patch、1 个 node、1 个 materialization attempt、1 个 runtime
receipt；ledger full/verified，job `done`，consistency 0/0。worker 使用隔离 `CODEX_HOME`，
主 `.codex` 哈希保持不变。该 follow-up 取代旧两节点 fixture 作为当前 delegation worker
smoke 基线，但仍不是长任务或 persistent session 验收。
