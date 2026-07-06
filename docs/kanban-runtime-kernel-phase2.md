# Hermes Kanban Runtime Kernel Phase 2A 实现计划

本文档定义 Phase 2A 的可落地范围。Phase 1 已经证明 runtime kernel 的最小闭环可以在 pytest 中跑通；Phase 2A 的目标不是引入真实 LLM、dashboard 或常驻 daemon，而是把 runtime kernel 变成可被 Hermes Kanban 控制面调用、并能与现有 Kanban dispatcher/worker lane 执行底座接线的系统能力。

长期架构仍以 `docs/kanban-runtime-kernel-design.md` 为准。Phase 1 的不变量继续有效：DB 是权威事实源，decision session 只是非权威推理上下文，goal contract/progress ledger 定义完成，reducer 拥有 readiness/job state/gap/liveness/completion，decision provider 只能提出 graph patch，不能 release node、complete job 或自由 mark blocked。

## Phase 2A 目标

第一，新增 runtime control plane。用户或 main agent 应能通过 `hermes kanban runtime ...` 这种薄入口创建 runtime job、把已有 Kanban root task 提升为 runtime job、查询 job 状态、推进一次或有界多次 supervisor tick、列出 runtime jobs。这个入口只调用 runtime kernel public API，不直接改 execution graph。

第二，验证 runtime materialization 和现有 Kanban 执行层相容。runtime node 物化后生成的 Kanban task 必须仍然能被现有 `dispatch_once()` 和 worker lane registry 处理；worker 完成后，kernel 必须能通过 `task_progress_snapshot()` ingest evidence，更新 node、event、artifact、progress ledger 和 goal gap。

第三，补齐 Phase 2A 的审计证据。CLI/API 的 create/promote/status/advance/list 都必须支持 JSON 输出，便于 dashboard、main agent 或后续 API 复用。每次 provider decision 仍要写入 `kernel_decisions`，每次 patch 仍要写入 `graph_patches`，decision delta 仍是追加到 decision session 的 DB-derived delta，而不是完整冷 snapshot。

## 明确非目标

不接真实 LLM provider。Phase 2A 可以继续使用 deterministic fake provider 或显式禁用 provider；真实 LLM provider 是后续阶段。

不做常驻 runtime daemon。Phase 2A 的 supervisor 是 CLI/API 触发的 bounded loop，不能和 Kanban dispatcher 合并成一个智能总控进程。

不做 dashboard UI 迁移。可以保留未来 `/runtime/jobs` API 的形状，但本阶段不实现 React/Ink dashboard 页面。

不把真实 Codex smoke、网络、外部 API key 或真实 Claude Code/Codex 进程作为完成前提。可用 worker-lane fixture 验证执行层接线；真实 agent smoke 只属于后续集成验证。

不恢复旧 Orchestra phase manager，不引入 planner/coder/reviewer/tester 固定角色，不把 `analysis -> implementation -> verification` 写成默认生命周期。测试 fixture 可以走这个路径，但必须由 goal gaps 和 graph patch 驱动。

## Control Plane CLI

新增 `hermes kanban runtime` 子命令组。建议子命令如下：

`create <objective>`：创建 root Kanban task 和 runtime job。可选 `--goal-item KEY:DESC`，没有指定时使用 Phase 1 默认 goal item。可选 `--workspace`、`--assignee`、`--created-by`、`--idempotency-key`、`--json`。

`promote <task_id>`：把已有 Kanban task 提升为 runtime job。objective 默认来自 task title/body，也可用 `--objective` 覆盖。它不复制任务历史，只创建 runtime job 并把 `root_task_id` 指向已有 task。

`status <job_id>`：输出 runtime job 状态。JSON 模式返回 `status_runtime_job()` 的结构；人读模式显示 job state、objective、goal items、open gaps、frontier nodes、recent decisions。

`list`：列出 runtime jobs。至少显示 job id、state、root task id、objective、graph revision、updated_at。JSON 模式返回结构化数组。

`advance <job_id>`：执行 runtime supervisor tick。默认只 advance 一次；`--loop --max-steps N` 执行有界循环，遇到 done、waiting_worker、waiting_human、blocked、或没有可推进动作时停止。可选 `--no-create-tasks`、`--fake-provider`、`--json`。`--fake-provider` 只用于 fixture/dev，不是默认真实智能。

所有子命令必须尊重 `--board`。runtime job 创建时保存 board，后续 Kanban 读取必须传同一个 board，避免跨 board 混读。

## Helper API

Phase 2A 可以继续把代码放在 `hermes_cli/kanban_runtime_kernel.py`，但应补齐薄 helper：

`list_runtime_jobs(conn, state=None, limit=50)`：返回 runtime job 列表。

`promote_runtime_job(conn, task_id, objective=None, board=None, workspace_path=None, goal_items=None)`：基于已有 root task 创建 runtime job。

`create_runtime_job_from_objective(conn, objective, board=None, workspace_path=None, assignee=None, created_by="runtime", goal_items=None, idempotency_key=None)`：创建 root Kanban task 后创建 runtime job。

`advance_runtime_job_until_idle(conn, job_id, board=None, create_tasks=True, decision_provider=None, max_steps=8)`：有界 supervisor loop。它不是 daemon；每次退出必须返回清晰 reason，例如 `done`、`waiting_worker`、`waiting_human`、`waiting_decision`、`max_steps`、`no_progress`。

这些 helper 不得绕过 patch validator，也不得直接改 progress ledger 来伪造完成。

## 执行底座接线

runtime node materialization 继续使用 `kanban_db.create_task()`。Phase 2A 测试必须证明：

物化出来的 task 可以带 assignee/worker lane。

`dispatch_once()` 可以 claim/spawn 该 task，且不会破坏 `node_materializations` 和 `execution_nodes.latest_task_id/latest_run_id`。

worker-lane fixture 或 fake completion 可以通过现有 `complete_task()` 写入 evidence。

`ingest_runtime_node_evidence()` 通过 `task_progress_snapshot()` 读取同一份 evidence，并更新 runtime node/event/progress ledger。

这项测试的目标是验证 Kanban 执行底座抽象，不是验证某个具体 Codex lane。

## 测试清单

`test_runtime_cli_create_status_and_list_json`：通过 `run_slash("runtime create ... --json")` 创建 job，再用 status/list 查询。

`test_runtime_cli_promote_existing_root_task`：先创建普通 Kanban task，再 promote 成 runtime job，确认 root_task_id、objective、goal contract 和初始 node。

`test_runtime_cli_advance_materializes_initial_node`：CLI advance 能把 ready node 物化为 Kanban task，并保持重复 advance 幂等。

`test_runtime_bounded_loop_records_decision_delta`：使用 fake provider 有界推进，确认 `kernel_decisions.delta_json` 和 `decision_sessions.context_state_json` 有增量记录。

`test_runtime_materialized_task_dispatch_and_ingest_fixture_lane`：给 runtime node 指定测试 worker lane，advance 物化后用 `dispatch_once()` 处理，再用 fake evidence 完成 task，最后 ingest 回 runtime。

`test_runtime_cli_does_not_enable_real_llm_by_default`：默认 advance 不调用真实 provider；只有显式 `--fake-provider` 才使用 fixture provider。

## 完成定义

Phase 2A 完成必须满足：

第一，`docs/kanban-runtime-kernel-phase2.md` 存在，`AGENTS.md` 明确要求 runtime branch 遵守 design、phase1 和 phase2 文档。

第二，`hermes kanban runtime create/promote/status/list/advance` 或等价 CLI helpers 可用，且 JSON 输出稳定。

第三，pytest 覆盖 CLI control plane、existing root task promote、bounded advance、decision delta/session 审计、runtime materialization 与 Kanban dispatch/worker-lane fixture 接线。

第四，相关测试通过，且不依赖真实 LLM、真实 Codex、网络、dashboard 或常驻 daemon。
