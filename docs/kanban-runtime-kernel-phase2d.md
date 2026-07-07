# Hermes Kanban Runtime Kernel Phase 2D 实现计划

本文档定义 Phase 2D 的范围。Phase 2C 已经把 runtime kernel 的本地目标推进语义补硬：goal-driven reducer、progress ledger、goal gaps、liveness、anti-stuck synthetic events 和 human gate policy 都已经成为真实 LLM provider 之前必须具备的本地规则。Phase 2D 的目标是在接真实 LLM provider 之前，把 job 级 decision session 的长期上下文生命周期补齐。

Phase 2D 的正式名称是 **Decision Session Compaction Runtime**。它不是 dashboard summary，不是 worker log summary，也不是 worker receipt contract。它管理的对象只有一个：job 级 decision session transcript，也就是 runtime 调度层不断追加的 “DB-derived delta -> provider patch -> validator result -> graph/ledger/gap state change” 这条连续决策记录。

长期架构仍以 `docs/kanban-runtime-kernel-design.md` 为准。Phase 1、Phase 2A、Phase 2B 和 Phase 2C 的不变量继续有效：DB 是权威事实源，decision session 是非权威推理上下文，goal contract/progress ledger 定义完成，reducer 拥有 readiness、job state、goal gaps、liveness、synthetic audit 和 completion，decision provider 只能提出 graph patch，不能 release node、complete job、直接写 DB 或自由 mark blocked。

## Phase 2D 目标

第一，引入 decision session segment 生命周期。每个 job 的 decision session 必须拥有一个 active segment。每次结构决策的 delta、provider raw output、parsed patch、validator result、patch_applied/patch_rejected 和相关 graph revision 变化都追加到 active segment。segment 达到压缩条件后关闭并归档，新 segment 用 checkpoint 继续。

第二，升级 checkpoint 语义。checkpoint 不是普通 summary，而是下一阶段结构决策所需的结构化调度认知状态。它必须保留当前目标解释、goal contract revision、active milestone、已满足 goal items、未满足 goal gaps、open blockers、关键架构决策、已排除方案、已知失败边界、validator rejection lessons、human decisions、重要 artifact index、当前 graph frontier、禁止重复的无效动作和下一阶段策略约束。

第三，实现 deterministic compaction fallback。Phase 2D 不接真实 LLM compaction provider。第一版 checkpoint 由 DB-derived deterministic builder 生成，保证在没有模型、网络或 API key 的情况下也能完成 segment close、checkpoint validate 和 new segment start。

第四，实现 checkpoint validator。checkpoint candidate 必须经过本地校验后才能成为新 segment 的前缀。validator 检查 checkpoint 引用的 node_key、goal_item_key、artifact_ref、patch_id、human_decision_id 是否存在，检查它是否把未验证事项写成 confirmed，检查它是否遗漏当前 hard blocker，检查它是否和当前 DB/graph/ledger revision 冲突。

第五，引入可配置 compaction policy。Phase 2D 可以先实现 token threshold 触发和手动触发，但接口上必须支持后续引入 milestone 切换、human decision 修改目标、validator 连续拒绝、同一 gap 多轮 strategy update、graph 大规模 supersede、active frontier 进入验证阶段等语义信号。

第六，定义 compaction profile 机制。Phase 2D 应新增 markdown profile 目录和几个 profile 文档，哪怕 deterministic fallback 暂时不调用模型。profile 是后续 LLM compaction provider 的提示模板和输出契约，不应写死在代码里。

第七，调整 provider input 语义。decision provider 的输入应来自 active segment、latest checkpoint 和本轮 delta，而不是把 `kernel_decisions.snapshot_json` 当作完整冷启动 snapshot。`kernel_decisions.delta_json` 或等价字段应明确表示“本次追加到 active segment 的 DB-derived delta”。

第八，补最小 CLI/API 可观测性。runtime status 或新子命令应能显示 active segment id、latest checkpoint revision、active segment token estimate、last compaction profile、last compaction validator status、archive_ref 和 provider input composition。

## 明确非目标

不接真实 LLM compaction provider。Phase 2D 只做 deterministic DB-derived checkpoint 和 provider 接口边界，真实模型压缩属于后续阶段。

不做 dashboard UI。可以补 CLI/API JSON 观测字段，但不迁移前端。

不压缩 worker 内部上下文。Codex/Claude Code 等 backend 内部的 `/compact` 或上下文管理由 backend 自己负责，runtime kernel 不接管。

不把 worker receipt 当成 compaction。worker receipt 是节点交付契约，必须继续返回 verdict、artifact、verification、risk、failure reason、remaining gaps 和 human gate request 等结构化字段。

不删除旧 transcript。compaction 关闭旧 segment 后，旧 transcript 仍必须可审计、可 debug、可回放，只是不再进入 active LLM context。

不放宽 graph patch validator。compaction runtime 不能改变 provider 只能提出 patch proposal 的边界。

## Schema v2D

新增 `decision_session_segments` 表：

`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`decision_session_id TEXT NOT NULL`、`segment_index INTEGER NOT NULL`、`state TEXT NOT NULL`、`started_at INTEGER NOT NULL`、`closed_at INTEGER`、`start_decision_id TEXT`、`end_decision_id TEXT`、`covered_event_start INTEGER`、`covered_event_end INTEGER`、`covered_graph_revision_start INTEGER`、`covered_graph_revision_end INTEGER`、`estimated_input_tokens INTEGER NOT NULL DEFAULT 0`、`estimated_output_tokens INTEGER NOT NULL DEFAULT 0`、`compacted_checkpoint_id TEXT`、`archive_ref TEXT`、`metadata_json TEXT NOT NULL DEFAULT '{}'`。

`state` 初期限制为 `active`、`closed`、`archived`、`compacting`、`compacted`、`failed_compaction`。每个 job 或每个 decision_session 同时只能有一个 active segment。

扩展 `decision_sessions`：

新增或使用 metadata 记录 `active_segment_id`、`latest_checkpoint_id`、`last_compaction_at`、`last_compaction_status`、`last_compaction_profile`、`context_window_policy`。

升级 `decision_checkpoints`：

现有表如果已存在，可以通过新增 nullable 字段或 metadata 扩展。目标字段包括 `source_segment_id`、`profile_name`、`checkpoint_revision`、`db_revision`、`graph_revision`、`ledger_revision`、`covered_event_start`、`covered_event_end`、`covered_decision_start`、`covered_decision_end`、`payload_json`、`payload_text`、`validator_status`、`reject_reason`、`supersedes_checkpoint_id`。

`payload_json` 是机器可读结构化状态，`payload_text` 是给 provider 阅读的紧凑文本版本。两者都必须绑定 DB/graph/ledger revision。

可选新增 `decision_segment_entries` 表。如果不想把 segment transcript 存在 `decision_session_segments.metadata_json` 或 archive file，可单独记录 append-only 条目：

`id INTEGER PRIMARY KEY AUTOINCREMENT`、`segment_id TEXT NOT NULL`、`job_id TEXT NOT NULL`、`entry_type TEXT NOT NULL`、`decision_id TEXT`、`event_id INTEGER`、`graph_revision INTEGER`、`payload_json TEXT NOT NULL`、`estimated_tokens INTEGER NOT NULL DEFAULT 0`、`created_at INTEGER NOT NULL`。

第一版可以不建这张表，先通过 `kernel_decisions`、`graph_patches`、`execution_events` 和 segment metadata 重建 transcript；但设计上要允许后续扩展。

## Compaction Profiles

新增目录建议为 `docs/kanban-runtime-kernel-compaction-profiles/` 或运行时可配置的 profile 目录。Phase 2D 至少提供以下 markdown profile 草案：

`token_budget_compaction.md`：常规窗口压力压缩。目标是去掉重复 delta 和旧 patch 原文，保留当前结构决策所需状态。

`validator_boundary_compaction.md`：连续 patch rejected 后使用。目标是沉淀 validator 边界、禁止重复无效 patch、保留当前可行 op 约束。

`human_decision_compaction.md`：用户修改目标、授权、提供凭证或改变偏好后使用。目标是把 human decision 写入下一段上下文的权威约束层。

`milestone_transition_compaction.md`：milestone 切换时使用。目标是沉淀已完成目标、当前 artifact index 和下一阶段 gap。

`anti_stuck_compaction.md`：长期无 progress 或 strategy update 后使用。目标是总结失败模式和禁止重复动作。

每个 profile 应包含：用途、输入选择规则、压缩目标、禁止事项、输出 schema、validator 要求和示例。kernel 不理解 profile 自然语言内容，只负责选择 profile、组装输入、调用 provider、校验 checkpoint。

## Compaction Policy

Phase 2D 默认 policy 可以很小：

`manual`：CLI/API 显式请求 compaction。

`token_threshold`：active segment estimated tokens 超过配置阈值。

`rejection_threshold`：连续 rejected/noop/parse_failed decision 超过阈值。

`human_decision`：human decision 修改 goal contract、授权边界或硬约束。

`anti_stuck`：reducer 生成 stale/no-progress/strategy-update synthetic event。

policy 输入应预留 telemetry：`stable_prefix_tokens`、`checkpoint_tokens`、`tail_tokens`、`delta_tokens`、`model_output_tokens`、`active_segment_tokens`、`cacheable_prefix_tokens`、`context_window_ratio`、`accepted_patch_count`、`rejected_patch_count`、`noop_count`。

阈值必须可配置，不能写成不可改常量。第一版可以用 runtime_jobs metadata 或 decision_profile metadata 保存默认阈值。

## Checkpoint Payload Contract

checkpoint payload 至少包含：

`objective_summary`：当前目标解释。

`goal_contract_revision`：goal contract version 或 revision。

`active_milestone`：当前推进窗口。

`satisfied_goal_items`：已满足 required/optional items 和 evidence refs。

`open_goal_gaps`：仍未满足、未验证、失败、阻塞或停滞的 goal gaps。

`open_blockers`：hard blockers、human gates、missing secrets、policy boundaries。

`key_decisions`：已做出的关键架构/策略决策。

`rejected_approaches`：已排除路线和证据引用。

`known_failure_boundaries`：已知失败边界。

`validator_rejection_lessons`：最近 validator 拒绝原因和禁止重复动作。

`human_decisions`：用户选择、授权、waiver 或目标变更。

`artifact_index`：重要 artifact refs 和摘要。

`graph_frontier`：当前 ready/running/failed/succeeded/waiting_human frontier。

`do_not_repeat`：当前不应重复的动作或 patch 形状。

`next_strategy_constraints`：下一阶段结构决策必须遵守的策略约束。

checkpoint 不应复制完整 patch JSON、完整 worker logs、完整 diff、完整 markdown 或每轮 delta 的重复字段。

## Provider Input Composition

Phase 2D 后，decision provider input 应由这些部分组成：

第一，stable runtime contract：patch schema、validator 硬约束、禁止 direct complete、禁止 release_node、DB authoritative state 规则。

第二，current goal contract：当前 active goal contract、constraints、defaults policy、human-required policy、completion policy。

第三，latest validated checkpoint：来自上一轮 compaction 的结构化认知状态。

第四，short tail：压缩边界附近少量尚未沉淀的 recent events 或 decision entries，长度可配置。

第五，current delta：本轮 DB-derived state delta 和待决策问题。

旧 segment 原文不能继续进入 provider input。否则 compaction 只是追加摘要，不是真正替换上下文。

## Checkpoint Validator

checkpoint validator 至少检查：

引用的 node_key 是否存在或已被明确标记 superseded。

引用的 goal_item_key 是否存在于当前或被覆盖的 goal contract revision。

引用的 artifact_ref、patch_id、human_decision_id 是否存在。

checkpoint 是否把 failed verifier、unverified evidence 或 partial evidence 写成 confirmed/satisfied。

checkpoint 是否遗漏当前 open hard blocker 或 active human gate。

checkpoint 的 db_revision、graph_revision、ledger_revision 是否和当前 DB 兼容。

checkpoint 是否包含 required top-level fields。

validator 失败时，checkpoint 不能成为新 active segment 前缀。系统可以重试同 profile、换 fallback profile，或者保留旧 active segment 并进入 recoverable waiting_decision/manual intervention。

## 建议 API

`ensure_decision_segment(conn, job_id)`：确保 job 有 active decision session segment。

`append_decision_segment_entry(conn, job_id, entry_type, payload, decision_id=None, event_id=None)`：追加 delta、patch、validator result 或 compaction signal。

`estimate_segment_tokens(conn, segment_id)`：估算 active segment tokens。

`should_compact_decision_session(conn, job_id, policy=None)`：返回是否应压缩、trigger reason 和 profile。

`build_compaction_input(conn, job_id, segment_id, profile_name)`：构造 compaction provider 输入。

`build_deterministic_checkpoint(conn, job_id, source_segment_id, profile_name)`：DB-derived fallback checkpoint。

`validate_decision_checkpoint(conn, job_id, checkpoint_payload)`：校验 checkpoint candidate。

`compact_decision_session(conn, job_id, profile_name=None, reason=None)`：关闭旧 segment、生成 checkpoint、开启新 segment。

`build_decision_provider_request(conn, job_id, delta)`：改为读取 active segment、latest checkpoint、short tail 和 current delta。

## CLI/API 可观测性

Phase 2D 可以扩展 `hermes kanban runtime`：

`runtime context <job_id> --json`：显示 active segment、latest checkpoint、token estimate、profile、archive_ref 和 provider input composition。

`runtime compact <job_id> --profile token_budget_compaction --json`：手动触发 compaction。第一版使用 deterministic fallback。

`runtime checkpoint <job_id> --json`：继续显示 latest checkpoint，但要包含 source_segment_id、validator_status 和 covered ranges。

这些命令不直接修改 execution graph。`runtime compact` 只改变 decision session segment/checkpoint 状态。

## 测试清单

`test_job_creation_creates_active_decision_segment`：创建 runtime job 时存在 active segment。

`test_decision_delta_appends_to_active_segment`：每次 decision delta、patch 和 validator result 都能归属 active segment。

`test_manual_compaction_archives_old_segment_and_creates_new_active_segment`：手动 compaction 后旧 segment 不再 active，新 segment 开启。

`test_new_provider_input_uses_checkpoint_not_old_transcript`：新 provider request 包含 latest checkpoint 和 short tail，不包含旧 segment 原文。

`test_checkpoint_binds_db_graph_ledger_revision`：checkpoint payload 和 row 绑定当前 revision。

`test_checkpoint_validator_rejects_unknown_node_reference`：引用不存在 node_key 的 checkpoint 被拒绝。

`test_checkpoint_validator_rejects_failed_verifier_as_confirmed`：把 failed verifier 写成 satisfied 的 checkpoint 被拒绝。

`test_compaction_profile_can_be_selected_by_policy`：policy 能选择不同 profile。

`test_compaction_failure_is_recoverable`：checkpoint validate 失败不导致 job failed，旧 segment 可保留或进入 waiting_decision。

`test_multiple_compactions_are_traceable`：多次 compaction 后能追溯某个 patch 属于哪个 segment、哪个 checkpoint 之后。

`test_worker_receipt_is_not_compacted_by_runtime`：runtime compaction 不读取或压缩 worker 内部对话，只处理 decision transcript。

## 完成定义

Phase 2D 完成必须满足：

第一，decision session 有 active segment 生命周期，且每个 job 同时只有一个 active segment。

第二，每次结构决策的 delta、provider output、validator result 和 patch outcome 都能归属 active segment 或从现有 DB 表重建到 segment。

第三，manual 或 token-threshold compaction 能关闭旧 segment、生成 checkpoint、校验 checkpoint、开启新 segment。

第四，新 provider input 使用 latest checkpoint 替代旧 segment 原文。

第五，checkpoint validator 能拒绝不存在引用、事实冲突和把未验证事项写成 confirmed 的 checkpoint。

第六，compaction policy 和 profile 是可配置/可替换的，不是硬编码在 provider prompt 里。

第七，CLI/API 能观测 active segment、checkpoint、token estimate、archive_ref 和最近 compaction 结果。

第八，所有测试使用 deterministic compaction fallback、本地 SQLite 和 fake/replay provider，不依赖真实 LLM、网络、dashboard、daemon 或真实 Codex/Claude Code。

Phase 2D 结束后，runtime 才适合进入真实 LLM provider 和真实 LLM compaction provider 阶段。否则真实模型会被迫承担无限上下文管理，这会破坏 DB authoritative state、checkpoint 审计和前缀缓存收益。
