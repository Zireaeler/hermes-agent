# Hermes Kanban Runtime Kernel 设计草案

本文档记录新实现线的基础设计。新分支为 `feature-kanban-runtime-kernel`，新 worktree 位于 `/tmp/hermes-agent-runtime-kernel`，基线提交为 `b5a262c fix(kanban): normalize codex receipt tails`。这个基线刻意选在旧 Orchestra 引入之前，因为它已经包含 external worker lanes、worker evidence、review/followup 和 dashboard/API 的执行底座，也包含一个可用的 Codex CLI worker backend；但 Codex 在新架构里只是可选执行单元，不是架构中心，也不应该出现在分支名里。

这个分支的目标不是继续扩展旧 `kanban_orchestra.py`，而是在 Hermes Kanban 的执行层之上新增一个事件驱动的 runtime kernel。Kanban 继续负责 worker 生命周期、任务派发、lane 解析、进程启动、heartbeat、timeout、crash/retry、task event、run evidence 和 dashboard 基础可观测性；runtime kernel 负责维护一个外部持久化的 execution graph，并在结构性事件发生时调用一次受限决策函数，生成可验证的 graph patch，再把可运行节点物化成 Kanban worker task。换句话说，Kanban 是执行系统，runtime kernel 是图调度系统，具体 worker backend 可以是 Codex、Claude Code、本地脚本、人工作业或后续任何可注册 lane。

## 一句话架构

系统的连续性只存在于数据库里的 job、execution graph、event log、artifact 和 graph patch 记录中。LLM 不作为长期 agent 存活，也不拥有隐式记忆；它只在 kernel 认为需要重算结构时被调用一次，输入是压缩后的状态快照，输出是受限 schema 的结构 patch。

## 与旧 Orchestra 的边界

旧 Orchestra 的中心是阶段式 manager loop。它用固定 phase 推进任务，典型路径是 planning、plan_review、implementation、review/test、fix、done/blocked，并围绕 planner、coder、reviewer、tester 这些角色创建 invocation。这个模型适合证明“Kanban 可以调起多个 worker 并收集 evidence”，但它已经预设了协作形态。

新 runtime kernel 不预设固定角色和固定阶段。它只维护 execution graph。节点可以是 analysis、implementation、verification、debug、research、human_gate 或 artifact_transform，但这些是 node type，不是固定流程。node_type 只表示执行意图、能力需求和 worker context 类型，不表达生命周期阶段，也不允许 kernel 根据 node_type 推导固定下一阶段。节点之间的关系由 dependency edge、artifact edge 和本地 policy 表达。结构如何长出来，由 event 触发的 decision patch 决定。

旧 Orchestra 代码可以作为参考，但不作为核心继承对象。可复用的是其中的经验，比如 worker context 如何截断、evidence 如何摘要、dashboard 如何呈现 worker lane、browser smoke 如何验证页面；不可复用的是固定 phase state machine 和 manager mailbox 作为核心调度语义。

## 复用 Hermes Kanban 的部分

当前基线已经有几块必须复用的工程资产。第一是 `tasks`、`task_runs`、`task_events`、`task_links` 这些 Kanban 数据结构，它们负责真实 worker 执行和审计。第二是 `dispatch_once()` 和 daemon，它们负责 ready task 的 claim、spawn、并发控制、stale reclaim、timeout 和 crash 检测。第三是 `worker_lanes.py`，它提供“某个 assignee 可以被解析成外部 worker lane”的抽象；`codex_worker.py` 是当前可用的一个实现，但不是 runtime kernel 的必选依赖。第四是 `task_progress_snapshot()`，它把 worker run、recent events、evidence、git metadata、verification 和 log tail 压成 dashboard/kernel 可读的进度快照。

runtime kernel 不应该绕开这些能力直接管理子进程。kernel 只决定哪些 execution node 现在可以运行，以及它们需要什么输入。真正的执行仍然通过创建或更新 Kanban task 完成。这样可以保留已有的 worker 隔离、board 解析、workspace 解析、日志、证据采集和失败处理。

## 不复用当前 HEAD 的部分

当前旧分支 HEAD `8bfd9df` 之后的 Orchestra 实现不作为新线基线。原因不是它没有价值，而是它已经把“任务如何推进”固化成另一套产品语义。新架构需要的是 event driven graph patch，而不是 manager phase advance。继续在当前 HEAD 上硬改，会导致代码里同时存在 phase、role、invocation、mailbox、execution graph、patch、kernel decision 多套概念，长期会变得难以解释和测试。

当前 HEAD 的前端和 smoke 脚本可以后续手工迁移，尤其是 task-centered dashboard、worker lane panel、progress/evidence panel、review/test gate、debug folded sections 这些 UI 经验。但迁移时应该面向新的 runtime API，而不是保留 Orchestra API 形状。

## 核心数据模型

第一阶段建议新增一个模块 `hermes_cli/kanban_runtime_kernel.py`，不要复用 `kanban_orchestra.py` 文件名。数据库 schema 使用独立表，和 Kanban 基础表通过 `task_id`、`run_id`、`board`、`tenant` 关联。

建议的核心表如下。

`runtime_jobs` 保存一个用户复杂任务的根对象。它对应用户最初提交的 root task 或 job description。字段包括 `id`、`root_task_id`、`board`、`state`、`objective`、`workspace_path`、`decision_profile`、`metadata`、`created_at`、`updated_at`。`state` 初期限制为 `active`、`waiting_worker`、`waiting_decision`、`waiting_human`、`blocked`、`done`、`cancelled`、`failed`。

`execution_nodes` 保存执行图节点。字段包括 `id`、`job_id`、`node_key`、`node_type`、`state`、`title`、`description`、`assignee`、`task_id`、`run_id`、`input_summary`、`output_summary`、`assumptions_json`、`constraints_json`、`metadata`、`created_at`、`updated_at`、`started_at`、`completed_at`。`node_key` 是 job 内稳定键，用于 graph patch 引用。`state` 初期限制为 `planned`、`ready`、`running`、`succeeded`、`failed`、`blocked`、`waiting_dependency`、`waiting_human`、`cancelled`、`superseded`。

`execution_edges` 保存图依赖。字段包括 `id`、`job_id`、`from_node_id`、`to_node_id`、`edge_type`、`required`、`metadata`、`created_at`。`edge_type` 初期限制为 `depends_on`、`artifact_input`、`verifies`、`blocks`、`supersedes`。提交 patch 时必须验证 DAG，除非 edge type 明确允许非调度依赖；第一阶段建议全部按 DAG 处理。

`execution_events` 保存结构性事件流。字段包括 `id`、`job_id`、`node_id`、`task_id`、`run_id`、`event_type`、`payload`、`source`、`source_event_id`、`graph_revision`、`created_at`。这里不是把所有 Kanban task_events 原样复制一遍，而是只记录 kernel 关心的结构性事件，例如 `job_created`、`node_created`、`node_materialized`、`node_started`、`node_progressed`、`node_completed`、`node_failed`、`node_uncertain`、`node_blocked`、`artifact_ready`、`dependency_satisfied`、`decision_requested`、`patch_applied`、`patch_rejected`、`human_required`、`structure_audit_requested`。

`graph_patches` 保存每次结构修改。字段包括 `id`、`job_id`、`decision_id`、`base_revision`、`applied_revision`、`patch_json`、`status`、`reject_reason`、`created_at`、`applied_at`。`status` 限制为 `proposed`、`applied`、`rejected`、`noop`。`base_revision` 是 decision provider 看到的 graph revision，patch 默认只能应用到相同 revision 上。

`kernel_decisions` 保存每次决策函数调用。字段包括 `id`、`job_id`、`trigger_event_id`、`snapshot_revision`、`snapshot_json`、`decision_json`、`model`、`status`、`error`、`created_at`、`completed_at`。第一阶段可以用 deterministic fake decision provider，后续再接真实 LLM。

`node_artifacts` 保存节点产物引用。字段包括 `id`、`job_id`、`node_id`、`artifact_type`、`path_or_ref`、`summary`、`metadata`、`created_at`。这里可以引用 worker 产生的文件、evidence markdown、测试结果、diff 摘要、外部工具结果，但事实状态仍以 DB 行为准。

## Kernel 循环

kernel 是可重复调用的函数，不是长时间思考的 agent。第一阶段可以实现为 `advance_runtime_job(conn, job_id, *, board=None, decision_provider=None, max_patches=1)`，后续再包一层 `supervise_runtime_job()` 做 bounded loop。

每次 advance 做五件事。第一，ingest Kanban worker 状态，把已物化节点绑定的 `task_id` 通过 `task_progress_snapshot()` 读取，识别是否完成、失败、阻塞或产生结构性进展，并写入 `execution_events`。第二，运行本地 reducer，重算节点 readiness、job 状态、synthetic audit event 和 completion rule；所有依赖满足的 `planned` 或 `waiting_dependency` 节点由 reducer 变为 `ready`。第三，把 `ready` 且尚未创建 Kanban task 的节点物化成真实 Kanban task，写回 `task_id`、`run_id`，并记录 `node_materialized`。第四，如果出现需要结构重算的事件，就构造压缩 snapshot，调用 decision provider 取得 graph patch。第五，验证并应用 graph patch，然后再次运行 reducer 更新可运行节点。

这个循环的关键是不让 LLM 直接改数据库，也不让 LLM 决定节点放行。LLM 或 fake provider 只能返回 patch JSON。所有 patch 都必须经过本地 validator。validator 负责检查引用的 node_key 是否存在、是否重复创建、edge 是否成环、状态迁移是否合法、node type 是否允许、assignee/lane 是否存在或可延后、patch 是否幂等、是否越权修改已完成节点、patch 的 `expected_revision` 是否仍匹配当前 graph revision。

## Snapshot 构造

snapshot 是决策函数唯一输入，不是完整历史。它应该由系统构造，初期包含这些部分。

`job` 包括 objective、state、workspace、当前未解决约束、用户可见目标。

`graph` 包括节点列表、节点状态、关键依赖边、每个节点的一句话输入输出摘要、最近 verdict、是否有 artifact。

`recent_events` 只取最近 N 条结构性事件，而不是 worker 全量日志。每条事件保留 event_type、node_key、summary、payload 中的关键字段。

`open_constraints` 保存当前没有解决的约束，例如缺少依赖信息、测试失败、需要人工选择、外部资源不可用、需要并行拆分。

`active_assumptions` 保存当前仍被系统采用的压缩假设，例如“目标仓库路径已确认”“失败主要来自认证而非代码逻辑”。这些不是推理链，而是后续结构决策不能丢的事实状态。

`rejected_approaches` 保存已经尝试并排除的方案，例如“直接复用旧 Orchestra phase machine 会造成语义污染”。它应包含简短原因和证据引用，避免 stateless decision provider 重复生成失败路径。

`known_failure_boundaries` 保存失败边界和不可越过约束，例如“当前没有 GitHub HTTPS 凭据”“某 lane 不可 spawn”“某验证命令在环境中不可用”。这些边界进入 snapshot 后，provider 才能做局部结构调整，而不是反复要求执行不可行动作。

`open_questions` 和 `risk_notes` 保存仍需人工或后续节点解决的问题。它们应该来自 worker receipt、human gate 或 synthetic audit，而不是从完整 worker 对话里临时抽取。

`available_actions` 明确告诉决策函数只能返回哪些 patch op。第一阶段 action 集合越小越好。

`policy` 描述本地硬规则，例如不能删除已完成节点、不能绕过 verifier、不能直接标记 job done、不能创建没有 title/description 的节点。

Snapshot 要足够小，可以进入一次 LLM 调用；但它必须包含 graph 拓扑和未解决约束，否则决策函数会退化成自由规划。

## Graph Patch schema

第一阶段只支持最小 patch 集合。

`create_node` 创建一个新 execution node。必填 `node_key`、`node_type`、`title`、`description`。可选 `assignee`、`constraints`、`depends_on`。

`add_dependency` 添加一条依赖边。必填 `from_node_key`、`to_node_key`，可选 `edge_type`。

`insert_verifier` 为某个节点创建验证节点，并自动添加 `target -> verifier` 的依赖关系。

`split_node` 把一个未开始或失败待重构节点拆成多个子节点，并把原节点标记为 `superseded`。第一阶段可以先不实现这个 op，而是用 create_node + add_dependency 替代。

`mark_blocked` 把 job 或 node 标成 blocked，并要求提供机器可读 reason。

`request_human` 写入 human gate，要求提供问题、候选项、默认建议和风险说明。

`complete_job` 只能在所有 required terminal verifier 通过后生效。第一阶段可以先由本地规则自动 complete，不开放给 LLM patch。

v1 明确不提供 `release_node`。节点是否从 `planned` 或 `waiting_dependency` 进入 `ready`，只能由本地 reducer 根据 dependency、node state、policy、lock、lane availability 和 human gate 状态计算。decision provider 可以改变图结构和约束，但不能直接放行执行。

每个 patch 必须包含 `expected_revision`。如果当前 graph revision 已经变化，validator 必须拒绝该 patch，或在所有 op 都已经等价存在时将其规范化成 noop；不能在过期 snapshot 上盲目 merge。

patch 示例形状如下：

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "implementation evidence shows two independent verification paths are needed",
  "ops": [
    {
      "op": "create_node",
      "node_key": "verify-unit-tests",
      "node_type": "verification",
      "title": "Run focused unit tests",
      "description": "Run the tests named by implementation evidence and report pass/fail.",
      "assignee": "test-worker",
      "depends_on": ["implement-core"]
    },
    {
      "op": "insert_verifier",
      "target_node_key": "implement-core",
      "verifier_node_key": "review-diff",
      "title": "Review implementation diff",
      "assignee": "review-worker"
    }
  ]
}
```

## 首个可跑切片

第一阶段目标不是做完整智能系统，而是跑通闭环。具体切片如下。

用户或 CLI 创建 root Kanban task，再创建 `runtime_job` 和一个初始 `analysis` node。该 node 被物化成 Kanban task，派给配置中的分析 lane 或测试里的 fake worker lane。worker 完成后写入 evidence。kernel ingest evidence，把 analysis node 标成 succeeded，并记录一个 `node_completed` 事件。decision provider 根据 snapshot 返回 patch，创建一个 `implementation` node 和一个 `verification` node，并建立 implementation 到 verification 的依赖。kernel 应用 patch，把 implementation 物化成 Kanban task。implementation 完成后，local reducer 根据依赖满足情况自动释放 verification node。verification 通过后，本地规则把 job 标成 done。

这个切片证明四件事：execution graph 是数据库事实源，worker 只是节点执行器，LLM/decision provider 只返回 patch，Kanban 继续承担真实执行生命周期。

这个切片只是 deterministic fixture，用来证明 graph patch、Kanban task materialization、evidence ingest 和本地 completion rule 能跑通。它不是默认任务模板，也不允许实现中硬编码 `analysis -> implementation -> verification`。初始 `analysis` 节点只能建立可执行理解和压缩状态，不能输出完整固定计划链；后续结构必须来自事件、snapshot 和受限 patch。

## API 初稿

CLI/API 第一阶段可以非常薄。

`create_runtime_job(conn, root_task_id, objective, board=None)` 创建 job 和初始 analysis node。

`status_runtime_job(conn, job_id)` 返回 job、nodes、edges、recent_events、pending_decisions、mapped Kanban task progress。

`advance_runtime_job(conn, job_id, board=None, create_tasks=True, decision_provider=None)` 执行一次 kernel tick。

`supervise_runtime_job(conn, job_id, mode="once"|"loop", max_steps=None, interval=5.0)` 对一个 job 做 bounded loop，遇到 waiting_worker、waiting_human、done、blocked、max_steps 即停。

`apply_graph_patch(conn, job_id, patch, decision_id=None)` 校验并事务应用 patch。

`build_decision_snapshot(conn, job_id, trigger_event_id=None)` 构造压缩快照。

`ingest_runtime_node_evidence(conn, node_id, board=None)` 从 Kanban task progress snapshot 吸收结构性结果。

dashboard API 后续再暴露 `/runtime/jobs`、`/runtime/jobs/{id}`、`/runtime/jobs/{id}/advance`、`/runtime/jobs/{id}/events`、`/runtime/jobs/{id}/graph`、`/runtime/jobs/{id}/patches`。第一阶段可以先不做前端，只做 CLI 和 pytest。

## 与 Markdown 的关系

DB 是唯一事实源。Markdown 只能作为 artifact、人读 handoff、evidence 摘要或审计文档。kernel 不应该依赖“读某个 agent memory md”来判断状态。需要进入决策的内容必须先被摘要进 DB snapshot 或 artifact summary。worker 可以写 markdown evidence，但 ingest 后要把 verdict、changed files、verification、artifact refs、summary 写入结构化字段。

## 触发策略

不是所有 worker 事件都触发决策。worker streaming/progress event、heartbeat、普通日志增长只进入 Kanban event 或 progress summary，不直接唤醒 decision provider。Codex JSON event 如果作为某个 lane 的进度事件出现，也按这个规则处理。触发结构重算的事件初期只包括 node completed、node failed、node blocked、node uncertain、human decision received、dependency cycle/rejection、manual operator request。

这样可以避免每个微小输出都触发 LLM 重算，也能保证系统成本和状态变化可解释。

显性事件之外，还需要 synthetic audit。runtime reducer 应在本地生成少量结构审计事件，例如 active frontier 长时间没有推进、所有节点都 succeeded 但 open_constraints 没减少、同类失败重复出现、缺少 required verification、没有 runnable node 但 job 也不是 blocked/done、patch 连续被拒绝、或 graph revision 长时间不变化。这类事件可以写成 `structure_audit_requested` 或 `node_uncertain`，再触发一次受限决策。synthetic audit 的作用是发现静默失败，而不是把普通 progress event 升级成 LLM 触发器。

## 测试策略

第一批测试应全部使用 deterministic fake decision provider 和 fake worker evidence，不依赖任何真实外部 agent 或网络调用。测试需要覆盖 schema 初始化、job 创建、初始 node 创建、patch validator、DAG cycle rejection、ready 计算、Kanban task materialization、evidence ingest、decision snapshot、patch application、job done rule。

第二批测试再接入 existing worker lane fixtures，验证 runtime node 物化后能走 `dispatch_once()`、worker receipt 能被 `task_progress_snapshot()` 读取、kernel 能把 worker evidence 转成 node event。这里可以覆盖 Codex lane，但测试目标是 lane/backend abstraction，不是 Codex 专用链路。

真实 Codex smoke 或其他真实 agent smoke 只作为后续集成验证，不作为第一阶段单测前提。

## 实现顺序

第一步新增 `kanban_runtime_kernel.py`，只包含 schema、dataclass、create/status/list helpers。第二步实现 patch schema 和 validator，不接 LLM，并确保 v1 patch 不包含 `release_node`。第三步实现 `create_runtime_job()` 和初始 analysis node。第四步实现 reducer 和 node materialization，把本地规则计算出的 ready node 变成 Kanban task。第五步实现 evidence ingest，从 task snapshot 更新 node、assumptions 和 event。第六步实现 deterministic decision provider，跑通 fixture graph 闭环，但不得把 fixture 写成默认流程。第七步加 CLI/API 薄封装。第八步再考虑 dashboard。

这个顺序的原则是先证明状态机和 graph patch，而不是先做 UI 或真实 LLM。

## 分支约束

本设计属于 `feature-kanban-runtime-kernel`。不要在这个分支例行 rebase main。不要把旧超大 session 作为运行上下文恢复。当前旧 Orchestra 分支可以保留为参考和部署验证记录，但新实现线应保持概念干净：Kanban 是执行基座，runtime kernel 是事件驱动图调度层，LLM 是 stateless graph patch decision function，具体 worker backend 是可替换执行单元。

## 运行进程形态

第一阶段不需要引入新的常驻服务。runtime kernel 可以先作为 CLI/API 调用中的同步函数存在，和现有 Kanban dispatcher 解耦。推荐的最小运行形态是三个循环并存，但职责分开。

第一个循环是现有 Kanban dispatcher daemon。它只看 Kanban `tasks` 表中可 dispatch 的 task，负责 claim 和 spawn worker。它不理解 execution graph，也不直接调用 decision provider。

第二个循环是 runtime supervisor。它可以先不是 daemon，而是一个 bounded loop：每次调用 `advance_runtime_job()`，遇到等待 worker、等待人工、完成、阻塞或达到 max_steps 就停。后续可以做成 dashboard/API 触发的后台线程，但第一阶段不需要。

第三个循环是 worker 自己的执行过程。worker 接收一个 Kanban task，只知道本节点的局部上下文、依赖输出摘要、约束和 expected receipt shape。worker 不知道全局 graph，也不和其他 worker 直接通信。

工程上必须避免把这三个循环揉成一个“智能总控 daemon”。dispatcher 维护执行生命周期，runtime supervisor 维护图结构，worker 维护单节点执行。三者通过 DB 和事件行通信。

## 任务创建入口

新系统应支持两个入口。第一个入口是已有 root Kanban task，即用户先创建一个普通 Kanban task，再把它提升为 runtime job。这个入口适合兼容当前 dashboard 和 CLI。第二个入口是从 goal/job description 直接创建 runtime job，同时自动创建 root Kanban task。这个入口适合未来 main-agent 控制面。

无论入口如何，创建后都必须得到一个 `runtime_jobs` 行、一个 root-level `execution_nodes` 初始节点，以及一条 `job_created` 事件。初始节点不应该是完整计划，而应该是 `analysis` 或 `understanding` 类型，目的只是建立当前问题的可执行理解。这样系统不会在第一步就陷入一次性大规划。

## 表结构细化

下面是第一阶段 schema 的建议细节。最终代码可以按 SQLite 约束能力调整，但字段语义应保持稳定。

`runtime_jobs.id` 使用短文本 id，例如 `rjob_<hex>`。`root_task_id` 对应 Kanban root task，可以为空只在极早期创建事务中短暂存在，提交后必须有值。`board` 固化创建时所在 board，后续所有 Kanban 读取都必须传入这个 board，避免跨 board 混读。`objective` 是用户目标的规范化文本。`state` 是 job 级状态，不等于所有 node 状态的简单聚合，但应由 node/event 推导更新。`decision_profile` 是决策函数配置名，可以映射到真实 LLM provider、fake provider 或禁用策略。`metadata` 用于存储非核心扩展，例如 `last_event_cursor`、`last_decision_id`、`human_gate`、`completion_rule`。

`execution_nodes.node_key` 必须在同一个 job 内唯一，并且是 graph patch 的稳定引用。它不能用自增 id 暴露给 LLM，因为 LLM 更适合引用语义键，例如 `understand-scope`、`implement-parser`、`verify-regression`。`node_type` 表示执行意图和能力需求，第一阶段只开放 `analysis`、`implementation`、`verification`、`review`、`debug`、`human_gate`、`artifact_transform`。这些名称不能被解释成固定 phase，也不能触发固定 next step。`assignee` 是 Kanban assignee/lane 名，可以为空；为空时 kernel 可以根据显式配置的 node_type-to-lane policy 选择默认 lane，或者停在 `blocked`/`waiting_human` 要求人工指定，但不能把 lane policy 扩展成流程模板。`task_id` 和 `run_id` 是物化后的 Kanban 映射，未物化前为空。`input_summary` 是给 worker 的局部输入摘要，`output_summary` 是 ingest 后的结构化输出摘要。`assumptions_json` 保存当前节点贡献的压缩认知状态，建议包含 `active_assumptions`、`rejected_approaches`、`known_failure_boundaries`、`open_questions`、`risk_notes`。`constraints_json` 保存硬约束，例如必须运行哪些测试、不能修改哪些路径、需要人工确认哪些外部操作。

`execution_edges` 中 `depends_on` 是调度依赖，表示 from 成功后 to 才能 ready。`artifact_input` 表示 to 需要读取 from 的 artifact。`verifies` 表示 to 验证 from 的结果。第一阶段可以把三者都视为 required dependency，以保证行为简单。后续如果需要软依赖，再引入 `required=0` 和 readiness policy。

`execution_events` 是 kernel 的事件流，不是日志垃圾桶。每条 event 应该有机器可读 payload，并尽量包含一行 `summary`。如果来自 Kanban task_event，`source_event_id` 指向原事件 id，保证 ingest 幂等。对于同一个 `source_event_id` 和 `event_type`，应有唯一性保护或代码层去重。

`kernel_decisions.snapshot_json` 保存压缩快照，而不是完整数据库 dump。`snapshot_revision` 保存该快照对应的 graph revision。`decision_json` 保存 provider 原始返回。即使 patch 被拒绝，decision 也要保留，方便解释为什么被拒绝。`status` 建议为 `started`、`completed`、`failed`、`rejected_patch`。

`graph_patches.patch_json` 保存规范化后的 patch。规范化意味着默认值已补齐、node_key 已 trim、op 顺序已确定、不可识别字段已拒绝或移入 metadata。这样后续审计时不依赖 provider 的原始输出。`base_revision` 必须来自 patch 的 `expected_revision`；成功 apply 后递增 job 的 graph revision，并把新 revision 记录到 `applied_revision`。

## 状态迁移细节

`runtime_jobs` 的状态建议按本地规则更新。创建后是 `active`。如果存在 running node，则是 `waiting_worker`。如果没有 running node，但存在可运行未物化 node，则仍是 `active`，下一次 advance 应物化它们。若最新结构性事件需要决策且 decision provider 尚未完成，可以短暂进入 `waiting_decision`。若存在 active human gate，则是 `waiting_human`。所有 required terminal verifier 通过后进入 `done`。无法继续且需要人工修复系统状态时进入 `blocked`。代码异常或 provider 调用失败不应直接让 job failed，除非错误不可恢复；一般应记录 `decision_failed` 或 `patch_rejected`，然后停在 `blocked` 或 `waiting_human`。

`execution_nodes` 的状态迁移必须严格。`planned` 可以到 `waiting_dependency` 或 `ready`。`waiting_dependency` 在依赖满足后到 `ready`。`ready` 物化成 Kanban task 后到 `running`。`running` 根据 ingest 到 `succeeded`、`failed`、`blocked`、`waiting_human`。`failed` 可以通过 patch 被 `superseded`，也可以创建 debug/fix 节点依赖它，但不应该原地改回 `ready`，除非这是明确的 retry op。`succeeded` 默认不可修改，只能被后续 verifier 判定为需要补充工作，但不能直接篡改原节点结果。

这些 state 是 materialized execution view，不是任务 phase。它们描述节点是否已计划、是否可运行、是否正在被某个 worker 执行、是否已有终态 evidence。真正的调度语义来自 graph、edge、policy 和 local reducer，而不是来自 `state + node_type` 的手写流程条件。实现中应避免出现“某 state 下某 node_type 固定生成某下一节点”的规则；这类结构变化只能来自 patch 或明确的本地安全规则。

Kanban task 状态和 node 状态的映射不能一一硬绑。Kanban `done` 通常对应 node `succeeded`，但如果 evidence verdict 是 `needs_fix`，node 应是 `failed` 或 `blocked`。Kanban `blocked` 对应 node `blocked`，但如果 block reason 是等待人工输入，node 应是 `waiting_human`。Kanban `running` 对应 node `running`。Kanban `ready` 只应该短暂存在于已物化但未 claim 的 task，node 仍可视为 `running` 或 `ready_materialized`；第一阶段为简单起见可以把已创建 task 的 node 统一标为 `running`，并通过 task snapshot 展示真实 dispatch 状态。

## Kanban 物化规则

node 物化成 Kanban task 时，task body 不是完整历史，而是 worker context。context 至少包含 root objective、node title/description、node constraints、直接依赖节点的 output_summary、相关 artifact refs、expected receipt schema、完成/阻塞协议。expected receipt schema 必须要求 worker 返回 summary、verdict、artifacts、verification、active_assumptions、decisions_made、rejected_approaches、known_failure_boundaries、open_questions、risk_notes。这里保存的是压缩事实，不要求也不鼓励输出完整推理链。task body 还要写入机器可读 footer，例如 `runtime_job_id`、`execution_node_id`、`node_key`、`node_type`，方便 worker evidence 和日志回溯。

新 task 的 `created_by` 建议为 `runtime_kernel`，`tenant` 建议为 `runtime:<job_id>`。`idempotency_key` 必须包含 job_id 和 node_id，避免重复 advance 创建重复 task。`workspace_kind` 和 `workspace_path` 继承 root task/job。`assignee` 来自 node 或显式配置的 lane policy；lane policy 只能选择执行后端，不能生成后续流程。`initial_status` 可以直接用 `running` 走现有 create path，也可以用 `ready` 让 dispatcher claim；应优先沿用当前 Kanban 创建 worker task 的成熟路径，具体以基线代码 helper 能力为准。

物化后马上记录 `node_materialized` event，并把 Kanban `task_id`、latest `run_id` 写回 node。若 task 创建成功但写回 node 失败，事务必须整体回滚；不要留下孤儿 task。若使用的 Kanban helper 内部自己开事务，runtime kernel 需要避免外层再开冲突事务，必要时先实现一个内部 helper 或接受“两阶段但幂等”的物化策略。

## Evidence ingest 规则

ingest 不读取 worker 完整对话，只读 `task_progress_snapshot()` 和必要的 artifact 文件。它应提取这些结构化字段：task status、run outcome、worker verdict、summary、changed_files、verification results、artifact refs、failure reason、human request、recent progress event summary、active_assumptions、decisions_made、rejected_approaches、known_failure_boundaries、open_questions、risk_notes。

verdict 需要规范化。`pass`、`approved`、`success` 归为 `succeeded`。`needs_fix`、`failed`、`error` 归为 `failed`。`blocked` 归为 `blocked`。`human_required`、`requires_human` 归为 `waiting_human`。无法判断但 task 已结束时归为 `node_uncertain`，触发 decision provider 或人工 gate，而不是假装成功。

ingest 必须幂等。每个 node metadata 可以保存 `last_ingested_run_id`、`last_ingested_event_id`、`ingested_terminal`。如果同一个 run 已经 terminal ingest 过，后续 advance 不应重复写 node_completed 或 node_failed。对于 progress 类事件，可以只保留最后 N 条摘要，不必全部复制到 execution_events。认知状态字段应被合并到 node 的 `assumptions_json`，并由 snapshot builder 汇总到 job 级 `active_assumptions`、`rejected_approaches` 和 `known_failure_boundaries`；不要只把这些内容写入 markdown evidence。

## Decision provider 接口

第一阶段 provider 可以是 Python callable，不需要立即接真实 LLM。建议接口形状为 `decision_provider(snapshot: dict) -> dict`。返回值必须是 patch JSON 或明确 noop。真实 LLM provider 只是这个接口的一种实现，负责把 snapshot 渲染成 prompt、调用模型、解析 JSON、返回 patch。fake provider 可以根据 snapshot 中的节点状态确定性返回下一步 patch。

provider 不允许访问数据库连接，不允许直接创建 task，不允许写文件。这样可以保证“智能”被限制在结构决策，而不是变成另一个隐式 agent。

真实 LLM provider 的 prompt 必须强调三点：只能返回 JSON，不能返回解释性正文，不能引用 snapshot 之外的信息。`rationale_summary` 可以保留，但它是审计摘要，不参与状态变更。解析失败、schema 不匹配、patch 被拒绝都应该生成 `patch_rejected` event，并停止本轮 advance。

## Patch validator 细节

validator 是新架构最重要的安全边界。它应该在 apply 前基于当前 DB 状态构造一个临时 graph，然后逐个 op 模拟执行。所有 op 模拟通过后，再在一个 write transaction 中真正写入。

必须拒绝的情况包括：未知 op、未知 node_key、重复 node_key、空 title/description、未知 node_type、非法状态迁移、给 terminal node 添加会改变其语义的 op、创建自依赖、创建环、直接放行节点执行、引用不存在 artifact、assignee 明确不存在且策略要求严格 lane、`expected_revision` 与当前 graph revision 不匹配、patch op 数超过上限、patch 尺寸超过上限、同一个 patch 重复应用但不是幂等 noop。

validator 还要处理部分幂等。例如同一个 `add_dependency` 如果边已存在，可以视为 noop；同一个 `create_node` 如果 node_key 已存在且字段完全一致，可以视为 noop，但如果字段不同必须拒绝。这样 supervisor 重试不会轻易造成重复结构。

## Snapshot 压缩策略

snapshot 构造不是简单截断。它应该优先保留会影响结构决策的信息。节点层面保留 node_key、type、state、title、output_summary、verdict、artifact summaries、assumptions summary 和依赖状态；去掉长日志、完整 diff、完整 markdown。事件层面保留最近结构事件和未解决事件；老事件只进入 `history_summary`。artifact 层面保留 path/ref、type、summary、size/hash，不直接塞大内容。约束层面必须完整保留，因为丢约束会导致错误 patch。认知状态层面必须显式保留 active assumptions、rejected approaches、known failure boundaries、open questions 和 risk notes，避免 stateless decision provider 重复探索已否定路径。

后续可以实现 snapshot budget，例如 `max_nodes`、`max_events`、`max_chars_per_summary`、`max_total_chars`。如果 graph 太大，需要按 active frontier、blocked frontier、recently changed nodes 和 terminal summaries 分层压缩。第一阶段 graph 小，可以先实现简单版本，但接口要为 budget 留参数。

## 并发和锁

runtime kernel 需要防止两个 supervisor 同时 advance 同一个 job。第一阶段可以依赖 SQLite write transaction 加 job metadata 中的 `advance_lock`。更简单的做法是在 `runtime_jobs` 加 `claim_lock`、`claim_expires_at` 字段，advance 前尝试原子 claim，结束后释放。没有这个保护时，两个 advance 可能同时看到同一个 ready node，然后各自创建 Kanban task。

锁必须有 TTL，防止进程崩溃后 job 永久卡住。若发现过期锁，新的 advance 可以抢占，并记录 `advance_lock_reclaimed` event。锁 owner 可以用 `hostname:pid:uuid`。

锁解决同时执行问题，graph revision 解决过期决策问题。每次结构变更，包括 patch apply、node 创建、edge 创建、node supersede、human gate 结构更新，都应递增 job graph revision。decision provider 基于某个 snapshot revision 返回 patch，apply 时必须校验 revision。若 revision 已变化，应重新构造 snapshot 或把 patch 判定为严格 noop；不能把旧 snapshot 上的 patch 合并进新 graph。

Kanban dispatcher 的并发控制仍归 dispatcher。runtime kernel 不直接限制全局 worker 数，只决定 node 是否 ready 和是否物化。节点并行度可以通过 graph 依赖和 node/job metadata 限制，例如 `max_active_nodes`，但最终进程并发仍由 Kanban lane/max_spawn 控制。

## 失败处理

worker 失败不等于 job 失败。node failed 后，kernel 应触发一次结构决策，让 provider 决定是否创建 debug/fix node、插入验证节点、请求人工、或标记 job blocked。只有当 patch 明确 mark_blocked，或本地规则判断没有可恢复路径时，job 才进入 blocked。

decision provider 失败也不等于 job 失败。解析失败、网络失败、schema 拒绝应该记录 decision failure，然后 job 停在 `waiting_decision` 或 `blocked`，由人工重试或切换 provider。第一阶段 fake provider 不会有网络失败，但仍应测试 provider 抛异常的路径。

patch rejected 是架构正常路径，不是系统崩溃。被拒绝的 patch 和 reject_reason 必须保留，方便改 prompt 或 validator。

## 人工交互

人工不是一个特殊 worker，而是 graph 中的 human gate。`request_human` patch 创建或更新 `human_gate` 节点，并把 job 状态推到 `waiting_human`。human gate 的 payload 必须包含问题、背景摘要、候选项、推荐项、风险和超时策略。人工回答后写入 `execution_events` 的 `human_decision_received`，然后 kernel 继续 advance。

dashboard 或 CLI 可以提供 human decision endpoint，但第一阶段可以只做 DB/helper 层。重要的是人工输入也必须变成结构化 event，而不是写进聊天上下文后让某个 agent 记住。

## 完成判定

不要让 LLM 直接决定 job done。第一阶段完成规则应本地化：存在至少一个满足 objective 的 terminal result node 成功，并且所有 required verifier/review nodes 成功，且没有 active blocker/human gate/running required node，才可以把 job 置为 done。LLM 可以建议 `complete_job`，但 validator 必须用本地规则确认。

如果没有 verifier，job 不应自动 done，除非 job metadata 明确允许 `completion_policy=no_verifier_allowed`。默认策略应该偏保守。

## Dashboard 方向

新前端不应该先做旧 Orchestra 控制台。它应该显示 job graph、active frontier、node 状态、事件流、patch history、worker task 映射、artifact/evidence 摘要和 human gate。任务仍然是用户可理解入口，但详情页的核心不再是固定 phase，而是 execution graph 如何演化。

第一阶段可以只保留 API/pytest，不急着做前端。等 kernel 闭环稳定后，再从旧分支迁移 task-centered dashboard 的布局经验。迁移时 API 命名应使用 `/runtime/jobs`，不要沿用 `/orchestras`，避免概念污染。

## 与 main agent 的关系

main agent 是控制面，不是 runtime kernel。它可以创建 job、查询状态、解释当前 graph、请求人工决策、触发一次 advance 或启动 supervisor，但不应该直接改 execution graph。main agent 如果要影响结构，也应该通过同一个 graph patch API 或 human decision event 写入。

这保证了用户在聊天里发出的高层命令也会进入同一套外部状态，而不是变成聊天历史中的隐式指令。

## 第一阶段验收标准

第一阶段完成时，应该能在单元测试里证明：新分支从 `b5a262c` 起步；`runtime_jobs` 和 graph 表可以初始化；创建 job 会产生初始 analysis node；analysis node 可以物化为 Kanban task；fake evidence 可以被 ingest 成 `node_completed` 并写入 assumptions/rejected approaches/failure boundaries；fake decision provider 可以返回带 `expected_revision` 的 graph patch；patch validator 可以应用 fixture 节点和依赖；reducer 自动计算 readiness，不接受 `release_node`；过期 revision patch 会被拒绝；依赖满足后后续节点才会 ready；验证节点成功后 job 由本地规则 done；synthetic audit 能在无 runnable node 且 job 未终止时生成结构事件；所有步骤都有 execution_events 和 graph_patches 记录。

如果这个闭环没有跑通，不应该投入大量前端工作。前端只能证明展示，不能证明新架构成立。
