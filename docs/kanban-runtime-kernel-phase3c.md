# Hermes Kanban Runtime Kernel Phase 3C 实现计划

Phase 3C 的目标是把真实 decision provider 从“能产生可落库 patch”推进到
“能参与多轮 runtime 闭环验证”。本阶段不引入 runtime daemon，也不要求真实
worker 自动执行代码；它要证明 provider patch、node materialization、Kanban
task evidence、runtime ingest、progress ledger 和后续 decision 可以串成一个
可恢复、可审计的端到端链路。

## 目标

第一，保留 Phase 3/3B 的 provider 边界。真实 provider 仍然只能返回
`runtime_graph_patch_v1` proposal，不能写 DB、不能执行工具、不能 dispatch
worker、不能直接完成 job、不能绕过 validator。

第二，补齐手动集成 smoke 的执行桥。测试或操作者应能通过 runtime CLI 按
`job_id + node_key` 写入最新 materialized Kanban task 的结构化 evidence，
然后由 `runtime advance` 正常 ingest。这个桥只完成 Kanban task，不直接改
execution node、goal item、progress ledger 或 graph revision。

第三，覆盖多轮闭环：

1. runtime job 创建 initial node；
2. supervisor materialize ready node 为 Kanban task；
3. worker/manual evidence 关闭 task；
4. advance ingest evidence 并更新 node/progress ledger/goal gap；
5. provider 看到 open gap 后提出 patch；
6. validator apply patch；
7. 后续 ready node 再次 materialize；
8. verifier evidence 满足 goal contract；
9. job 由 local completion reducer 标记 done。

第四，默认测试继续离线。Phase 3C 单元测试应使用 fake provider 和 CLI
evidence，不依赖真实模型、网络、API key 或 `.codex`。

第五，真实 `.codex` smoke 只作为手动验证路径。它可以使用临时
`HERMES_HOME`，显式 `--provider real --codex-config`，并通过
`runtime complete-node` 写入模拟 worker receipt，但不能成为默认测试前提。

## 非目标

不实现 runtime daemon。

不接真实 compaction provider。

不让 decision provider 使用 web/search/tools。

不把 `runtime complete-node` 变成 graph mutation API。它只写 Kanban task
completion metadata；runtime graph 和 ledger 必须由后续 `advance` ingest。

不把 deterministic fixture 的 analysis/implementation/verification 路径当作
默认 workflow。它仍然只是证明 goal gap -> patch -> evidence -> verifier 的
测试夹具。

## CLI

新增：

```bash
hermes kanban runtime complete-node <job_id> <node_key> \
  --summary "worker summary" \
  --metadata '{"verdict":"succeeded","claimed_goal_items":["initial-runtime-result"],"verification":{"passed":true}}' \
  --json
```

语义：

- 查找 `job_id` 下的 execution node；
- 要求该 node 已有 `latest_task_id`；
- 调用 Kanban `complete_task()` 写入 task result/summary/metadata；
- 不调用 `ingest_runtime_node_evidence()`；
- 不直接更新 progress ledger；
- 不直接改变 graph revision；
- 返回 task id、node key、metadata keys 和 `ingest_required=true`。

操作者随后运行：

```bash
hermes kanban runtime advance <job_id> --loop --provider fake --json
```

或显式真实 provider：

```bash
hermes kanban runtime advance <job_id> \
  --loop \
  --provider real \
  --codex-config \
  --profile graph_patch_decision \
  --timeout 120 \
  --json
```

## 验收标准

第一，离线 CLI 测试能用 `runtime advance --loop --fake-provider` 和
`runtime complete-node` 完成一个多轮 runtime job，并验证：

- initial node materialized；
- analysis evidence 被 ingest；
- provider patch applied；
- implementation node materialized；
- implementation evidence 更新 progress ledger 但未完成 job；
- verifier node materialized；
- verified evidence 满足 goal contract；
- job state 为 `done`；
- decision audit 至少包含 applied provider patch；
- materialization history 覆盖多个 node。

第二，`runtime complete-node` 不直接更新 ledger 或 graph revision；只有后续
`runtime advance` 才 ingest evidence。

第三，真实 `.codex` 手动 smoke 可以在隔离 job 上复用同一路径。真实 provider
只参与 decision_requested 时的 patch proposal；evidence 仍来自 Kanban task
metadata。

第四，默认相关测试继续无网络通过。
