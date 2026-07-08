# Hermes Kanban Runtime Kernel Phase 3 实现计划

本文档定义 Phase 3 的范围。Phase 2D 已经让 runtime 拥有本地 decision session 生命周期：active segment、append-only transcript entries、deterministic checkpoint、checkpoint validator、manual/auto compaction policy、markdown compaction profiles、strict short tail provider input 和 CLI 可观测性。Phase 3 的目标是在不破坏这些边界的前提下接入真实 decision provider。

Phase 3 的正式名称是 **Real Decision Provider Integration**。它不是引入负责人 agent，不是恢复旧 Orchestra manager loop，也不是让真实 LLM 拥有数据库事实状态。真实 provider 只替代 deterministic/replay provider 的 patch proposal 生成能力；DB authoritative state、local reducer、patch validator、goal gap detector、liveness、completion 和 compaction lifecycle 仍由 runtime kernel 本地控制。

长期架构仍以 `docs/kanban-runtime-kernel-design.md` 为准。Phase 3 必须继续遵守 Phase 1、Phase 2A、Phase 2B、Phase 2C 和 Phase 2D 的不变量：DB 是事实源，decision session 是非权威推理上下文，goal contract/progress ledger 定义完成，reducer 拥有 readiness/job state/gap/liveness/completion，decision provider 只能提出 graph patch proposal。

## Phase 3 目标

第一，新增真实 decision provider abstraction。它应实现与现有 deterministic/replay provider 相同的核心语义：输入是 `DecisionProviderRequest` 或等价的 rendered provider input，输出是 `runtime_graph_patch_v1` JSON proposal 或可记录的 parse/provider failure。

第二，复用 Phase 2D provider input。真实 provider 不能重新拼全量冷启动 snapshot，也不能读取旧 segment 原文。输入必须来自 `build_decision_provider_request()` 和 `render_decision_prompt()`：stable runtime contract、current goal contract、latest validated checkpoint、strict short tail 和 current delta。

第三，记录 provider request/response refs。每次真实 provider 调用都必须能审计：使用了哪个 provider/profile/model、基于哪个 db/graph revision、request 渲染版本是什么、raw response 是什么、parse/validator 结果是什么、是否触发 retry、是否最终落库。

第四，保持 parser/validator 双边界。provider raw output 必须先通过 strict JSON extraction/schema parse，再通过 graph patch validator。parser 可以有有限 retry/repair，但 validator 不能为模型输出放宽规则。

第五，保持测试 deterministic。Phase 3 第一批单元测试不得依赖真实网络、API key、真实 OpenAI/Anthropic/Claude/Codex 调用。测试应使用 fake HTTP client、record/replay provider 或 injected callable，验证调用形状、错误路径和审计记录。

第六，真实 provider 失败不等于 job failed。网络错误、rate limit、parse failure、schema mismatch 和 patch rejected 都应进入 `kernel_decisions` / `decision_segment_entries` / `execution_events`，job 停在可恢复的 `waiting_decision` 或合法 blocked/human gate，而不是直接 failed。

## 明确非目标

不接真实 compaction provider。Phase 3 第一批只接真实 decision provider；真实 LLM compaction provider 属于后续阶段。Phase 2D 的 deterministic checkpoint 继续作为 compaction fallback。

不做 dashboard UI。可以补 CLI/API JSON 观测字段，但不迁移前端。

不做 runtime daemon。`advance_runtime_job()` 和 bounded supervisor 仍是工程入口；真实 provider 只是被 decision_requested 调用时的一种 provider 实现。

不允许 LLM 直接调用 DB、创建 Kanban task、写文件、标记 job done、release node、mark blocked 或绕过 verifier。

不把 provider prompt 当事实源。provider request 是从 DB/checkpoint/delta 渲染出的推理输入；和 DB 冲突时永远以 DB 为准。

不把 Codex/Claude Code worker backend 和 decision provider 混成同一层。worker backend 执行 node；decision provider 提出 graph patch。两者可以使用同类模型服务，但职责和权限不同。

## Provider Interface

建议新增或补强以下接口：

```python
class RuntimeDecisionProvider(Protocol):
    def decide(self, request: DecisionProviderRequest) -> DecisionProviderResult:
        ...
```

或者保持 callable 兼容：

```python
decision_provider(session: dict, delta: dict) -> Any
```

但真实 provider 的内部实现应使用 `DecisionProviderRequest`，而不是只吃旧的 `session, delta`。兼容层可以在 `advance_runtime_job()` 中暂时保留，以避免破坏现有 tests 和 replay provider。

`DecisionProviderResult` 应包含：

- `patch`: parsed patch 或 `None`
- `raw_output`: provider 原始响应或文本
- `provider_name`
- `model`
- `profile_name`
- `request_ref`
- `response_ref`
- `parse_status`
- `retry_count`
- `error`

## Provider Profiles

Phase 3 应引入 runtime decision provider profile，而不是把 provider/model/prompt 写死在代码里。

建议新增目录：

```text
docs/kanban-runtime-kernel-decision-profiles/
  graph_patch_decision.md
  validator_recovery_decision.md
  anti_stuck_strategy_decision.md
```

每个 profile 至少声明：

- 用途
- 输入组成
- 可用 patch ops
- 禁止事项
- 输出 schema
- parse/repair 策略
- validator 失败反馈方式
- profile version

checkpoint/decision row 应记录 profile name/version/hash/path，保证审计和回放能知道某次 provider 调用使用了哪个决策契约。

第一批实现可以只提供 `graph_patch_decision.md`，其目标是根据 current delta 和 open goal gaps 产生一个 graph patch proposal。

## Provider Request Rendering

真实 provider 输入应由 `render_decision_prompt(request)` 或后续等价 renderer 生成。renderer 必须 cache-friendly：

第一，stable runtime contract 放最前，包括 patch schema、allowed ops、forbidden ops、DB authoritative state、provider only returns patch JSON、node type is not phase。

第二，current goal contract 保持 canonical order。

第三，latest validated checkpoint。

第四，strict short tail，只能包含 checkpoint 覆盖之后的 entries，并受 entry count/token budget 限制。

第五，current delta。

动态字段如当前时间、随机 id、recent events 和 frontier change 不得进入稳定前缀。

## Parsing And Retry

Phase 3 parser 必须继续严格：

- provider output 必须解析为一个 JSON object；
- JSON fence 可以接受；
- fence 外自由文本应拒绝；
- schema 必须是 `runtime_graph_patch_v1`；
- `expected_revision` 必须匹配当前 decision revision；
- op 必须在 allowed ops 内；
- `release_node` 和 direct `complete_job` 必须拒绝。

Phase 3 可以引入有限 retry：

1. 第一次 raw output parse failed；
2. 生成一个 parser correction request；
3. correction request 只能要求返回同一 patch schema JSON；
4. 最多 retry N 次，默认 1；
5. retry 的 raw input/output 都记录到 decision segment entries。

Retry 不得绕过 validator。validator rejected 后可以进入下一轮 decision，不应在同一个 apply 中自动放宽 patch。

## Persistence And Audit

Phase 3 应补强 `kernel_decisions` 或相关 metadata，至少记录：

- `provider`
- `model`
- `profile_name`
- `profile_version`
- `profile_hash`
- `request_ref`
- `response_ref`
- `retry_count`
- `parse_status`
- `provider_latency_ms`
- `input_token_estimate`
- `output_token_estimate`

如果不立即迁移 schema，可先写入 `validator_result_json`、`decision_json` 或 `decision_segment_entries.payload_json`，但字段语义要稳定，方便后续表化。

`decision_segment_entries` 应追加：

- `provider_input`
- `provider_raw_output`
- `patch_parsed`
- `validator_result`
- `patch_applied`
- `patch_rejected`
- `provider_error`
- `parse_retry`

已有 entry type 可以复用，但真实 provider 的 request/response ref 必须可追踪。

## CLI/API

Phase 3 可以扩展 `hermes kanban runtime`：

`runtime prompt <job_id> --profile graph_patch_decision --json`：渲染真实 provider 将看到的 request，不调用模型。

`runtime advance <job_id> --provider replay|fixture|real --model ... --profile graph_patch_decision --json`：显式选择 provider。第一批可只实现 fake/injected provider profile，真实网络 provider 可以放到第二步。

`runtime decision <job_id> --json`：显示 provider/model/profile/retry/parse/validator 信息。

`runtime provider-smoke <job_id> --dry-run --json`：构造 request 并运行 parser dry-run，不应用 patch。若实现成本高，可后置。

所有 CLI 命令都不能直接修改 graph，除非经过 `advance_runtime_job()` 的 provider -> parser -> validator -> apply 路径。

## 配置

Phase 3 provider 配置建议来自 job metadata、decision session metadata 或 CLI 显式参数。第一批不要依赖全局默认模型隐式行为。

建议配置字段：

```json
{
  "decision_provider": {
    "provider": "openai",
    "model": "gpt-...",
    "profile": "graph_patch_decision",
    "max_retries": 1,
    "temperature": 0,
    "timeout_seconds": 60
  }
}
```

如果没有显式 provider，runtime 应继续使用 deterministic/replay provider 或停在 `waiting_decision`，不能偷偷调用用户默认聊天模型。

## Phase 3 实施顺序

第一步，新增 decision profile markdown 目录和 `graph_patch_decision.md`，包含 schema、禁止事项、输出示例和 version/hash。

第二步，新增 provider request envelope 和 profile loader，记录 profile hash/version/path。

第三步，新增 `RuntimeDecisionProvider` 或 equivalent adapter，先实现 record/replay/fake provider 的新接口，保持旧 callable 兼容。

第四步，扩展 `advance_runtime_job()` decision path，让它可以记录 `provider_input` entry、provider/model/profile metadata、latency、token estimates 和 request/response refs。

第五步，加入 parse retry 机制。第一批只处理 parse failure，不处理 validator rejection retry。

第六步，新增 CLI 参数或 helper，让 tests 可以选择 provider/profile。

第七步，补 fake HTTP/provider tests，不依赖真实网络。

第八步，再接入一个真实 provider adapter。真实 adapter 的 smoke 不应成为单元测试前提。

## 测试清单

`test_decision_profile_loader_records_hash`：profile loader 读取 markdown version/hash/path。

`test_provider_request_records_profile_metadata`：decision call 记录 profile/model/provider。

`test_real_provider_adapter_uses_rendered_request`：provider adapter 输入来自 `build_decision_provider_request()` / renderer，不重新拼 snapshot。

`test_provider_raw_output_appended_to_segment`：真实 provider raw output 进入 decision segment entries。

`test_parse_failure_retries_once`：parse failure 触发一次 correction retry，并记录 retry entry。

`test_parse_failure_after_retry_keeps_graph_unchanged`：retry 仍失败时 graph revision 不变，job 可恢复。

`test_validator_rejection_records_profile_and_response_ref`：validator rejected 时记录 profile/request/response refs。

`test_provider_network_error_records_waiting_decision`：provider error 不使 job failed。

`test_provider_cannot_return_release_node`：真实 provider 输出 forbidden op 仍被 parser/validator 拒绝。

`test_provider_cannot_complete_job_directly`：direct complete rejected。

`test_no_network_required_for_unit_tests`：单测使用 fake provider，不依赖 API key。

## 完成定义

Phase 3 第一批完成必须满足：

第一，真实 decision provider 有明确 interface/profile/config 边界。

第二，provider input 来自 Phase 2D 的 request composition，不再走冷启动 snapshot。

第三，provider/model/profile/request/response/parse/retry/validator 信息可审计。

第四，parse failure、provider error、validator rejection 都是可恢复路径，不破坏 graph。

第五，validator 不因真实 provider 接入而放宽。

第六，单元测试不依赖真实网络或 API key。

第七，真实 provider smoke 如果存在，只作为手动或集成测试，不作为默认 pytest 前提。

Phase 3 第一批结束后，runtime 才适合进入真实 long-running autonomous task loop。否则真实模型接入会放大隐式上下文、不可审计 prompt 和失败不可恢复的问题。
