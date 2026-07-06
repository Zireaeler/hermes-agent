# Hermes Kanban Runtime Kernel Phase 2B 实现计划

本文档定义 Phase 2B 的范围。Phase 2A 已经完成 runtime control plane 和现有 Kanban 执行底座接线：`hermes kanban runtime ...` 可以创建、promote、status、list、advance，runtime materialized task 可以经过 `dispatch_once()` 和 worker-lane fixture，再通过 `task_progress_snapshot()` ingest 回 runtime。

Phase 2B 的目标不是 dashboard，也不是常驻 daemon，而是补齐 decision provider/session 层：把当前 deterministic fixture provider 后面的接口、上下文、prompt/delta/checkpoint、严格 JSON patch 解析和审计路径定义清楚并实现到可测试状态。这样后续接真实 LLM provider 时，不需要改 runtime kernel 的事实源、调度主权和 validator 边界。

## Phase 2B 目标

第一，新增 decision provider 抽象层。runtime kernel 继续只接受 `decision_provider(session, delta) -> patch` 形状，但 provider 的选择、配置、错误处理、返回解析、审计记录应从 `kanban_runtime_kernel.py` 中剥离到独立模块或清晰子层。fixture provider 仍然存在，但它只是一个 provider 实现，不是 runtime 默认策略。

第二，定义 cache-friendly decision session 渲染路径。DB 仍然是唯一权威事实源；decision session 仍然只是非权威推理上下文。Phase 2B 要把 stable prefix、goal contract、checkpoint、state delta、policy、available patch ops 渲染成规范化 provider input，并保证动态字段靠后、对象排序稳定、字段顺序稳定，方便后续模型前缀缓存。

第三，补最小 checkpoint/compaction。Phase 1/2A 已经有 `decision_sessions` 行和 delta append；Phase 2B 应新增或实现 `decision_checkpoints` 的最小 schema/API。checkpoint 不需要做复杂压缩模型调用，但必须能从 DB 推导出稳定结构，包含 goal contract、goal item states、progress ledger summaries、open gaps、frontier nodes、rejected patches、known failure boundaries、active assumptions 和 recent validator rejections。

第四，严格化 provider output 边界。provider 可以返回字符串、dict 或 fixture object，但进入 kernel 前必须被统一解析成 patch JSON。解析器必须拒绝自由文本、markdown fenced block 外的多余正文、未知 schema、未知 op、缺失 `expected_revision`、缺失 `rationale_summary`、以及不是 object/list 的 payload。解析失败应写入 `kernel_decisions` 和 `execution_events`，但不能修改 graph。

第五，提供 replay/record 测试能力。Phase 2B 不应该直接依赖真实 LLM 单测。应实现 recording/replay provider 或 provider transcript fixture，使 tests 可以验证 provider 输入和 patch 输出的审计路径。真实 LLM provider 可以作为后续 Phase 2C/3 的实现；如果 Phase 2B 添加 provider adapter skeleton，也必须默认关闭，不能成为测试前提。

## 明确非目标

不做 dashboard UI。Phase 2B 可以让 API/CLI 更容易展示 decision session、checkpoint 和 patch history，但不实现前端页面。

不做常驻 runtime daemon。supervisor 仍由 CLI/API 触发 bounded advance。

不把真实 LLM 调用、真实 Codex/Claude Code、网络、API key 或模型服务可用性作为完成前提。

不放宽 validator。provider 解析层不能因为“真实模型可能输出不稳定”而接受自由文本 patch、自动修补危险 op、绕过 `expected_revision` 或允许 `release_node`/`complete_job`。

不引入负责人 agent。decision session 可以保留长期推理上下文，但不能拥有事实状态、不能直接改 DB、不能创建 Kanban task、不能标记 job done。

## 建议模块

`hermes_cli/kanban_runtime_decision.py`：

负责 provider registry、provider config、provider input rendering、patch parsing、checkpoint construction、record/replay fixture provider。它可以被 `kanban_runtime_kernel.py` 调用，但不能反向直接修改 graph。

建议 public API：

`DecisionProviderRequest`：包含 `session`、`delta`、`checkpoint`、`stable_prefix`、`db_revision`、`job_id`。

`DecisionProviderResult`：包含 `patch`、`raw_output`、`provider_name`、`model`、`parse_status`、`error`。

`build_decision_provider_request(conn, job_id, delta)`：从 DB 和当前 session 构造 provider input。

`render_decision_prompt(request)`：输出 cache-friendly prompt/messages。Phase 2B 可以只返回 dict/list，不必接真实 API。

`parse_provider_patch(raw, expected_revision)`：严格解析 provider 输出为 patch dict。

`create_decision_checkpoint(conn, job_id, reason)`：从 DB 推导 checkpoint 并写入 `decision_checkpoints`。

`recording_decision_provider(...)` 和 `replay_decision_provider(...)`：用于测试审计路径。

## Schema v2B

新增 `decision_checkpoints` 表：

`id TEXT PRIMARY KEY`、`job_id TEXT NOT NULL`、`decision_session_id TEXT NOT NULL`、`revision INTEGER NOT NULL`、`checkpoint_json TEXT NOT NULL`、`reason TEXT NOT NULL`、`transcript_ref TEXT`、`created_at INTEGER NOT NULL`。

可选新增 `decision_provider_records` 表。如果实现 record/replay 时不想复用 `kernel_decisions`，可以建表保存 provider request/response；但优先复用 `kernel_decisions.delta_json`、`decision_json`、`validator_result_json`，避免过早扩表。

`decision_checkpoints.checkpoint_json` 必须是 DB-derived，不允许直接保存模型自由摘要作为唯一事实。若后续引入模型辅助压缩，模型输出也必须标记为非权威摘要，并保留 DB-derived fields。

## Provider Input Contract

provider 输入必须分四层：

第一层是 stable runtime contract：patch schema、允许 op、禁止 op、validator 硬约束、DB authoritative state 规则、不能直接 complete job、不能 release node、不能把 node_type 当 phase。

第二层是 stable goal contract：objective、goal items、hard constraints、defaults policy、human-required policy、completion policy。

第三层是 checkpoint：当前 satisfied/open/contradicted goal items、open gaps、progress ledger summaries、frontier nodes、artifact index、assumptions、rejected approaches、known failure boundaries、recent validator rejections。

第四层是本轮 delta：trigger reason、new events、changed nodes、new/changed ledger rows、current biggest gaps、current graph revision、pending decision question。

动态字段必须放在第四层。排序必须 canonicalize：goal items 按 `item_key`，nodes 按 `node_key`，gaps 按 `gap_key`，events 按 id。

## Provider Output Contract

provider 最终只能产生一个 patch object：

`schema` 必须是 `runtime_graph_patch_v1`。

`expected_revision` 必须等于当前 DB graph revision。

`rationale_summary` 必须是一行审计摘要，不参与状态变更。

`ops` 必须是 list，且只能包含当前 validator 支持的 op。

允许 provider 返回 JSON string 或 dict。若返回 markdown fenced JSON，解析器可以提取 fenced block；若 fenced block 外存在非空正文，Phase 2B 应拒绝，避免模型解释文本被误当成事实。

## CLI/API 可观测性

Phase 2B 可以扩展 `hermes kanban runtime`：

`runtime decision <job_id>`：显示最近 decision records。

`runtime checkpoint <job_id>`：创建或显示 latest checkpoint。

`runtime prompt <job_id>`：以 JSON 输出 provider request/rendered input，用于调试和缓存布局验证。

这些命令只读或只写 checkpoint，不直接改 execution graph。graph 仍只能通过 patch validator 改。

## 测试清单

`test_decision_checkpoint_schema_and_creation`：schema 包含 `decision_checkpoints`，checkpoint 可从 DB-derived state 创建。

`test_decision_prompt_layout_is_canonical`：同一 DB 状态重复渲染输出稳定；goal items、nodes、gaps 排序稳定；动态 delta 不进入 stable prefix。

`test_provider_patch_parser_accepts_strict_json_object`：合法 JSON object 被解析为 patch。

`test_provider_patch_parser_rejects_free_text_and_unknown_ops`：自由正文、未知 op、`release_node`、`complete_job`、缺 revision 被拒绝。

`test_provider_parse_failure_records_decision_without_graph_change`：解析失败写 decision/event，但 graph revision 不变。

`test_replay_provider_can_drive_existing_runtime_advance`：replay provider 用记录的 patch 驱动 `advance_runtime_job`，仍经过 validator。

`test_runtime_prompt_cli_outputs_provider_request_json`：CLI 可输出 provider request，且不包含完整 DB dump 或 worker logs。

## 完成定义

Phase 2B 完成必须满足：

第一，decision provider/session 层有独立模块或清晰子层，不再把所有 provider 逻辑塞进 kernel tick。

第二，provider input render、checkpoint creation、provider output parse 都有单元测试。

第三，解析失败、patch rejected、stale revision 都能被审计记录，不改变 graph。

第四，fixture/replay provider 可以继续驱动 runtime advance，且所有 patch 仍经过现有 validator。

第五，相关测试通过，且不依赖真实 LLM、网络、dashboard、daemon 或真实 Codex/Claude Code。
