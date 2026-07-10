# Hermes Kanban Runtime Kernel Phase 4G4：Worker 执行连续性

Phase 4G4 在 Delegation Initialization 和真实单 worker L5 smoke 之后补齐 primary worker
node 的执行连续性。Delegation Policy 已允许一个具备适当能力的 worker 在同一个 node 内完成
inspection、implementation、testing、debugging 和 verification，但当前 `codex_cli` backend
仍把每个 materialization attempt 当成一次全新的 Codex invocation。worker timeout、crash 或
进程重启后，即使 workspace 中保留了修改，下一次 attempt 仍需要重新读取仓库并重建上下文。

本阶段不改变 Runtime Kernel 的事实边界。DB、validator、reducer、goal contract 和 progress
ledger 仍是权威状态；Codex session 只是一个可恢复的 backend 执行上下文，不能声明 node 或
goal 完成。

## 1. 目标

Phase 4G4 MVP 建立以下连续性链路：

```text
durable runtime node
        |
        v
materialization attempt-1
        |
        v
Codex backend session / thread
        |
        v
timeout or crash
        |
        v
runtime reconcile + resume eligibility validation
        |
        v
materialization attempt-2
        |
        v
codex exec resume <session-id>
        |
        v
terminal receipt -> runtime ingest
```

必须实现：

- 区分 durable runtime node、materialization attempt 和 backend worker session；
- 从 Codex JSONL `thread.started` 事件提取 backend session ID；
- 通过本地 reducer 把 Kanban task event 投影为 runtime backend-session 事实；
- timeout、crash 或 stale recovery 后优先恢复同一 node 的合法 session；
- resume 前验证 workspace、worker lane、capability envelope、node contract 和 workspace
  revision；
- resume 不可用或校验不通过时显式降级为 fresh attempt，并记录 context reacquisition；
- observability 能区分 `fresh`、`resume_pending`、`resuming`、`resumed`、
  `resume_failed` 和 `fallback_fresh`；
- deterministic fake backend 和隔离真实 Codex smoke 都验证中断后的恢复行为。

## 2. 三层执行对象

### 2.1 Runtime Node

Runtime node 是 durable execution responsibility。它拥有 objective、typed contract、goal/gap
linkage、capability request 和 terminal fact。node 的 terminal fact 不得因 session resume 而
静默改写。

### 2.2 Materialization Attempt

Materialization attempt 是 node 到 Kanban task/run 的一次物化。每次 infra retry 都创建新的
attempt、task 和 run，保留旧 attempt 的 timeout/crash/stale 事实。resume 不复用旧 task/run，
只复用 backend session 上下文。

### 2.3 Backend Worker Session

Backend worker session 是 worker backend 自己保存的执行上下文。对 Codex CLI，它对应
`thread.started.thread_id` 以及隔离 `CODEX_HOME` 中的 session 文件。一个 backend session
可以跨多个 materialization attempts，但只能属于一个 runtime node。

Backend session 不是 Decision Session。两者禁止复用 ID、表、checkpoint 或 compaction
语义：

```text
Decision Session = runtime 调度推理连续性
Backend Session  = 单个 worker node 的执行连续性
```

## 3. 数据模型

新增 `backend_worker_sessions` 投影表，至少保存：

- runtime session record ID；
- `job_id`、`node_id`；
- backend kind 和 backend session key；
- initial/latest materialization ID；
- worker lane 和 workspace path；
- capability fingerprint 和 node-contract fingerprint；
- interruption 时的 workspace revision；
- bounded checkpoint metadata；
- status、resume count、heartbeat、created/updated/completed time；
- 不包含 API key、完整 prompt、完整 transcript 或隐藏模型推理。

`node_materializations.metadata_json` 保存本 attempt 的连续性决策：

```json
{
  "execution_continuity": {
    "mode": "resume",
    "backend_session_record_id": "bws_xxx",
    "resume_session_id": "opaque-backend-session-id",
    "resume_from_materialization_id": "mat_xxx",
    "eligibility": "accepted",
    "context_reacquisition": false
  }
}
```

首次执行为 `fresh`。无法恢复时使用 `fallback_fresh`，同时记录拒绝原因和
`context_reacquisition=true`。

## 4. Write Path 与投影边界

Codex worker wrapper 继续只写 Kanban task events，不直接修改 runtime graph、node、ledger 或
backend-session 投影表。

首次 JSONL `thread.started` 产生：

```text
worker_backend_session_started
```

heartbeat、timeout、crash、completion 继续写 Kanban task/run facts。Runtime supervisor tick
中的本地 reducer 读取这些 task events，幂等 upsert `backend_worker_sessions`，再执行
materialization reconcile。

这样可以保证：

- backend adapter 只报告事实；
- runtime 决定 session 是否可恢复；
- provider 不参与 resume authorization；
- event replay 可以解释每次 fresh/resume/fallback 决策。

## 5. Resume Eligibility

只有以下条件全部满足才允许 resume：

1. prior attempt 因 infra timeout、crash 或 stale 结束；
2. backend 明确支持 non-interactive resume；
3. backend session ID 已持久化且未完成、未失效；
4. runtime node 仍为 nonterminal，且仍是同一个 node；
5. workspace canonical path 与 prior session 一致；
6. interruption checkpoint 的 workspace revision 与当前 workspace 一致；
7. worker lane/backend kind 相同；
8. capability fingerprint 相同；
9. node-contract fingerprint 相同；
10. session 没有超过 resume 次数预算。

MVP 不允许 human authorization、capability policy 或 node contract 在 resume 时隐式扩大。
有效授权发生变化后，旧 session 默认不再可恢复；runtime 创建 fresh attempt 或等待 human，
不能把新权限注入旧 session 后继续。

## 6. Codex CLI Adapter

当前 Codex CLI `0.144.1` 支持：

```text
codex exec resume <SESSION_ID> <PROMPT> --json
```

Runtime node 的首次 Codex 调用必须启用 JSON events，即使 lane 没有显式设置
`json_events=true`，以便可靠获得 `thread.started.thread_id`。普通非 runtime Kanban task
保持已有配置行为。

Fresh argv 与 resume argv 都必须使用相同 workspace、sandbox、approval、model 和隔离
`CODEX_HOME`。resume prompt 不重复发送完整 node contract，只发送 bounded continuation
footer，包括：

- 当前 materialization attempt；
- prior failure type；
- runtime 要求继续满足的原 node acceptance criteria；
- 不允许把 prior partial progress 当成 terminal success；
- 仍必须输出完整 `runtime_worker_receipt_v1`。

Codex CLI 找不到 session、session 文件损坏或 resume 子进程失败时，worker 记录
`worker_backend_session_resume_failed`。MVP 不在同一个 task/run 内自动再启动 fresh Codex；
runtime reconcile 负责下一次 materialization 的 fallback 决策，避免一个 attempt 包含多个
不可审计执行身份。

## 7. Checkpoint 与 Workspace Revision

Phase 4G4 的 checkpoint 是 bounded backend execution checkpoint，不是 Decision Session
checkpoint，也不是 worker transcript summary。MVP 保存：

- backend session ID；
- latest task/run/materialization；
- latest worker progress items；
- latest heartbeat time；
- latest Codex event type；
- interruption failure type；
- workspace revision；
- resume/fallback audit metadata。

Git workspace revision 至少包含 HEAD 和 dirty status fingerprint。没有 Git 仓库时使用受限
filesystem marker。checkpoint 不保存完整 stdout、完整 prompt、API response 或 secrets。

## 8. Recovery 状态机

```text
fresh
  -> active
  -> completed

active
  -> interrupted
  -> resume_pending
  -> resuming
  -> resumed
  -> completed

resume_pending/resuming
  -> resume_failed
  -> fallback_fresh
```

Materialization attempt 仍使用已有 terminal recovery status。Backend session status 与 node
status 正交：session `interrupted` 不等于 node `failed`；只有 recovery policy 超过预算或
resume/fresh fallback 都不可继续时，reducer 才把 node 标记 failed。

## 9. Liveness 与 Observability

等待 resume 的 job 是合法 `waiting_worker`，但必须存在 ready node、active materialization
或明确的 resume decision，不能成为静默 idle。

只读 snapshot 至少新增：

- backend sessions；
- 当前 continuity mode；
- resume count；
- latest checkpoint；
- latest resume failure；
- context reacquisition count；
- fresh/resume/fallback materialization 数量。

关键 runtime events：

- `worker_session_discovered`；
- `worker_session_interrupted`；
- `worker_session_resume_scheduled`；
- `worker_session_resumed`；
- `worker_session_resume_failed`；
- `worker_session_fallback_fresh`；
- `worker_context_reacquired`。

## 10. 一致性约束

Consistency checker 至少检查：

- 一个 backend session 只能关联一个 runtime node；
- resume materialization 必须引用存在的 prior session 和 prior materialization；
- resume attempt 的 workspace/lane/capability/contract fingerprint 与 session 一致；
- completed session 不得再次 resume；
- terminal node 不得存在 active/resuming session；
- `context_reacquisition=false` 只能用于实际 resume；
- session resume count 与 materialization lineage 一致。

恢复不得改写旧 materialization 的 terminal status、task/run outcome 或 node terminal event。

## 11. MVP 非目标

Phase 4G4 MVP 不实现：

- worker 执行中途暂停并调用 Decision Provider 修改 graph；
- backend internal subagent 的独立 session 管理；
- 多机器或 remote worker session migration；
- 跨 workspace/worktree resume；
- capability 扩权后继续旧 session；
- 完整 worker transcript checkpoint；
- Dashboard 前端；
- OAuth 或任何业务认证功能。

## 12. 测试与真实 Smoke

Deterministic tests 必须覆盖：

- fresh argv 与 resume argv；
- JSONL thread ID 提取和 task event；
- backend-session reducer 幂等投影；
- timeout/crash 后合法 resume scheduling；
- workspace、lane、capability、contract mismatch 的 fallback；
- backend 不支持 resume；
- resume 子进程失败；
- resume 成功后 receipt ingest 和 node completion；
- terminal fact preservation；
- observability 和 consistency。

真实 smoke 必须使用独立 `HERMES_HOME`、workspace 和 `CODEX_HOME`。它只能终止明确由
Hermes smoke 创建的 worker PID，不能操作用户已有 Codex session。场景为中性本地任务：

1. attempt-1 启动 Codex 并获得 session ID；
2. worker 写入可验证的 partial artifact 后由 lane timeout 终止 Hermes-owned 子进程；
3. runtime reconcile 记录 interrupted 并创建 attempt-2；
4. smoke 在 `.git` 内写入不改变 workspace revision 的 continuation signal，attempt-2 使用
   同一 session ID 执行 `codex exec resume`；
5. worker 完成原 node，输出合法 receipt；
6. ledger/job/consistency 通过；
7. 主 `.codex` 文件哈希不变，隔离输出 credential scan 无命中。

## 13. 完成标准

Phase 4G4 MVP 完成时：

- runtime DB 能区分 node、materialization 和 backend session；
- timeout/crash 后合法 Codex session 会被优先恢复；
- 不合法或不可用 session 走可审计 fresh fallback；
- resume 不绕过 capability、contract、receipt validator 或 ledger；
- deterministic suite 和隔离真实 interruption/resume smoke 通过；
- roadmap 与真实验证台账记录实现事实；
- 改动提交并推送到 `feature-kanban-runtime-kernel`。

完成本阶段后，优先补真实 compaction candidate L3 quality；Phase 4H Dashboard Runtime UI
继续排在 execution continuity 和 compaction quality 之后。

## 14. 当前实现与验证结果

截至 2026-07-10，Phase 4G4 MVP 已完成：

- 新增 `backend_worker_sessions`，将 backend session 与 runtime node、initial/latest
  materialization、lane、workspace、capability/contract fingerprint、checkpoint、heartbeat
  和 resume count 分开持久化；
- `codex_cli` runtime task 自动启用 JSON events，从 `thread.started` 提取 session ID，并支持
  `codex exec resume`；
- worker wrapper 仍只写 Kanban task events，runtime reducer 幂等投影 session 事实；
- timeout/crash/stale 后，只有 workspace revision、lane、capability 和 contract 全部匹配才
  schedule resume；不匹配或 resume 失败时记录 `fallback_fresh` 和 context reacquisition；
- observability snapshot 已增加 backend sessions、materialization modes、resume events 和
  context reacquisition count；consistency checker 覆盖 session/materialization lineage；
- 新增 `runtime continuity-smoke` bounded runner，必须显式使用
  `--execute-real-worker`，且只通过 dispatcher 与 lane timeout 管理 Hermes-owned worker；
- Codex JSONL parser 只消费带 `type` 的事件，不会把 runtime receipt JSON 误当成 event；
- wrapper 在读到 `turn.completed` / `turn.failed` 后提供 5 秒进程退出宽限，避免完整 receipt
  已到达但 wall timeout 在同一轮抢先终止进程。

Deterministic fake backend 已验证：attempt-1 timeout、session reducer、attempt-2 resume、同一
session identity、receipt ingest、ledger completion、workspace mismatch fallback、resume failure
fallback、terminal-event grace、observability 和 consistency。

隔离真实 Codex 运行使用独立 `HERMES_HOME`、Git workspace 和 `CODEX_HOME`：attempt-1 在
创建并验证 `partial.txt` 后等待 runtime signal，30 秒 lane timeout 形成可审计 interrupted
fact；attempt-2 使用相同 backend session ID 执行 `codex exec resume`，创建并精确验证两行
`result.txt`，随后输出合法 runtime receipt。最终 attempt 状态为
`timed_out -> succeeded`，materialization mode 为 `fresh -> resume`，只有 1 个 backend
session，`resume_count=1`，context reacquisition=0；ledger 为 `full/verified`、job=`done`、
consistency 为 0 violations / 0 warnings。主 `.codex` 哈希不变，隔离 DB/workspace credential
scan 为 0 命中。

真实运行前一次尝试还暴露并保留了 terminal-event race：resume 已完成 artifact、verification
和 receipt，wrapper 却在 Codex 进程退出前触发 wall timeout。该问题由 terminal-event exit
grace 修复，并由 deterministic regression 固定。

本阶段仍不证明 paused worker、跨机器 session migration、backend internal subagent
session 观测、任意长时间 primary node soak 或执行前路径级 sandbox 已完成。
