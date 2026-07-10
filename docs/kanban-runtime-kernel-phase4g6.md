# Hermes Kanban Runtime Kernel Phase 4G6：长期运行可靠性 Soak

Phase 4G6 用于把已经分别验证的 Runtime Kernel 能力放入一条有意义的长期活跃运行路径，
验证多轮 decision、graph mutation、materialization、evidence ingest、recovery 和 compaction
之后，runtime 仍保持上下文连续、事实一致、可恢复且不会静默停住。

本阶段不是增加新的智能能力，也不是 Dashboard 阶段。它补齐 Phase 4G deterministic soak
与 Phase 4G5 单次真实 compaction L3 之间的长期可靠性缺口。

## 1. 背景

当前 Phase 4G baseline 已覆盖 recovery、capability、memory、compaction 和 supervisor lease
的组合路径，但其长期 tick 数主要通过 job terminal 后的 no-op tick 补齐。实际发生 runtime
状态推进的 tick 数有限，并且 scenario 仍使用旧的 fixture initialization。

Phase 4G5 已证明一次真实 compaction candidate 可以在无 deterministic fallback 时通过
validator，但尚未证明：

- 多次 checkpoint rollover 后旧 segment 是否持续退出 provider input；
- provider fallback 长期发生时是否会被静默掩盖；
- process restart 后 checkpoint/segment chain 是否仍可作为合法 decision context；
- 当前 provider-first production initialization 是否能承受长期循环；
- 多轮真实 compaction 在状态持续变化时是否仍保持 candidate quality。

Phase 4G6 解决这些问题。

## 2. 目标

Phase 4G6 必须完成：

- 新增 production-initialized active long-run deterministic scenario；
- 至少运行 50 个发生实际 runtime 推进的 active tick；
- 多轮创建 coherent primary node、materialize、ingest partial/verified evidence 和 reopen gap；
- 至少完成 5 次 compaction，并覆盖多个 compaction profile；
- 验证所有已 compacted segment 的 sentinel 不进入最新 provider input；
- 实现 DB-derived compaction health；
- 连续 fallback 超阈值时产生 quality degraded audit/event；
- provider candidate 恢复成功时清零 streak，但不删除历史；
- 实现 decision-context checkpoint chain validation；
- 模拟 process restart 后继续构造合法 provider input；
- 完成至少 3 次隔离真实 no-fallback compaction；
- 输出 bounded、可审计、无 secret 的 soak report。

## 3. 非目标

本阶段不实现：

- Dashboard 前端；
- daemon/service packaging；
- 数小时真实 worker 项目执行；
- multi-worker 并发业务任务；
- worker internal subagent 观测；
- 从 checkpoint 恢复 graph、ledger、goal 或 job state；
- 完整 event-sourced DB rebuild；
- embedding、RAG 或新的 memory learning 系统；
- 为 compaction provider 开放 tool 或 web search；
- 无真实失败证据时预先增加复杂 validator-aware repair。

## 4. Active Tick 定义

Phase 4G6 区分：

```text
active tick
=
发生至少一种 runtime 状态推进

terminal/noop tick
=
job 已 terminal 或没有产生任何新事实
```

active tick 至少满足一项：

- 新增 decision；
- patch applied/rejected；
- graph revision 变化；
- node state 变化；
- materialization attempt 创建或终止；
- evidence ingest；
- ledger/gap 变化；
- capability/human authorization 变化；
- recovery event；
- compaction/checkpoint/segment rollover；
- supervisor lease takeover；
- liveness/quality event。

Job terminal 后的 repeated supervisor skip 不计入 active tick，也不能用于满足长期运行门槛。

Report 必须同时记录：

- `active_tick_count`；
- `noop_tick_count`；
- `graph_revision_delta`；
- `decision_count`；
- `materialization_attempts`；
- `compaction_count`。

## 5. Production Initialization

新 scenario 必须使用 production runtime initialization：

```text
runtime job created
        |
        v
waiting_decision + empty graph
        |
        v
Decision Provider proposes one coherent primary node
```

禁止预建 `understand-scope` fixture。每次新增 execution node 必须：

- 关联当前 goal item/gap；
- 携带 typed contract；
- 默认一次只创建一个 runnable worker node；
- 经 graph patch validator；
- 不用 analysis/implementation/test phase 拆分制造 tick。

## 6. Active Long-Run Scenario

建议新增 scenario：

```text
phase4g6-active-long-run
```

第一版使用 scripted provider 和 synthetic worker receipt，但必须穿过 production API：

- `advance_runtime_job()` / `supervisor_runtime_tick()`；
- `apply_graph_patch()`；
- materialization；
- Kanban task/run terminal state；
- `ingest_runtime_node_evidence()`；
- `reconcile_runtime_materializations()`；
- `compact_decision_session()`；
- progress ledger、gap detector、completion 和 consistency checker。

Scenario 应循环创建一系列串行 coherent delivery node。每个 node 提供当前 goal item 的
partial evidence，最终 verifier 才提供 full/verified evidence。这样 goal 在大多数 cycle 中
保持 open，runtime 必须继续决策，而不是提前 done。

必须注入：

- 合法 patch apply；
- validator rejection；
- stale revision rejection；
- worker crash/timeout 后新 materialization attempt；
- capability requires-human 和 authorization；
- supervisor lease takeover；
- goal gap reopen；
- compaction accepted；
- provider rejection 且 no-fallback；
- deterministic fallback；
- fallback streak degradation；
- provider success recovery。

## 7. Compaction Health

Compaction health 必须从 DB 中已有 checkpoint、decision segment entry 和 event 推导，不能只
存在于 supervisor process memory。

建议提供：

```python
summarize_compaction_health(conn, job_id, policy=None) -> dict
```

最小字段：

```json
{
  "status": "healthy | degraded | unavailable",
  "provider_attempt_count": 8,
  "provider_success_count": 5,
  "fallback_count": 3,
  "fallback_streak": 2,
  "rejection_count": 1,
  "provider_error_count": 1,
  "last_result": "fallback",
  "degraded_threshold": 2,
  "operator_attention_required": true
}
```

规则：

- provider candidate accepted：`fallback_streak=0`；
- provider rejected/error 后 deterministic fallback accepted：streak 加一；
- no-fallback rejection：记录 rejection，但不伪装成 successful compaction；
- streak 达阈值时写入一次幂等 `compaction_quality_degraded` event；
- 后续 provider accepted 时写入 `compaction_quality_recovered` event；
- recovered 只改变当前 health，不删除历史 fallback/rejection；
- degraded 默认是可观测质量状态，不自动修改 graph、ledger 或 completion；
- policy 可以要求 operator attention，但不能造成无法解释的 silent idle。

Runtime inspect/dashboard API 应直接返回 `compaction_health`，而不是要求 operator 从 recent
events 自行推导。

## 8. Checkpoint Context Chain Validation

建议新增：

```python
validate_decision_context_chain(conn, job_id) -> dict
```

它验证 checkpoint 是否能继续作为 decision provider context，不执行 DB truth restore。

必须检查：

- job 只有一个 active decision segment；
- latest checkpoint 存在且 validator status 为 accepted；
- checkpoint source segment 存在且为 compacted；
- source segment 的 `compacted_checkpoint_id` 与 checkpoint 一致；
- active segment index 位于 source segment 之后；
- checkpoint revision 不在当前 graph revision 未来；
- checkpoint provenance refs 仍存在；
- covered entry range 合法；
- checkpoint 不包含 memory hint；
- active provider input 不包含 compacted segment sentinel/raw transcript。

旧 checkpoint 的 graph revision 小于当前 graph revision是正常情况，因为 short tail/current
delta 会表达 checkpoint 后的新事实。Validation 不得简单要求 checkpoint revision 等于当前
revision。

若 latest checkpoint 不合法：

- 产生显式 `decision_context_checkpoint_invalid` audit/event；
- 不修改 graph、ledger、goal 或 job state；
- 选择 prior valid checkpoint，或回退到当前 DB-derived context baseline；
- 无合法上下文时必须可观测，不能静默使用损坏 checkpoint。

## 9. 跨 Segment 输入隔离

每次 compaction 前在 source segment 写入唯一 sentinel：

```text
SEGMENT_SENTINEL_<segment_index>_<id>
```

完成 N 次 compaction 后，必须验证：

- 最新 decision provider input 不包含任一历史 sentinel；
- request 只含 latest validated checkpoint、strict short tail 和 current delta；
- rejected checkpoint 没有关闭 source segment；
- fallback checkpoint 的 provider audit 可见；
- memory hints 没有泄漏进 checkpoint；
- provenance refs 在所有 accepted checkpoint 中可验证。

## 10. 真实 Multi-Cycle Smoke

真实 smoke 与默认 deterministic soak 分离。它必须：

- 使用隔离 `HERMES_HOME`、workspace、`CODEX_HOME`；
- 使用当前真实 provider/model；
- 显式 `fallback_to_deterministic=false`；
- 最多进行少量 bounded 调用；
- 至少完成 3 次真实 compaction；
- compaction 之间注入新的 DB-derived goal/gap/event/graph state；
- 至少覆盖两个 compaction profile；
- 每次 candidate 都经过 parser 和 checkpoint validator；
- 每次 accepted 都 rollover segment；
- 最终 checkpoint context chain 和 consistency 均通过；
- 主 `.codex` 哈希不变，credential scan 为 0 命中。

若真实 candidate 出现可修复的 validator rejection，应先记录失败类型。只有重复结果证明
prompt contract 不足时，才实现最多一次 bounded validator-aware repair。

## 11. 测试计划

默认离线测试必须覆盖：

- terminal no-op 不计入 active tick；
- production initialization 不创建 fixture node；
- active scenario 至少 50 active ticks；
- graph revision、decision、materialization 和 compaction 均持续增长；
- 至少 5 次 compaction；
- 所有 historical sentinel 从最新 provider input 排除；
- fallback count/streak 推导正确；
- degraded/recovered event 幂等；
- no-fallback rejection 保留 active segment；
- process restart 后 context chain validation 通过；
- future revision、missing source segment、broken checkpoint ref 被检测；
- invalid latest checkpoint 不回写 runtime truth；
- memory hint 不进入 checkpoint；
- goal open 时不 silent idle；
- final completion 只由 full/verified ledger evidence产生；
- report bounded 且不包含 secret/raw transcript。

## 12. 验收标准

Phase 4G6 MVP 完成时：

- 中文阶段文档与 roadmap 一致；
- `active_tick_count >= 50`，且不是 terminal/noop padding；
- `compaction_count >= 5`；
- 至少三个 compacted segment 的 sentinel 被证明不进入最新 provider input；
- compaction health 能区分 healthy/degraded/unavailable；
- fallback streak 产生 degraded event，provider success 产生 recovered event；
- checkpoint context chain 在模拟 restart 后验证通过；
- invalid checkpoint 不污染 DB truth；
- consistency 0 violations；
- 至少三次隔离真实 no-fallback compaction accepted；
- 真实 smoke 覆盖至少两个 profile；
- credential scan 0 命中，主 `.codex` 哈希不变；
- 默认离线回归和 Runtime API 定向测试通过；
- 实现、文档和真实验证台账作为一个完整阶段提交并推送。

## 13. 后续关系

Phase 4G6 完成后，Runtime Kernel 才具备较可信的长期运行 production baseline。后续优先级
应是 packaged supervisor daemon/service，再做 Phase 4H Dashboard Runtime UI。Dashboard
应消费已经稳定的 compaction health、checkpoint chain、liveness、worker recovery 和
capability observability，而不是自行推导这些状态。

## 14. 当前实现与验证结果

2026-07-11 已完成 Phase 4G6 MVP。

实现入口：

```text
hermes_cli/kanban_runtime_soak.py
hermes_cli/kanban_runtime_decision.py
hermes_cli/kanban_runtime_real_smoke.py
```

新增 CLI：

```bash
hermes kanban runtime soak \
  --scenario phase4g6-active-long-run \
  --max-ticks 50 \
  --json

hermes kanban runtime real-compaction-soak <job_id> \
  --cycles 3 \
  --codex-config \
  --max-retries 0 \
  --json
```

Deterministic active soak 的当前结果：

- 62 active ticks、2 incidental noop ticks、0 terminal padding；
- production `provider_first` 初始化，空 graph 进入 `waiting_decision`；
- 27 decisions、25 applied patches、2 rejected patches；
- 25 个串行 coherent primary node、26 个 materialization attempts；
- 1 次 worker crash/retry、1 次 capability authorization、1 次 lease takeover；
- 7 次 compaction attempts、6 个 accepted checkpoints；
- 覆盖 `token_budget`、`validator_boundary`、`anti_stuck` profile；
- 两次连续 fallback 产生 degraded event，后续 provider accepted 产生 recovered event；
- 一次 no-fallback invalid candidate 被拒绝且 active segment 保留；
- goal 经历 temporary satisfied、later contradiction reopen、最终 verified evidence 恢复；
- 7 个 historical segment sentinel 全部不进入最新 provider input；
- fresh DB connection 下 6-checkpoint context chain 为 valid；
- consistency 为 0 violations、0 warnings。

隔离真实 multi-cycle smoke 使用当前 `.codex` 模型源副本，连续执行：

```text
token_budget_compaction
validator_boundary_compaction
anti_stuck_compaction
```

三轮均为 `parsed -> validator accepted -> no fallback -> segment rollover`。最终存在 3 个
compacted segment 和 1 个 active segment；fresh process context chain 检查 3 个 checkpoint
均 valid；compaction health 为 healthy，consistency 0/0，credential scan 0 命中，主
`.codex/config.toml` 与 `auth.json` 哈希不变。

默认离线 Runtime/CLI 回归 247 项通过；Runtime observability API 定向测试 1 项通过。
