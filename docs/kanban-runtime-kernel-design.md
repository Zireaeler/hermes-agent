# Hermes Kanban Runtime Kernel 设计草案

本文档记录新实现线的基础设计。新分支为 `feature-kanban-runtime-kernel`，新 worktree 位于 `/tmp/hermes-agent-runtime-kernel`，基线提交为 `b5a262c fix(kanban): normalize codex receipt tails`。这个基线刻意选在旧 Orchestra 引入之前，因为它已经包含 external worker lanes、worker evidence、review/followup 和 dashboard/API 的执行底座，也包含一个可用的 Codex CLI worker backend；但 Codex 在新架构里只是可选执行单元，不是架构中心，也不应该出现在分支名里。

这个分支的目标不是继续扩展旧 `kanban_orchestra.py`，而是在 Hermes Kanban 的执行层之上新增一个目标驱动的事件溯源执行运行时。Kanban 继续负责 worker 生命周期、任务派发、lane 解析、进程启动、heartbeat、timeout、crash/retry、task event、run evidence 和 dashboard 基础可观测性；runtime kernel 负责维护外部持久化的 goal contract、progress ledger、execution graph 和 event log，并在结构性事件或目标 gap 出现时调用一次受限决策函数，生成可验证的 graph patch，再把可运行节点物化成 Kanban worker task。换句话说，Kanban 是执行系统，runtime kernel 是 goal-driven graph runtime，具体 worker backend 可以是 Codex、Claude Code、本地脚本、人工作业或后续任何可注册 lane。

## 一句话架构

系统的事实连续性只存在于数据库里的 goal contract、job、progress ledger、execution graph、event log、artifact 和 graph patch 记录中。LLM 不拥有事实状态，也不直接控制系统；每个 runtime job 可以维护一个长期 decision session 作为推理上下文和缓存友好的决策会话。kernel 基于 DB 事实生成增量 delta 并追加到 session；LLM 只能返回 graph patch proposal；patch 经 validator 校验后才能成为 DB 事实。系统的收敛条件不是“当前 execution graph 跑完”，而是“goal contract 的 required items 被足够 evidence 支持，且没有未解决 hard constraint”。

## 核心不变量

这个 runtime 的第一不变量是 DB authoritative state 和 decision session inference context 必须分离。DB 保存可恢复、可审计、可并发校验的事实；decision session 只保存非权威的长期推理上下文。session 可以帮助模型利用项目连续性、稳定前缀和前缀缓存，但不能让模型记忆覆盖 DB 事实，也不能让模型绕过 validator 写入状态。

第二不变量是 goal contract 高于 execution graph。graph 是当前工作结构，不是完成定义；当前 graph 跑完不代表 job done，当前 graph 没有 runnable node 也不代表 job blocked。job 是否完成只由 goal contract、progress ledger、hard constraints、human gates 和本地 completion rule 判定。

第三不变量是本地 reducer 拥有调度主权。节点 readiness、dependency satisfaction、job state、goal gaps、liveness violation、synthetic audit 和 completion rule 都由 kernel 根据 DB 推导。LLM 可以提出结构 patch，但不能 release node、不能直接 complete job、不能自由 mark blocked，也不能把 node_type 当成 phase 生成固定下一阶段。

第四不变量是每个结构变更必须能解释它推进了哪个 goal item、gap 或 human gate。没有 `goal_item_keys`、`gap_keys` 或 `human_gate_reason` 的新节点不是目标推进，而是自由规划，validator 必须拒绝。worker receipt 也必须能被 ingest 到 progress ledger，否则 node succeeded 不能被当作 goal progress。

## Decision Context

runtime state 和 inference context 必须分开。authoritative runtime state 落在数据库里，用于恢复、审计、并发、回滚和本地校验；decision session 是 job 级长期推理上下文，用于让 LLM 不必每次冷启动重新理解项目，同时支持长上下文、缓存友好的稳定前缀和增量追加。两者冲突时永远以 DB 为准。

decision session 可以“记得”之前为什么选择某个方案、哪些路径被否定、哪些 patch 被 validator 拒绝、当前 milestone 为什么这样切分，但这些记忆只影响下一次模型如何提出 patch，不会直接改变系统状态。真正改变 execution graph、progress ledger 或 job state 的唯一方式仍然是模型输出 patch proposal，patch 经过 validator 校验，通过后写入 DB。

目标请求形态是 `decision_provider(session, delta) -> patch`，不是每轮冷启动的 snapshot oracle。`session` 是该 job 长期保留的决策上下文，`delta` 是本轮从 DB 推导出来的新变化。第一次创建 job 时，kernel 创建 decision session，把稳定前缀写进去：runtime 规则、patch schema、validator 硬约束、用户目标、goal contract、workspace、允许的 node type、允许的 patch op、禁止直接完成 job、禁止绕过 verifier、禁止修改 terminal fact 等。后续调用只追加短 delta，例如 node 完成或失败、ledger 更新了哪些 goal item、当前最大 gap 是什么、validator 拒绝了什么。

decision session 的存在不改变调用触发策略。普通 worker progress、heartbeat、日志增长只进入 Kanban 或 progress summary；kernel 先用本地 reducer 更新 DB。只有 reducer 发现当前结构需要决策，例如目标未完成但没有 ready/running node、verifier failed、join point 需要合并/验证、同一 gap 多轮无进展、或 anti-stuck policy 触发时，才向 decision session 追加 delta 并调用 provider。

decision session 还必须有一等的 compaction lifecycle。它不是无限追加的长对话，也不是普通摘要字段。每个 job 的 decision session 应被切成多个 segment：当前 active segment 接收 delta、provider patch、validator result、patch_applied/patch_rejected、goal gap 变化和 graph revision 变化；当 compaction policy 触发时，kernel 关闭当前 segment，归档原始 transcript，生成经过 validator 校验的 checkpoint，然后开启新的 active segment。新 segment 的活跃上下文只由稳定 runtime contract、当前 goal contract、最新 checkpoint、极短 tail 和新 delta 组成；旧 segment 原文只用于审计、debug 和回放，不再继续进入活跃 LLM 上下文。

这里的边界必须明确：worker receipt 不是 decision session compaction。节点启动时定义的 artifact、verdict、验证命令、风险、失败原因、未完成项和 human gate 请求，是 worker 的交付契约。Codex/Claude Code 等 worker backend 内部也可以有自己的 `/compact` 或上下文压缩机制，但 runtime kernel 不接管。runtime kernel 的 compaction 对象只有 job 级 decision session transcript，也就是系统调度层的连续决策记录。

checkpoint 不是普通 summary，而是调度认知状态的结构化重写。它服务下一段结构决策，必须保留当前目标解释、goal contract revision、active milestone、已满足 goal items、未满足 goal gaps、open blockers、关键架构决策、已排除方案、已知失败边界、validator rejection lessons、human decisions、重要 artifact index、当前 graph frontier、当前不应重复的无效动作和下一阶段策略约束。它不应该复制每轮 patch JSON 原文或重复 delta 字段，因为这些已经落在 `graph_patches`、`kernel_decisions`、`execution_events` 和 artifact 表里。

## Goal Contract

系统最高层抽象是 goal，不是 task，也不是 execution graph。用户的自然语言目标会被规范化成一个 `runtime_job`，但 job 只是容器；真正定义系统是否应该继续推进的是 goal contract。goal contract 是系统对用户目标的结构化承诺，描述完成时必须满足哪些可验证条件、哪些约束必须保持、哪些选择可以由系统默认决定、哪些选择必须请求用户确认。

goal contract 初期应包含 `goal_items`、`constraints`、`defaults_policy`、`human_required_conditions`、`completion_policy` 和 `waivers`。`goal_items` 是可被 evidence 支持的目标条款，每个 item 至少包含稳定 key、描述、required/optional、验收标准、证据要求、是否需要 verifier、当前状态。`constraints` 表示执行中不可破坏的硬约束，例如不能修改特定路径、不能使用付费 API、不能做破坏性迁移。`defaults_policy` 说明普通工程选择如何默认推进，避免频繁询问用户。`human_required_conditions` 只覆盖真正需要授权、偏好或外部凭证的决策。

execution graph 是 goal contract 的实现结构，而不是目标本身。图里的 node 表示当前系统认为需要执行的工作单元，`execution_dependencies` 表示会影响 readiness 的调度依赖，`node_relations` 表示验证、替代、阻塞说明等语义关系。node 的产生、废弃、拆分、替代都只是为了推进 goal contract 中尚未满足的条款。当前图耗尽但 goal contract 未满足时，系统不能 idle；它必须进入 goal-gap resolution，生成新的结构、请求人工，或记录可恢复的 liveness violation。

## Progress Ledger

progress ledger 是 goal contract 和 execution graph 之间的桥。worker 完成一个 node 后，系统不能只把 node 标记为 succeeded 或 failed，还必须把 evidence 映射到 goal contract 的具体条款上。一个 node 可能完整满足某个 goal item，也可能只部分满足，或者只提供了待验证证据。

ledger entry 应记录 `goal_item_key`、`node_id`、`artifact_id`、`verifier_node_id`、`evidence_ref`、`satisfaction`、`verification_state`、`confidence`、`summary`、`created_at`。`satisfaction` 初期可以是 `none`、`partial`、`full`、`waived`、`contradicted`。`verification_state` 初期可以是 `unverified`、`self_reported`、`verified`、`failed_verification`、`needs_human`。completion rule 只能根据 ledger 判断 goal item 是否满足，不能根据 worker 自述或 node succeeded 直接判断。

没有 progress ledger，系统只能知道“做过哪些节点”，但不知道“大目标还差什么”。有了 ledger，kernel 可以从 goal contract 和 evidence 之间的差距推导下一步，而不是依赖某个长期 agent 在上下文里记得还差什么。

## Gap Detector

gap detector 是持续推进能力的核心。它是本地 reducer 的一部分，不依赖 LLM 自觉判断“是否完成”。每次 advance 时，kernel 都应从 DB 推导三个视图：当前 execution graph 状态，当前 goal contract 哪些条款已被 evidence 满足，当前还存在哪些 goal gap。

goal gap 可以来自多种情况：required goal item 没有 ledger evidence，ledger 只有 partial satisfaction，evidence 未验证，verifier 失败，hard constraint 被阻塞，human gate 未回答，当前 milestone 超预算，或者现有 graph 没有任何 runnable node 但 job 未完成。只要存在 gap，且没有合法 human gate 或 worker wait，runtime 就必须尝试推进。推进方式可以是创建 implementation node、插入 verifier node、拆分失败节点、创建 research/debug node、调整依赖，或者在确实涉及用户偏好、权限、成本、破坏性变更时创建 human gate。

kernel 的核心 liveness invariant 是：只要 job 未完成且未处于合法等待状态，就必须存在某种可推进状态，也就是 running node、ready node、pending decision、pending graph patch 或 required human gate。`no runnable node` 不是正常 idle；如果 goal contract 仍有 gap，它意味着现有图无法继续推进，接下来必须进入 gap resolution。

## 与旧 Orchestra 的边界

旧 Orchestra 的中心是阶段式 manager loop。它用固定 phase 推进任务，典型路径是 planning、plan_review、implementation、review/test、fix、done/blocked，并围绕 planner、coder、reviewer、tester 这些角色创建 invocation。这个模型适合证明“Kanban 可以调起多个 worker 并收集 evidence”，但它已经预设了协作形态。

新 runtime kernel 不预设固定角色和固定阶段。它只维护 execution graph。节点可以是 analysis、implementation、verification、debug、research、human_gate 或 artifact_transform，但这些是 node type，不是固定流程。node_type 只表示执行意图、能力需求和 worker context 类型，不表达生命周期阶段，也不允许 kernel 根据 node_type 推导固定下一阶段。节点之间的运行顺序由 `execution_dependencies` 和本地 policy 表达，验证、替代、阻塞说明等语义关系由 `node_relations` 表达。结构如何长出来，由 event 触发的 decision patch 决定。

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

`runtime_jobs` 保存一个用户复杂任务的运行容器。它对应用户最初提交的 root task 或 job description，但不直接定义完成标准。字段包括 `id`、`root_task_id`、`board`、`state`、`objective`、`workspace_path`、`decision_profile`、`active_milestone_key`、`metadata`、`created_at`、`updated_at`。`state` 初期限制为 `active`、`waiting_worker`、`waiting_decision`、`waiting_human`、`budget_paused`、`blocked`、`done`、`cancelled`、`failed`。

`goal_contracts` 保存 job 的结构化目标合同。字段包括 `id`、`job_id`、`objective`、`version`、`constraints_json`、`defaults_policy_json`、`human_required_conditions_json`、`completion_policy_json`、`metadata`、`created_at`、`updated_at`。一个 job 初期只需要一个 active contract；后续如果用户改变目标，可以创建新 version，并把旧 version 归档。

`goal_items` 保存 goal contract 的可验证条款。字段包括 `id`、`contract_id`、`item_key`、`description`、`required`、`acceptance_criteria_json`、`evidence_requirements_json`、`verifier_required`、`state`、`metadata`、`created_at`、`updated_at`。`state` 初期限制为 `open`、`partial`、`satisfied`、`waived`、`blocked`、`contradicted`。`item_key` 必须稳定，因为 graph patch、ledger 和 human gate 都会引用它。

`execution_nodes` 保存执行图节点。字段包括 `id`、`job_id`、`node_key`、`node_type`、`state`、`title`、`description`、`assignee`、`latest_task_id`、`latest_run_id`、`input_summary`、`output_summary`、`assumptions_json`、`constraints_json`、`metadata`、`created_at`、`updated_at`、`started_at`、`completed_at`。`node_key` 是 job 内稳定键，用于 graph patch 引用。`state` 初期限制为 `planned`、`ready`、`running`、`succeeded`、`failed`、`blocked`、`waiting_dependency`、`waiting_human`、`cancelled`、`superseded`。`latest_task_id` 和 `latest_run_id` 只是当前最新物化指针，不是完整执行历史。

`execution_dependencies` 保存会影响 readiness 的调度依赖。字段包括 `id`、`job_id`、`from_node_id`、`to_node_id`、`dependency_type`、`required`、`metadata`、`created_at`。`dependency_type` 初期限制为 `depends_on`、`artifact_input`。这张表必须保持 DAG，并且只有它参与 local reducer 的 ready 计算。

`node_relations` 保存不直接参与 readiness 的语义关系。字段包括 `id`、`job_id`、`from_node_id`、`to_node_id`、`relation_type`、`metadata`、`created_at`。`relation_type` 初期限制为 `verifies`、`blocks`、`supersedes`、`explains`、`replaces_attempt`。这些关系用于审计、dashboard、completion rule 和 progress ledger 解释，但不能被误当作 required dependency。若某 verifier 必须在某 node 后运行，应同时写入 `execution_dependencies` 的调度依赖和 `node_relations` 的 `verifies` 语义关系。

`node_materializations` 保存 execution node 到 Kanban task/run 的每次物化历史。字段包括 `id`、`job_id`、`node_id`、`attempt`、`task_id`、`run_id`、`worker_lane`、`status`、`created_at`、`started_at`、`completed_at`、`terminal_event_id`、`metadata`。同一个 node 可以因 retry、rerun、换 lane、worker crash 后重跑产生多次 materialization；`execution_nodes.latest_task_id/latest_run_id` 只指向最新一次。

`progress_ledger` 保存目标条款和执行证据之间的映射。字段包括 `id`、`job_id`、`contract_id`、`goal_item_id`、`node_id`、`artifact_id`、`verifier_node_id`、`evidence_ref`、`satisfaction`、`verification_state`、`confidence`、`summary`、`metadata`、`created_at`。它是 completion rule 和 gap detector 的主要输入。node terminal 不代表 goal item terminal；只有 ledger 满足证据要求，goal item 才能进入 `satisfied`。

`goal_gaps` 可以作为持久化 gap 视图，也可以由 reducer 动态推导。若持久化，字段包括 `id`、`job_id`、`goal_item_id`、`gap_key`、`gap_type`、`state`、`summary`、`evidence_ref`、`last_attempt_node_id`、`attempt_count`、`metadata`、`created_at`、`updated_at`。`gap_type` 初期限制为 `missing_evidence`、`partial_satisfaction`、`unverified_evidence`、`failed_verifier`、`blocked_constraint`、`stalled_progress`、`human_required`、`no_runnable_graph`。

`milestones` 保存 goal contract 派生的局部工作窗口。字段包括 `id`、`job_id`、`milestone_key`、`title`、`goal_item_keys_json`、`state`、`budget_json`、`metadata`、`created_at`、`updated_at`。milestone 只能控制当前推进焦点，不能定义 job 完成条件，也不能退化成 planning/implementation/review 这种预设 phase。

`execution_events` 保存结构性事件流。字段包括 `id`、`job_id`、`node_id`、`task_id`、`run_id`、`event_type`、`payload`、`source`、`source_event_id`、`graph_revision`、`created_at`。这里不是把所有 Kanban task_events 原样复制一遍，而是只记录 kernel 关心的结构性事件，例如 `job_created`、`goal_contract_created`、`goal_gap_detected`、`progress_ledger_updated`、`node_created`、`node_materialized`、`node_started`、`node_progressed`、`node_completed`、`node_failed`、`node_uncertain`、`node_blocked`、`artifact_ready`、`dependency_satisfied`、`decision_requested`、`patch_applied`、`patch_rejected`、`human_required`、`human_decision_received`、`structure_audit_requested`、`liveness_violation`。

`graph_patches` 保存每次结构修改。字段包括 `id`、`job_id`、`decision_id`、`base_revision`、`applied_revision`、`patch_json`、`status`、`reject_reason`、`created_at`、`applied_at`。`status` 限制为 `proposed`、`applied`、`rejected`、`noop`。`base_revision` 是 decision provider 看到的 graph revision，patch 默认只能应用到相同 revision 上。

`kernel_decisions` 保存每次决策函数调用。字段包括 `id`、`job_id`、`trigger_event_id`、`db_revision`、`decision_session_id`、`delta_json`、`decision_json`、`model`、`status`、`validator_result_json`、`error`、`created_at`、`completed_at`。`delta_json` 是本次追加到 decision session 的 DB-derived state delta，不是完整事实快照。第一阶段可以用 deterministic fake decision provider，后续再接真实 LLM。

`decision_sessions` 保存受外部事实约束的 job 级长期决策上下文容器。字段包括 `id`、`job_id`、`profile`、`provider`、`model`、`state`、`stable_prefix_hash`、`session_ref`、`transcript_ref`、`active_segment_id`、`last_appended_event_id`、`last_checkpoint_revision`、`context_state_json`、`metadata`、`created_at`、`updated_at`。`session_ref` 可以是 provider 侧会话 id，也可以为空；`transcript_ref` 可以指向本地归档的 session transcript。架构只要求它是 job 级长期决策上下文容器，不要求绑定某个模型 API。

`decision_session_segments` 保存 decision session 的分段生命周期。字段包括 `id`、`job_id`、`decision_session_id`、`segment_index`、`state`、`started_at`、`closed_at`、`start_decision_id`、`end_decision_id`、`covered_event_start`、`covered_event_end`、`covered_graph_revision_start`、`covered_graph_revision_end`、`estimated_input_tokens`、`estimated_output_tokens`、`compacted_checkpoint_id`、`archive_ref`、`metadata`。`state` 初期限制为 `active`、`closed`、`archived`、`compacting`、`compacted`、`failed_compaction`。同一个 job 同时只能有一个 active segment。

`decision_segment_entries` 保存 active segment 的 append-only transcript 条目。字段包括 `id`、`segment_id`、`job_id`、`entry_type`、`decision_id`、`event_id`、`patch_id`、`graph_revision`、`payload_json`、`estimated_tokens`、`created_at`。这是 compaction 的直接输入，不应只依赖从 `kernel_decisions`、`graph_patches` 和 `execution_events` 事后拼接 transcript。`entry_type` 初期可以包括 `delta_appended`、`provider_output`、`patch_parsed`、`validator_result`、`patch_applied`、`patch_rejected`、`compaction_requested`、`checkpoint_created`。

`decision_checkpoints` 保存 decision session compaction 生成的结构化检查点。字段包括 `id`、`job_id`、`decision_session_id`、`source_segment_id`、`profile_name`、`profile_version`、`profile_hash`、`profile_path`、`checkpoint_revision`、`db_revision`、`graph_revision`、`ledger_revision`、`covered_event_start`、`covered_event_end`、`covered_decision_start`、`covered_decision_end`、`covered_entry_start`、`covered_entry_end`、`payload_json`、`payload_text`、`validator_status`、`reject_reason`、`supersedes_checkpoint_id`、`transcript_ref`、`created_at`。`payload_json` 是机器可读的结构化状态，`payload_text` 是给 provider 阅读的紧凑文本版本。checkpoint 必须绑定 DB/graph/ledger revision，避免压缩上下文和事实源脱节。profile 是热插拔 markdown，因此 checkpoint 必须记录 profile version/hash/path，保证后续审计和回放能知道它由哪个压缩契约生成。

`node_artifacts` 保存节点产物引用。字段包括 `id`、`job_id`、`node_id`、`artifact_type`、`path_or_ref`、`summary`、`metadata`、`created_at`。这里可以引用 worker 产生的文件、evidence markdown、测试结果、diff 摘要、外部工具结果，但事实状态仍以 DB 行为准。

## Kernel 循环

kernel 是可重复调用的函数，不是长时间思考的 agent。第一阶段可以实现为 `advance_runtime_job(conn, job_id, *, board=None, decision_provider=None, max_patches=1)`，后续再包一层 `supervise_runtime_job()` 做 bounded loop。

每次 advance 做六件事。第一，ingest Kanban worker 状态，把已物化节点绑定的 `task_id` 通过 `task_progress_snapshot()` 读取，识别是否完成、失败、阻塞或产生结构性进展，并写入 `execution_events`。第二，把 worker evidence 映射到 goal items，更新 progress ledger，而不是只更新 node state。第三，运行本地 reducer，重算节点 readiness、job 状态、goal gaps、synthetic audit event、liveness invariant 和 completion rule；所有依赖满足的 `planned` 或 `waiting_dependency` 节点由 reducer 变为 `ready`。第四，把 `ready` 且尚未创建 Kanban task 的节点物化成真实 Kanban task，创建 `node_materializations` 行，写回 `latest_task_id`、`latest_run_id`，并记录 `node_materialized`。第五，如果存在 unresolved goal gap、structure audit、failed verifier、no runnable graph 或 manual operator request，就从 DB 构造本轮 state delta，追加到 decision session，并调用 decision provider 取得 graph patch。第六，验证并应用 graph patch，然后再次运行 reducer 更新 ledger、gap 和可运行节点；patch_applied 或 patch_rejected 也要作为 delta 追加回 decision session。

这个循环的关键是不让 LLM 直接改数据库，也不让 LLM 决定节点放行。LLM 或 fake provider 只能返回 patch JSON。所有 patch 都必须经过本地 validator。validator 负责检查引用的 node_key 是否存在、是否重复创建、调度依赖是否成环、状态迁移是否合法、node type 是否允许、assignee/lane 是否存在或可延后、patch 是否幂等、是否越权修改已完成节点、patch 的 `expected_revision` 是否仍匹配当前 graph revision。

## Decision Delta And Checkpoint

decision provider 的输入不是随意拼接的完整数据库，也不是每次冷启动的完整 prompt。稳定项目上下文保存在 decision session 中，本轮输入是由 kernel 从 DB 推导出的 state delta。为了审计、调试和 provider fallback，kernel 可以构造一个规范化 snapshot，但 snapshot 不是主路径输入；主路径是 delta append、session continuation 和 checkpoint compaction。

初始 session 前缀应包含这些稳定内容。

`job` 包括 objective、state、workspace、当前未解决约束、用户可见目标、active milestone 和 graph revision。

`goal_contract` 包括 required/optional goal items、acceptance criteria、evidence requirements、hard constraints、default policy、human-required conditions 和 completion policy。

`graph` 包括节点列表、节点状态、关键调度依赖、关键语义关系、每个节点的一句话输入输出摘要、最近 verdict、是否有 artifact。

`progress_ledger` 包括每个 goal item 当前由哪些 node/artifact/verifier 支持，满足程度是什么，验证状态是什么，证据是否足够。

`goal_gaps` 包括仍未满足、部分满足、未验证、被阻塞或停滞的目标条款。decision provider 的任务是提出解决这些 gap 的 graph patch，而不是自由扩展图。

`recent_events` 只取最近 N 条结构性事件，而不是 worker 全量日志。每条事件保留 event_type、node_key、summary、payload 中的关键字段。

`open_constraints` 保存当前没有解决的约束，例如缺少依赖信息、测试失败、需要人工选择、外部资源不可用、需要并行拆分。

`active_assumptions` 保存当前仍被系统采用的压缩假设，例如“目标仓库路径已确认”“失败主要来自认证而非代码逻辑”。这些不是推理链，而是后续结构决策不能丢的事实状态。

`rejected_approaches` 保存已经尝试并排除的方案，例如“直接复用旧 Orchestra phase machine 会造成语义污染”。它应包含简短原因和证据引用，避免 decision session 重复生成失败路径。

`known_failure_boundaries` 保存失败边界和不可越过约束，例如“当前没有 GitHub HTTPS 凭据”“某 lane 不可 spawn”“某验证命令在环境中不可用”。这些边界进入 decision delta 或 checkpoint 后，provider 才能做局部结构调整，而不是反复要求执行不可行动作。

`open_questions` 和 `risk_notes` 保存仍需人工或后续节点解决的问题。它们应该来自 worker receipt、human gate 或 synthetic audit，而不是从完整 worker 对话里临时抽取。

`available_actions` 明确告诉决策函数只能返回哪些 patch op。第一阶段 action 集合越小越好。

`policy` 描述本地硬规则，例如不能删除已完成节点、不能绕过 verifier、不能直接标记 job done、不能创建没有 title/description 的节点。

本轮 delta 应只包含新变化和待决策问题，例如新增 terminal node、artifact_ready、progress_ledger_updated、goal_gap_detected、validator rejection、当前没有 ready/running node、或 active milestone 停滞。delta 必须说明这些变化影响了哪些 goal item、哪些 gap 仍未解决、为什么需要结构决策。

delta、checkpoint 和审计 snapshot 都要足够小，可以进入一次 LLM 调用；但必须包含 graph frontier、goal gaps 和未解决约束，否则决策函数会退化成自由规划。这里的 snapshot 是审计和 fallback 用的规范化视图，不是每次调用的主要输入；主要输入仍然是追加到 decision session 的 delta。动态字段不要放到稳定前缀中，例如当前时间、随机 id、最近事件列表、节点运行状态变化都应靠后追加。graph、goal item 和 ledger 的渲染顺序必须 canonicalize，例如按 `node_key`、`goal_item_key` 排序，并使用固定字段顺序，避免同样内容因为排序变化破坏缓存命中。

## Decision Session Compaction Runtime

Decision Session Compaction Runtime 是 runtime kernel 的一等子系统，位置在真实 LLM provider 接入之前。它的职责不是总结 worker 日志，也不是生成 dashboard summary，而是管理 job 级 decision session transcript 的分段、压缩、checkpoint 生成、旧上下文归档和新上下文启动。

一个 job 创建时，kernel 创建 decision session 和第一个 active segment。active segment 的前缀包含稳定 runtime contract、patch schema、validator 规则、goal contract、workspace 和必要初始目标信息。每次结构决策时，kernel 把 DB-derived delta 追加到 active segment；provider 返回 patch 后，validator result、patch_applied 或 patch_rejected、graph revision 变化和 goal gap 变化也追加进去。

当 compaction policy 触发时，kernel 执行真正的上下文替换，而不是在旧上下文后面追加摘要。流程是：第一，关闭 active segment 并把它标记为 compacting/archived；第二，调用 compaction provider 或 deterministic fallback 生成 checkpoint candidate；第三，用 checkpoint validator 校验引用和事实一致性；第四，checkpoint 通过后写入 `decision_checkpoints`；第五，开启新的 active segment。新 segment 的上下文由稳定 runtime contract、当前 goal contract、最新 checkpoint、极短 tail 和本轮新 delta 组成，旧 segment 原文不再进入活跃 LLM 上下文。

compaction provider 和 decision provider 是两条不同接口。普通结构决策接口是 `decision_provider(session_segment, delta) -> graph_patch_proposal`，目标是推进 execution graph。压缩接口是 `compaction_provider(segment, db_state, profile, budget) -> checkpoint_candidate`，目标是重写下一阶段需要保留的调度认知状态。两者不能混用；compaction provider 不能提出 graph patch，decision provider 不能替代 checkpoint lifecycle。

compaction policy 不应该写死在内核里。kernel 可以提供默认 policy，但触发因素应该来自可配置策略函数。输入信号包括 active segment 估算 token 超过模型窗口比例、最近 N 次 decision token 增长过快、cacheable prefix ratio 下降、milestone 切换、human decision 修改 goal contract、validator 连续拒绝、同一个 gap 多轮 strategy update、graph 大规模 supersede、active frontier 从 implementation 转向 verification。系统还应记录 telemetry：`stable_prefix_tokens`、`checkpoint_tokens`、`tail_tokens`、`delta_tokens`、`model_output_tokens`、`active_segment_tokens`、`cacheable_prefix_tokens`、`context_window_ratio`、`accepted_patch_count`、`rejected_patch_count`、`noop_count`。

这些 telemetry 不是单纯展示字段，而是 `should_compact_decision_session()` 这类 policy 函数的正式输入。实现上可以先用简单阈值，但接口必须把 telemetry 作为 policy evaluation 的参数，避免后续 compaction policy 只能靠硬编码常量扩展。

compaction prompt 必须 profile 化，而不是写死在代码里。建议新增 `docs/kanban-runtime-kernel-compaction-profiles/` 或运行时配置目录，每个 profile 是 markdown 文件。初期 profile 可以包括 `token_budget_compaction.md`、`validator_boundary_compaction.md`、`human_decision_compaction.md`、`milestone_transition_compaction.md`、`anti_stuck_compaction.md`。profile 应声明用途、输入选择规则、压缩目标、禁止事项、输出 schema、校验要求和示例。kernel 不解释 profile 的自然语言内容，只负责选择 profile、组装输入、调用 provider、验证 checkpoint。

checkpoint validator 不判断压缩是否聪明，而判断它是否安全且不违背 DB 事实。validator 必须检查 checkpoint 中引用的 `node_key`、`goal_item_key`、`artifact_ref`、`patch_id`、`human_decision_id` 是否存在；检查它是否把未验证事项写成 confirmed；检查它是否遗漏当前 hard blocker；检查它是否和 DB 当前 graph/ledger revision 冲突；检查它是否把 failed verifier 写成 passed。校验失败的 checkpoint 不能成为新 active segment 的前缀，可以重试、换 profile，或降级为 deterministic DB-derived checkpoint。

checkpoint payload 中每个结论项都必须带 provenance。`satisfied_goal_items`、`open_goal_gaps`、`open_blockers`、`key_decisions`、`rejected_approaches`、`known_failure_boundaries`、`validator_rejection_lessons`、`human_decisions`、`artifact_index`、`do_not_repeat` 等条目都应包含 source refs，例如 `event_id`、`decision_id`、`patch_id`、`goal_item_id`、`ledger_entry_id`、`artifact_ref`、`node_key` 或 `human_decision_id`。没有 provenance 的 checkpoint 结论只能作为非权威 note，不能被 validator 当作事实，也不能用于覆盖 DB-derived state。

`kernel_decisions.snapshot_json` 或等价字段的语义应统一成“本次追加到 active decision session 的 delta”，而不是“本次完整压缩快照”。delta 应包含 trigger_reason、db_revision、graph_revision、goal_gap_delta、recent_structural_events、frontier_change、available_actions 和 request_boundary。长期上下文来自 active segment 与最新 checkpoint；审计来自 segment archive、kernel_decisions、graph_patches 和 execution_events。

compaction 可观测性至少要能回答：当前 active segment id 是什么；最新 checkpoint revision 是什么；active segment token 估算是多少；最近一次 compaction profile 是什么；最近一次 compaction validator 是否通过；旧 segment archive_ref 在哪里；当前 provider 输入由 stable prefix、goal contract、checkpoint、tail 和 delta 哪几部分组成。dashboard 可以后做，但 CLI/API JSON 必须预留这些字段。

short tail 必须被严格限制。它只能包含最新 checkpoint 覆盖范围之后尚未被 checkpoint 吸收的 `decision_segment_entries`，并同时受 `max_tail_entries` 和 `max_tail_tokens` 约束。compaction 成功后，旧 segment 原文不能以“最近有用”为理由重新进入 provider input；否则 compaction 会退化成追加摘要而不是上下文替换。

## Graph Patch schema

第一阶段只支持最小 patch 集合。

`create_node` 创建一个新 execution node。必填 `node_key`、`node_type`、`title`、`description`，并且必须至少提供 `goal_item_keys`、`gap_keys` 或 `human_gate_reason` 中的一种。可选 `assignee`、`constraints`、`depends_on`。

`add_dependency` 添加一条调度依赖。必填 `from_node_key`、`to_node_key`，可选 `dependency_type`，默认是 `depends_on`。该 op 只写 `execution_dependencies`，必须保持 DAG。

`insert_verifier` 为某个节点或 goal item 创建验证节点，并自动添加 `target -> verifier` 的调度依赖，同时写入 `node_relations.verifies` 语义关系。

`split_node` 把一个未开始或失败待重构节点拆成多个子节点，并把原节点标记为 `superseded`。第一阶段可以先不实现这个 op，而是用 create_node + add_dependency 替代。

`propose_blocked` 请求把 job 或 node 置为 blocked，但不能直接改变终态。必填 `target`、`blocker_type`、`reason`、`evidence_ref`，可选 `goal_item_keys`、`gap_keys`。`blocker_type` 初期限制为 `missing_secret`、`external_permission`、`destructive_change_needs_approval`、`unavailable_dependency`、`system_error`、`policy_violation`。最终 blocked 状态只能由本地 reducer/validator 在确认不可默认推进后写入。

`request_human` 写入 human gate，要求提供问题、候选项、默认建议和风险说明。

`complete_job` 不能由 LLM 直接生效。第一阶段建议不开放给 LLM patch，只允许本地 completion rule 在所有 required goal items 都被 progress ledger 的足够 evidence 支持、required verifier 成功、且没有 active blocker/human gate/running required node/contradicted ledger entry 时自动完成。后续如果允许 provider 提议 complete，也只能作为建议，由 validator 用同一套本地规则接受或拒绝。

v1 明确不提供 `release_node`。节点是否从 `planned` 或 `waiting_dependency` 进入 `ready`，只能由本地 reducer 根据 dependency、node state、policy、lock、lane availability 和 human gate 状态计算。decision provider 可以改变图结构和约束，但不能直接放行执行。

每个 patch 必须包含 `expected_revision`。如果当前 graph revision 已经变化，validator 必须拒绝该 patch，或在所有 op 都已经等价存在时将其规范化成 noop；不能把基于过期 revision 生成的 patch 盲目 merge 到当前 graph。

patch 示例形状如下：

```json
{
  "schema": "runtime_graph_patch_v1",
  "expected_revision": 7,
  "rationale_summary": "goal item market-data-provider has partial evidence but still lacks independent verification",
  "ops": [
    {
      "op": "create_node",
      "node_key": "verify-unit-tests",
      "node_type": "verification",
      "title": "Run focused unit tests",
      "description": "Run the tests named by implementation evidence and report pass/fail.",
      "goal_item_keys": ["provider-behavior-verifiable"],
      "gap_keys": ["provider-tests-unverified"],
      "assignee": "test-worker",
      "depends_on": ["implement-core"]
    },
    {
      "op": "insert_verifier",
      "target_node_key": "implement-core",
      "verifier_node_key": "review-diff",
      "title": "Review implementation diff",
      "goal_item_keys": ["implementation-quality-reviewed"],
      "gap_keys": ["diff-review-missing"],
      "assignee": "review-worker"
    }
  ]
}
```

## 首个可跑切片

第一阶段目标不是做完整智能系统，而是跑通闭环。具体切片如下。

用户或 CLI 创建 root Kanban task，再创建 `runtime_job`、goal contract、required goal items、decision session 和一个初始 `analysis` node。该 node 被物化成 Kanban task，派给配置中的分析 lane 或测试里的 fake worker lane，并创建一条 `node_materializations` 记录。worker 完成后写入 evidence。kernel ingest evidence，把 analysis node 标成 succeeded，记录 `node_completed`，把 worker evidence 映射到 progress ledger，并由 gap detector 推导仍未满足的 goal gap。reducer 如果发现目标未完成且现有图需要扩展，就写入 `decision_requested`，构造 state delta 追加到 decision session。fake decision provider 基于 delta 返回 patch，创建一个服务于 gap 的 `implementation` node 和一个 `verification` node，并建立 implementation 到 verification 的调度依赖和 verifies 语义关系。kernel 应用 patch，把 implementation 物化成 Kanban task。implementation 完成后，local reducer 根据依赖满足情况把 verification node 推到 ready；verification 通过后，ledger 把对应 required goal item 标成 satisfied，最后由本地 completion rule 把 job 标成 done。

这个切片证明四件事：execution graph 是数据库事实源，worker 只是节点执行器，LLM/decision provider 只返回 patch，Kanban 继续承担真实执行生命周期。

这个切片只是 deterministic fixture，用来证明 goal contract、progress ledger、graph patch、Kanban task materialization、evidence ingest、decision session delta 和本地 completion rule 能跑通。它不是默认任务模板，也不允许实现中硬编码 `analysis -> implementation -> verification`。初始 `analysis` 节点只能建立可执行理解和压缩状态，不能输出完整固定计划链；后续结构必须来自 reducer 生成的结构事件、decision session delta 和受限 patch。

## API 初稿

CLI/API 第一阶段可以非常薄。

`create_runtime_job(conn, root_task_id, objective, board=None)` 创建 job、goal contract 和初始 analysis node。

`status_runtime_job(conn, job_id)` 返回 job、goal contract、progress ledger、goal gaps、nodes、execution dependencies、node relations、recent_events、pending_decisions、mapped Kanban task progress。

`advance_runtime_job(conn, job_id, board=None, create_tasks=True, decision_provider=None)` 执行一次 kernel tick。

`supervise_runtime_job(conn, job_id, mode="once"|"loop", max_steps=None, interval=5.0)` 对一个 job 做 bounded loop，遇到 waiting_worker、waiting_human、done、合法 blocked、max_steps 即停。若 job 未完成且没有 worker/human/pending decision，却因为没有 runnable node 想停止，应记录 liveness violation，而不是正常退出。

`apply_graph_patch(conn, job_id, patch, decision_id=None)` 校验并事务应用 patch。

`build_decision_delta(conn, job_id, trigger_event_id=None)` 构造本轮追加到 decision session 的 state delta。

`checkpoint_decision_session(conn, job_id, decision_session_id)` 压缩长期 decision context，生成新的稳定 checkpoint。

`ingest_runtime_node_evidence(conn, node_id, board=None)` 从 Kanban task progress snapshot 吸收结构性结果。

`detect_goal_gaps(conn, job_id)` 从 goal contract、progress ledger 和 graph state 推导当前未满足目标差距。

`update_progress_ledger(conn, node_id, evidence)` 把 worker evidence 映射到 goal items，并记录 satisfaction 和 verification state。

dashboard API 后续再暴露 `/runtime/jobs`、`/runtime/jobs/{id}`、`/runtime/jobs/{id}/advance`、`/runtime/jobs/{id}/events`、`/runtime/jobs/{id}/graph`、`/runtime/jobs/{id}/patches`。第一阶段可以先不做前端，只做 CLI 和 pytest。

## 与 Markdown 的关系

DB 是唯一事实源。Markdown 只能作为 artifact、人读 handoff、evidence 摘要或审计文档。kernel 不应该依赖“读某个 agent memory md”来判断状态。需要进入决策的内容必须先被摘要进 DB、decision delta、checkpoint 或 artifact summary。worker 可以写 markdown evidence，但 ingest 后要把 verdict、changed files、verification、artifact refs、summary 写入结构化字段。

## 触发策略

不是所有 worker 事件都触发决策，也不是所有 DB 变化都追加到 decision session。worker streaming/progress event、heartbeat、普通日志增长只进入 Kanban event 或 progress summary，不直接唤醒 decision provider。Codex JSON event 如果作为某个 lane 的进度事件出现，也按这个规则处理。kernel 先用本地 reducer 更新 DB、node state、dependency readiness、progress ledger 和 goal gaps。只有 reducer 发现“当前结构需要决策”时，才构造 delta 并追加到 decision session，例如目标未完成但没有 ready/running node、verifier failed 需要选择 retry/debug/split/human、多个并行节点到达 join point、同一 gap 多轮没有 progress、或 anti-stuck policy 触发。

`node_completed`、`node_failed` 和 `node_blocked` 本身不是 decision trigger；它们只是 reducer 输入。reducer 可以只释放已有 verifier node、更新 ledger、或生成 synthetic audit event。只有 reducer 写出 `decision_requested`、`goal_gap_detected`、`structure_audit_requested`、`liveness_violation` 或需要 human gate 时，才调用 decision session。

这样可以避免每个微小输出都触发 LLM 重算，也能保证系统成本和状态变化可解释。

显性事件之外，还需要 synthetic audit。runtime reducer 应在本地生成少量结构审计事件，例如 active frontier 长时间没有推进、所有节点都 succeeded 但 open_constraints 没减少、同类失败重复出现、缺少 required verification、没有 runnable node 但 job 也不是 blocked/done、patch 连续被拒绝、或 graph revision 长时间不变化。这类事件可以写成 `structure_audit_requested` 或 `node_uncertain`，再触发一次受限决策。synthetic audit 的作用是发现静默失败，而不是把普通 progress event 升级成 LLM 触发器。

## 测试策略

第一批测试应全部使用 deterministic fake decision provider 和 fake worker evidence，不依赖任何真实外部 agent 或网络调用。测试需要覆盖 schema 初始化、job 创建、初始 node 创建、patch validator、DAG cycle rejection、ready 计算、Kanban task materialization、evidence ingest、decision delta、decision session append、patch application、job done rule。还应覆盖这些核心不变量：`release_node` 不在 LLM ops 中；没有 goal/gap linkage 的 `create_node` 被拒绝；`propose_blocked` 缺少合法 `blocker_type` 被拒绝；stale revision patch 被拒绝；同一 ready node 在两个 supervisor 下不会重复 materialize；node completed 不直接触发 LLM，而是由 reducer 决定是否 `decision_requested`；human gate 只有 policy 允许时产生。

第二批测试再接入 existing worker lane fixtures，验证 runtime node 物化后能走 `dispatch_once()`、worker receipt 能被 `task_progress_snapshot()` 读取、kernel 能把 worker evidence 转成 node event。这里可以覆盖 Codex lane，但测试目标是 lane/backend abstraction，不是 Codex 专用链路。

真实 Codex smoke 或其他真实 agent smoke 只作为后续集成验证，不作为第一阶段单测前提。

## 实现顺序

第一步新增 `kanban_runtime_kernel.py`，只包含 schema、dataclass、create/status/list helpers。第二步实现 goal contract、goal items 和 progress ledger 的创建/查询。第三步实现 patch schema 和 validator，不接 LLM，并确保 v1 patch 不包含 `release_node`，且新 node 必须关联 goal item、gap 或 human gate reason。第四步实现 `create_runtime_job()`、初始 goal contract 和初始 analysis node。第五步实现 reducer、gap detector、liveness invariant 和 node materialization，把本地规则计算出的 ready node 变成 Kanban task。第六步实现 evidence ingest，从 task snapshot 更新 node、assumptions、progress ledger 和 event。第七步实现 deterministic decision provider，跑通 fixture graph 闭环，但不得把 fixture 写成默认流程。第八步加 CLI/API 薄封装。第九步再考虑 dashboard。

这个顺序的原则是先证明状态机和 graph patch，而不是先做 UI 或真实 LLM。

## 分支约束

本设计属于 `feature-kanban-runtime-kernel`。不要在这个分支例行 rebase main。不要把旧超大 session 作为运行上下文恢复。当前旧 Orchestra 分支可以保留为参考和部署验证记录，但新实现线应保持概念干净：Kanban 是执行基座，runtime kernel 是 goal-driven graph runtime，decision session 是受 DB 约束的推理上下文，具体 worker backend 是可替换执行单元。

## 运行进程形态

第一阶段不需要引入新的常驻服务。runtime kernel 可以先作为 CLI/API 调用中的同步函数存在，和现有 Kanban dispatcher 解耦。推荐的最小运行形态是三个循环并存，但职责分开。

第一个循环是现有 Kanban dispatcher daemon。它只看 Kanban `tasks` 表中可 dispatch 的 task，负责 claim 和 spawn worker。它不理解 execution graph，也不直接调用 decision provider。

第二个循环是 runtime supervisor。它可以先不是 daemon，而是一个 bounded loop：每次调用 `advance_runtime_job()`，遇到等待 worker、等待人工、完成、合法 blocked 或达到 max_steps 就停。若只是当前 graph 没有 runnable node，但 goal contract 仍有 unmet item，则不能按阻塞退出，必须记录 liveness violation 并触发 gap resolution。后续可以做成 dashboard/API 触发的后台线程，但第一阶段不需要。

第三个循环是 worker 自己的执行过程。worker 接收一个 Kanban task，只知道本节点的局部上下文、依赖输出摘要、约束和 expected receipt shape。worker 不知道全局 graph，也不和其他 worker 直接通信。

工程上必须避免把这三个循环揉成一个“智能总控 daemon”。dispatcher 维护执行生命周期，runtime supervisor 维护图结构，worker 维护单节点执行。三者通过 DB 和事件行通信。

## 任务创建入口

新系统应支持两个入口。第一个入口是已有 root Kanban task，即用户先创建一个普通 Kanban task，再把它提升为 runtime job。这个入口适合兼容当前 dashboard 和 CLI。第二个入口是从 goal/job description 直接创建 runtime job，同时自动创建 root Kanban task。这个入口适合未来 main-agent 控制面。

无论入口如何，创建后都必须得到一个 `runtime_jobs` 行、一个 root-level `execution_nodes` 初始节点，以及一条 `job_created` 事件。初始节点不应该是完整计划，而应该是 `analysis` 或 `understanding` 类型，目的只是建立当前问题的可执行理解。这样系统不会在第一步就陷入一次性大规划。

## 表结构细化

下面是第一阶段 schema 的建议细节。最终代码可以按 SQLite 约束能力调整，但字段语义应保持稳定。

`runtime_jobs.id` 使用短文本 id，例如 `rjob_<hex>`。`root_task_id` 对应 Kanban root task，可以为空只在极早期创建事务中短暂存在，提交后必须有值。`board` 固化创建时所在 board，后续所有 Kanban 读取都必须传入这个 board，避免跨 board 混读。`objective` 是用户目标的规范化文本。`state` 是 job 级运行状态，不等于所有 node 状态的简单聚合，也不等于目标完成状态；它应由 goal contract、ledger、gap、node/event 共同推导更新。`decision_profile` 是决策函数配置名，可以映射到真实 LLM provider、fake provider 或禁用策略。`active_milestone_key` 用于聚焦当前推进窗口。`metadata` 用于存储非核心扩展，例如 `last_event_cursor`、`last_decision_id`、`human_gate`、`completion_rule`、`graph_revision`。

`execution_nodes.node_key` 必须在同一个 job 内唯一，并且是 graph patch 的稳定引用。它不能用自增 id 暴露给 LLM，因为 LLM 更适合引用语义键，例如 `understand-scope`、`implement-parser`、`verify-regression`。`node_type` 表示执行意图和能力需求，第一阶段只开放 `analysis`、`implementation`、`verification`、`review`、`debug`、`human_gate`、`artifact_transform`。这些名称不能被解释成固定 phase，也不能触发固定 next step。`assignee` 是 Kanban assignee/lane 名，可以为空；为空时 kernel 可以根据显式配置的 node_type-to-lane policy 选择默认 lane，或者停在 `blocked`/`waiting_human` 要求人工指定，但不能把 lane policy 扩展成流程模板。`latest_task_id` 和 `latest_run_id` 是最新 Kanban 映射，完整历史在 `node_materializations`。`input_summary` 是给 worker 的局部输入摘要，`output_summary` 是 ingest 后的结构化输出摘要。`assumptions_json` 保存当前节点贡献的压缩认知状态，建议包含 `active_assumptions`、`rejected_approaches`、`known_failure_boundaries`、`open_questions`、`risk_notes`。`constraints_json` 保存硬约束，例如必须运行哪些测试、不能修改哪些路径、需要人工确认哪些外部操作。

`execution_dependencies` 中 `depends_on` 表示 from 成功后 to 才能 ready，`artifact_input` 表示 to 需要读取 from 的 artifact。只有这张表参与 readiness。`node_relations` 中的 `verifies` 表示某节点验证另一个节点的结果，`supersedes` 表示新节点替代旧节点，`blocks` 表示一个节点揭示了另一个节点或 goal gap 的阻塞原因。语义关系不能自动阻塞调度，除非 reducer 或 validator 基于 policy 显式创建调度依赖。

`execution_events` 是 kernel 的事件流，不是日志垃圾桶。每条 event 应该有机器可读 payload，并尽量包含一行 `summary`。如果来自 Kanban task_event，`source_event_id` 指向原事件 id，保证 ingest 幂等。对于同一个 `source_event_id` 和 `event_type`，应有唯一性保护或代码层去重。

`kernel_decisions.delta_json` 保存本次追加到 decision session 的 state delta，而不是完整数据库 dump。`db_revision` 保存该 delta 对应的 DB/graph revision。`decision_json` 保存 provider 原始返回。即使 patch 被拒绝，decision 也要保留，方便解释为什么被拒绝。`status` 建议为 `started`、`completed`、`failed`、`rejected_patch`。

`graph_patches.patch_json` 保存规范化后的 patch。规范化意味着默认值已补齐、node_key 已 trim、op 顺序已确定、不可识别字段已拒绝或移入 metadata。这样后续审计时不依赖 provider 的原始输出。`base_revision` 必须来自 patch 的 `expected_revision`；成功 apply 后递增 job 的 graph revision，并把新 revision 记录到 `applied_revision`。

## 状态迁移细节

`runtime_jobs` 的状态建议按本地规则更新。创建后是 `active`。如果存在 running node，则是 `waiting_worker`。如果没有 running node，但存在可运行未物化 node，则仍是 `active`，下一次 advance 应物化它们。若存在 goal gap 且需要结构决策，可以进入 `waiting_decision`。若存在 active human gate，则是 `waiting_human`。达到运行预算但状态可恢复时进入 `budget_paused`。所有 required goal items 被 ledger evidence 满足并通过 completion rule 后进入 `done`。无法继续且需要人工修复系统状态时进入 `blocked`。代码异常或 provider 调用失败不应直接让 job failed，除非错误不可恢复；一般应记录 `decision_failed` 或 `patch_rejected`，然后停在 `waiting_decision`、`blocked` 或 `waiting_human`。

`execution_nodes` 的状态迁移必须严格。`planned` 可以到 `waiting_dependency` 或 `ready`。`waiting_dependency` 在依赖满足后到 `ready`。`ready` 物化成 Kanban task 后到 `running`。`running` 根据 ingest 到 `succeeded`、`failed`、`blocked`、`waiting_human`。`failed` 可以通过 patch 被 `superseded`，也可以创建 debug/fix 节点依赖它，但不应该原地改回 `ready`，除非这是明确的 retry op。`succeeded` 默认不可修改，只能被后续 verifier 判定为需要补充工作，但不能直接篡改原节点结果。

这些 state 是 materialized execution view，不是任务 phase。它们描述节点是否已计划、是否可运行、是否正在被某个 worker 执行、是否已有终态 evidence。真正的调度语义来自 graph、execution dependencies、node relations、policy 和 local reducer，而不是来自 `state + node_type` 的手写流程条件。实现中应避免出现“某 state 下某 node_type 固定生成某下一节点”的规则；这类结构变化只能来自 patch 或明确的本地安全规则。

Kanban task 状态和 node 状态的映射不能一一硬绑。Kanban `done` 通常对应 node `succeeded`，但如果 evidence verdict 是 `needs_fix`，node 应是 `failed` 或 `blocked`。Kanban `blocked` 对应 node `blocked`，但如果 block reason 是等待人工输入，node 应是 `waiting_human`。Kanban `running` 对应 node `running`。Kanban `ready` 只应该短暂存在于已物化但未 claim 的 task，node 仍可视为 `running` 或 `ready_materialized`；第一阶段为简单起见可以把已创建 task 的 node 统一标为 `running`，并通过 task snapshot 展示真实 dispatch 状态。

## Kanban 物化规则

node 物化成 Kanban task 时，task body 不是完整历史，而是 worker context。context 至少包含 root objective、node title/description、node constraints、它要填补的 goal item/gap、success evidence contract、直接依赖节点的 output_summary、相关 artifact refs、expected receipt schema、完成/阻塞协议、允许默认决策范围、必须停止请求 human 的边界。expected receipt schema 必须要求 worker 返回 summary、verdict、artifacts、verification、claimed_goal_items、partial_goal_items、unmet_goal_items、new_constraints、human_gate_suggestions、active_assumptions、decisions_made、rejected_approaches、known_failure_boundaries、open_questions、risk_notes。这里保存的是压缩事实，不要求也不鼓励输出完整推理链。task body 还要写入机器可读 footer，例如 `runtime_job_id`、`execution_node_id`、`node_key`、`node_type`、`node_materialization_id`，方便 worker evidence 和日志回溯。

新 task 的 `created_by` 建议为 `runtime_kernel`，`tenant` 建议为 `runtime:<job_id>`。`idempotency_key` 必须包含 job_id 和 node_id，避免重复 advance 创建重复 task。`workspace_kind` 和 `workspace_path` 继承 root task/job。`assignee` 来自 node 或显式配置的 lane policy；lane policy 只能选择执行后端，不能生成后续流程。`initial_status` 可以直接用 `running` 走现有 create path，也可以用 `ready` 让 dispatcher claim；应优先沿用当前 Kanban 创建 worker task 的成熟路径，具体以基线代码 helper 能力为准。

物化后马上创建 `node_materializations` 行，记录 `node_materialized` event，并把 Kanban `task_id`、latest `run_id` 写回 node 的最新指针。若 task 创建成功但写回 materialization/node 失败，事务必须整体回滚；不要留下孤儿 task。若使用的 Kanban helper 内部自己开事务，runtime kernel 需要避免外层再开冲突事务，必要时先实现一个内部 helper 或接受“两阶段但幂等”的物化策略。evidence ingest 必须根据 materialization attempt 做幂等，不能只看 node 的最新 run。

## Evidence ingest 规则

ingest 不读取 worker 完整对话，只读 `task_progress_snapshot()` 和必要的 artifact 文件。它应提取这些结构化字段：task status、run outcome、worker verdict、summary、changed_files、verification results、artifact refs、failure reason、human request、recent progress event summary、claimed_goal_items、partial_goal_items、unmet_goal_items、new_constraints、human_gate_suggestions、active_assumptions、decisions_made、rejected_approaches、known_failure_boundaries、open_questions、risk_notes。

verdict 需要规范化。`pass`、`approved`、`success` 归为 `succeeded`。`needs_fix`、`failed`、`error` 归为 `failed`。`blocked` 归为 `blocked`。`human_required`、`requires_human` 归为 `waiting_human`。无法判断但 task 已结束时归为 `node_uncertain`，触发 decision provider 或人工 gate，而不是假装成功。

ingest 必须幂等。每个 node metadata 可以保存 `last_ingested_run_id`、`last_ingested_event_id`、`ingested_terminal`。如果同一个 run 已经 terminal ingest 过，后续 advance 不应重复写 node_completed 或 node_failed。对于 progress 类事件，可以只保留最后 N 条摘要，不必全部复制到 execution_events。认知状态字段应被合并到 node 的 `assumptions_json`，并由 decision context builder 汇总到 job 级 `active_assumptions`、`rejected_approaches` 和 `known_failure_boundaries`；不要只把这些内容写入 markdown evidence。

## Decision provider 接口

第一阶段 provider 可以是 Python callable，不需要立即接真实 LLM。建议接口形状为 `decision_provider(session: DecisionSession, delta: dict) -> dict`。返回值必须是 patch JSON 或明确 noop。真实 LLM provider 只是这个接口的一种实现，负责把 stable prefix、checkpoint 和 delta 渲染成 cache-friendly prompt 或 provider session append，调用模型，解析 JSON，返回 patch。fake provider 可以根据 DB-derived delta 和测试用 session state 确定性返回下一步 patch。

provider 不允许访问数据库连接，不允许直接创建 task，不允许写文件。这样可以保证“智能”被限制在结构决策，而不是变成另一个隐式 agent。

真实 LLM provider 的 prompt 必须强调三点：只能返回 JSON，不能返回解释性正文，不能把 session 记忆当成事实覆盖 DB delta。`rationale_summary` 可以保留，但它是审计摘要，不参与状态变更。解析失败、schema 不匹配、patch 被拒绝都应该生成 `patch_rejected` event，并停止本轮 advance。

decision session 不是负责人 agent，但也不是完全无上下文的冷启动函数。它是一个受外部事实约束的长期决策上下文，可以保留 job objective、goal contract、重要历史决策、已排除路径、当前 milestone、最近 gap resolution 过程和 validator 拒绝历史，从而维持项目理解。它没有写权限，不能直接改变 DB、文件或 Kanban task。每次只能基于 kernel 提供的当前 DB delta 和待解决 gap 输出 patch proposal。如果 decision session 的记忆和 DB 事实冲突，DB 优先，并把冲突纠正作为 event/delta 追加回 decision session。

前缀缓存是 decision session 的实现目标之一，但不是 correctness 依赖。cache-friendly layout 应把长期不变的 runtime contract 放在最前面，包括 patch schema、validator 硬约束、禁止直接完成 job、禁止绕过 verifier、禁止修改 terminal fact；然后是稳定 goal contract；再后面是 checkpoint；最后才是本次 delta 和待决策问题。只要前面的 token 序列稳定，provider 可以复用前缀计算；即使 provider 不支持缓存，系统 correctness 仍然由 DB、delta、validator 和 event log 保证。

decision session 需要前文定义的 Decision Session Compaction Runtime。session 不能无限增长；当 compaction policy 触发时，kernel 应关闭旧 segment、归档 transcript、生成并校验 checkpoint，再开启新的 active segment。provider 输入只应包含 stable runtime contract、当前 goal contract、latest checkpoint、短 tail 和本轮 delta，不应继续携带旧 segment 原文。

## Patch validator 细节

validator 是新架构最重要的安全边界。它应该在 apply 前基于当前 DB 状态构造一个临时 graph，然后逐个 op 模拟执行。所有 op 模拟通过后，再在一个 write transaction 中真正写入。

必须拒绝的情况包括：未知 op、未知 node_key、重复 node_key、空 title/description、未知 node_type、非法状态迁移、给 terminal node 添加会改变其语义的 op、创建自依赖、创建环、直接放行节点执行、引用不存在 artifact、assignee 明确不存在且策略要求严格 lane、`expected_revision` 与当前 graph revision 不匹配、创建没有 `goal_item_keys`、`gap_keys` 或 `human_gate_reason` 的 node、试图把 job 直接标记为 done、试图跳过 required verifier、patch op 数超过上限、patch 尺寸超过上限、同一个 patch 重复应用但不是幂等 noop。

validator 还要处理部分幂等。例如同一个 `add_dependency` 如果边已存在，可以视为 noop；同一个 `create_node` 如果 node_key 已存在且字段完全一致，可以视为 noop，但如果字段不同必须拒绝。这样 supervisor 重试不会轻易造成重复结构。

## Decision Delta And Checkpoint 内容选择细节

decision delta、short tail 和 checkpoint payload 的构造不是简单截断。它应该优先保留会影响结构决策的信息。节点层面保留 node_key、type、state、title、output_summary、verdict、artifact summaries、assumptions summary 和依赖状态；去掉长日志、完整 diff、完整 markdown。事件层面保留最近结构事件和未解决事件；老事件只进入 checkpoint 的结构化历史字段。artifact 层面保留 path/ref、type、summary、size/hash，不直接塞大内容。约束层面必须完整保留，因为丢约束会导致错误 patch。认知状态层面必须显式保留 active assumptions、rejected approaches、known failure boundaries、open questions 和 risk notes，避免 decision session 重复探索已否定路径。

后续可以实现 context budget，例如 `max_nodes`、`max_events`、`max_chars_per_summary`、`max_total_chars`。如果 graph 太大，需要按 active frontier、blocked frontier、recently changed nodes 和 terminal summaries 分层压缩。第一阶段 graph 小，可以先实现简单版本，但接口要为 budget 留参数。

## 并发和锁

runtime kernel 需要防止两个 supervisor 同时 advance 同一个 job。第一阶段可以依赖 SQLite write transaction 加 job metadata 中的 `advance_lock`。更简单的做法是在 `runtime_jobs` 加 `claim_lock`、`claim_expires_at` 字段，advance 前尝试原子 claim，结束后释放。没有这个保护时，两个 advance 可能同时看到同一个 ready node，然后各自创建 Kanban task。

锁必须有 TTL，防止进程崩溃后 job 永久卡住。若发现过期锁，新的 advance 可以抢占，并记录 `advance_lock_reclaimed` event。锁 owner 可以用 `hostname:pid:uuid`。

锁解决同时执行问题，graph revision 解决过期决策问题。每次结构变更，包括 patch apply、node 创建、dependency 创建、relation 创建、node supersede、human gate 结构更新，都应递增 job graph revision。decision provider 基于某个 DB/graph revision 返回 patch，apply 时必须校验 revision。若 revision 已变化，应重新构造 delta 或把 patch 判定为严格 noop；不能把旧 revision 上的 patch 合并进新 graph。

Kanban dispatcher 的并发控制仍归 dispatcher。runtime kernel 不直接限制全局 worker 数，只决定 node 是否 ready 和是否物化。节点并行度可以通过 `execution_dependencies` 和 node/job metadata 限制，例如 `max_active_nodes`，但最终进程并发仍由 Kanban lane/max_spawn 控制。

## 失败处理

worker 失败不等于 job 失败。node failed 后，kernel 应先运行 reducer，更新 progress ledger 和 goal gaps；如果需要结构决策，再让 provider 决定是否创建 debug/fix node、插入验证节点、请求人工、supersede 失败节点或提出 `propose_blocked`。`blocked` 不是模型逃避复杂任务的出口，最终 blocked 状态只能由本地 reducer/validator 在确认存在外部资源、权限、破坏性变更、不可用依赖、策略违规或系统错误等不可默认推进条件时写入。

decision provider 失败也不等于 job 失败。解析失败、网络失败、schema 拒绝应该记录 decision failure，然后 job 停在 `waiting_decision` 或 `blocked`，由人工重试或切换 provider。第一阶段 fake provider 不会有网络失败，但仍应测试 provider 抛异常的路径。

patch rejected 是架构正常路径，不是系统崩溃。被拒绝的 patch 和 reject_reason 必须保留，方便改 prompt 或 validator。

## 人工交互

人工不是一个特殊 worker，而是 graph 中的 human gate。`request_human` patch 创建或更新 `human_gate` 节点，并把 job 状态推到 `waiting_human`。human gate 的 payload 必须包含问题、背景摘要、候选项、推荐项、风险、触发的 goal item/gap、为什么不能按默认策略继续、以及超时策略。人工回答后写入 `execution_events` 的 `human_decision_received`，然后 kernel 继续 advance。

human gate 是受控阻塞，不是普通失败。只有遇到真正需要用户授权或偏好的问题时才进入 human gate，例如是否使用付费 API、是否允许破坏性迁移、是否选择长期架构路线、是否提供外部凭证。普通工程选择不应该频繁询问用户，系统应该按 goal contract 的 defaults policy 推进并记录 rationale。

dashboard 或 CLI 可以提供 human decision endpoint，但第一阶段可以只做 DB/helper 层。重要的是人工输入也必须变成结构化 event，而不是写进聊天上下文后让某个 agent 记住。

## 完成判定

不要让 LLM 直接决定 job done。第一阶段完成规则应本地化：所有 required goal items 都在 progress ledger 中有足够 evidence 支持；required verifier/review nodes 成功；没有未解决 hard constraint；没有 active blocker；没有 active human gate；没有 running required node；没有 contradicted ledger entry。LLM 可以建议 `complete_job`，但 validator 必须用本地规则确认。

如果 worker 说“完成了”，但没有 ledger evidence 或验证 evidence，job 不能完成。如果实现和验证都完成，但缺少用户要求的使用说明、部署入口或交付物，job 也不能完成，除非 goal contract 明确把该 item 设为 optional 或用户 waiver。默认策略应该偏保守。

## Milestone

milestone 是 goal contract 派生的局部工作窗口，不能退化成旧 phase。phase 是预设流程，例如 planning、implementation、review；milestone 是从目标合同中派生出来的可交付切片，例如“跑通最小端到端路径”“补齐验证证据”“替换 mock provider 为真实 provider”。milestone 的作用是控制当前推进焦点，避免每次 decision 都面对整个巨大目标空间。

系统可以先推进当前 active milestone，完成后再选择下一个 milestone。但 job 的完成仍然由整个 goal contract 判定，而不是由 milestone 判定。milestone 不能让 kernel 写出固定流程状态机；它只是 decision context 和 gap detector 的聚焦器。

## Liveness And Anti-Stuck

supervisor 的运行语义是 liveness-driven loop。工程上它仍然可以是 bounded 的，避免无限占用进程；但每次退出都必须有合法原因。合法原因只有目标完成、等待 worker、等待用户、达到运行预算但仍可恢复、或系统错误需要人工处理。不能出现“目标未完成、没有 worker、没有 human gate、也没有 pending decision，但 supervisor 停了”的状态。如果出现这种状态，应记录 `liveness_violation`，并触发 goal-gap decision 或策略更新。

anti-stuck policy 用来避免系统在同一种失败模式里重复继续。停滞应被定义成可检测状态，例如同一个 gap 多轮没有新增 evidence，同类 node 连续失败，decision 连续产生 noop patch，worker 多次 uncertain，active milestone 超过预算没有满足任何 goal item，或 patch 连续被 validator 拒绝。一旦检测到停滞，系统不应该继续 retry 同类节点，而应该强制 strategy update，例如拆小任务、换 worker lane、插入 research、降级 milestone、请求用户选择或改变实现路径。

## Dashboard 方向

新前端不应该先做旧 Orchestra 控制台。它应该显示 job graph、active frontier、node 状态、事件流、patch history、worker task 映射、artifact/evidence 摘要和 human gate。任务仍然是用户可理解入口，但详情页的核心不再是固定 phase，而是 execution graph 如何演化。

第一阶段可以只保留 API/pytest，不急着做前端。等 kernel 闭环稳定后，再从旧分支迁移 task-centered dashboard 的布局经验。迁移时 API 命名应使用 `/runtime/jobs`，不要沿用 `/orchestras`，避免概念污染。

## 与 main agent 的关系

main agent 是控制面，不是 runtime kernel。它可以创建 job、查询状态、解释当前 graph、请求人工决策、触发一次 advance 或启动 supervisor，但不应该直接改 execution graph。main agent 如果要影响结构，也应该通过同一个 graph patch API 或 human decision event 写入。

这保证了用户在聊天里发出的高层命令也会进入同一套外部状态，而不是变成聊天历史中的隐式指令。

## 第一阶段验收标准

第一阶段完成时，应该能在单元测试里证明：新分支从 `b5a262c` 起步；`runtime_jobs`、goal contract、goal items、progress ledger 和 graph 表可以初始化；创建 job 会产生 goal contract、required goal items 和初始 analysis node；analysis node 可以物化为 Kanban task；fake evidence 可以被 ingest 成 `node_completed`，并写入 assumptions/rejected approaches/failure boundaries 和 progress ledger；gap detector 能从未满足 goal items 推导 goal gaps；fake decision provider 可以返回带 `expected_revision` 的 graph patch；patch validator 可以应用 fixture 节点和依赖，并拒绝没有 goal/gap linkage 的 node；reducer 自动计算 readiness，不接受 `release_node`；过期 revision patch 会被拒绝；依赖满足后后续节点才会 ready；验证节点成功后 ledger item 变为 satisfied；job 只有在 required goal items 满足后才由本地规则 done；synthetic audit 能在无 runnable node 且 goal 未满足时生成结构事件；liveness violation 能被记录；所有步骤都有 execution_events 和 graph_patches 记录。

如果这个闭环没有跑通，不应该投入大量前端工作。前端只能证明展示，不能证明新架构成立。
