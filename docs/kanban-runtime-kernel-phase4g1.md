# Hermes Kanban Runtime Kernel Phase 4G1：Real Model Provider Smoke

Phase 4G1 的目标是在 Phase 4G deterministic baseline 稳定之后，验证真实模型源能否在
runtime kernel 的边界内工作。

Phase 4G1 不是真实长任务阶段，也不是接真实 worker lane 的阶段。它只验证真实 decision
provider 和真实 compaction provider 的调用路径、解析、validator、审计和隔离行为。所有
真实模型调用都必须显式 opt-in、bounded、可审计，并且优先在隔离环境中执行。

## 1. 背景

Phase 3 已经接入真实 decision provider，并提供 `provider-smoke --execute` 和
`advance --provider real` 等命令。

Phase 4A 已经接入真实 compaction provider，并提供
`compact --provider real`。

Phase 4G 已经通过 deterministic synthetic long-run soak 验证 runtime 自身的恢复、
capability、memory、compaction、lease 和 consistency 基线。

下一步不应直接跑真实长任务。真实长任务失败时很难判断是模型输出质量、prompt/profile、
provider transport、compaction 质量、worker 状态、capability policy 还是 runtime
状态机问题。Phase 4G1 先把真实模型源本身纳入可重复的 smoke runbook。

## 2. 目标

Phase 4G1 要验证：

- 当前模型源配置可以被 runtime provider adapter 显式读取；
- decision provider 是 no-tools single-shot；
- provider request 不包含工具 schema，不执行 web search，不启动 worker；
- provider output 可以被解析成 `runtime_graph_patch_v1` proposal，或被明确分类为
  parse/provider failure；
- validator dry-run 可以判断 patch 是否 would apply；
- one-step real advance 可以落库一个受 validator 保护的 patch；
- 真实 compaction provider 可以输出 checkpoint candidate，或被 fallback / no-fallback
  路径安全处理；
- request_ref / response_ref / profile hash / model / parse status / validator result
  可审计；
- 真实模型调用不会写入 API key，不修改 `~/.codex`，不污染主 runtime DB。

## 3. 非目标

Phase 4G1 不接真实 Codex / Claude Code worker lane。

Phase 4G1 不跑真实数小时任务。

Phase 4G1 不要求真实模型一次性产出高质量完整 execution graph。

Phase 4G1 不让真实 provider 绕过 validator。

Phase 4G1 不把 provider smoke 作为默认单元测试。默认 pytest 仍必须离线、deterministic、
不依赖 API key 或网络。

Phase 4G1 不让 provider 使用 tool / web search。真实 provider 只能返回结构化 proposal
或 checkpoint candidate。

## 4. 隔离要求

真实模型 smoke 必须先在隔离环境中运行。

建议：

```bash
export HERMES_HOME="$(mktemp -d)"
export HERMES_KANBAN_DB="$HERMES_HOME/kanban.db"
```

或使用等价测试隔离目录。

如果使用当前 Codex CLI 模型源：

```bash
--codex-config
```

该路径只能读取 `~/.codex/config.toml` 和 `~/.codex/auth.json`，不得修改这些文件，不得
打印 API key，不得把 key 写入 runtime DB、decision segment、logs 或 CLI output。

Smoke 输出可以记录：

- provider name；
- model；
- base_url hash 或 provider alias；
- request_ref；
- response_ref；
- token estimate；
- latency；
- parse status。

Smoke 输出不得记录：

- API key；
- raw credential；
- secret header；
- 完整 external response body 中的敏感内容。

## 5. Runbook

Phase 4G1 使用四步 runbook，由低风险到高风险逐步推进。

### 5.1 Step 1：Decision Provider Dry-Run

创建隔离 job：

```bash
hermes kanban runtime create "phase4g1 real provider smoke" --json
```

渲染 provider input，不调用模型：

```bash
hermes kanban runtime provider-smoke <job_id> --json
```

验收：

- `applied == false`；
- 不创建 `kernel_decisions`；
- 不插入 `graph_patches`；
- 不改变 `graph_revision`；
- 输出包含 provider input summary、profile hash 和 no-tools envelope。

### 5.2 Step 2：Decision Provider Execute Smoke

调用真实模型，但只做 validator dry-run，不 apply：

```bash
hermes kanban runtime provider-smoke <job_id> \
  --execute \
  --codex-config \
  --profile graph_patch_decision \
  --json
```

或显式 provider：

```bash
hermes kanban runtime provider-smoke <job_id> \
  --execute \
  --model-provider <provider> \
  --model <model> \
  --profile graph_patch_decision \
  --json
```

验收：

- provider 被调用一次；
- `provider_result.request_ref` 存在；
- `provider_result.response_ref` 存在或 provider_error 有明确分类；
- `provider_result.parse_status` 为 `parsed`、`parse_failed` 或 `provider_error`；
- validation 使用 `accepted`、`rejected`、`stale` 或 `skipped`；
- `applied == false`；
- graph revision 不变。

`validation.status == rejected` 是允许结果。真实模型第一次 proposal 被 validator 拒绝是正常
情况，关键是拒绝可解释、可审计、可恢复。

### 5.3 Step 3：Decision Provider One-Step Apply

只有 Step 2 证明 provider 调用和 validator dry-run 路径正常后，才允许执行一次 real
advance：

```bash
hermes kanban runtime advance <job_id> \
  --provider real \
  --codex-config \
  --profile graph_patch_decision \
  --json
```

验收：

- 若 patch accepted，则新增 `graph_patches.status = applied`；
- 若 patch rejected，则新增 rejected patch / decision event，不污染 graph；
- `kernel_decisions` 记录 provider output、validator result 和 model；
- `decision_segment_entries` 记录 provider_input、provider_output、validator_result；
- `runtime consistency` 仍 passed 或只有可解释 warning；
- job 不因 provider_error / parse_failed / patch_rejected 直接 failed。

### 5.4 Step 4：Real Compaction Provider Smoke

在同一个隔离 job 上积累少量 decision segment entries 后，调用真实 compaction provider：

```bash
hermes kanban runtime compact <job_id> \
  --provider real \
  --codex-config \
  --profile token_budget_compaction \
  --json
```

验收：

- provider 是 no-tools single-shot；
- provider 输出只允许 checkpoint candidate，不允许 graph patch；
- checkpoint candidate 经过 `validate_decision_checkpoint()`；
- accepted checkpoint 会关闭 source segment 并创建新 active segment；
- rejected checkpoint 不关闭 source segment；
- provider_error / parse_failed / rejected 默认可以 fallback 到 deterministic checkpoint；
- `--no-fallback` 时只写审计，不污染 active segment；
- 旧 segment 原文不进入后续 active provider input。

## 6. Bounded Real-Provider Loop

Step 1 到 Step 4 通过后，Phase 4G1 可以增加一个 bounded real-provider loop：

```text
real decision provider
      |
      v
validator
      |
      v
synthetic worker evidence
      |
      v
ledger / gap update
      |
      v
consistency checker
```

限制：

- 最多 3 到 5 个 decision ticks；
- worker evidence 仍使用 synthetic receipt，不接真实 worker；
- capability policy 仍生效；
- memory hint 可以注入，但不得影响 validator / capability / completion；
- compaction 可以触发，但必须 bounded；
- 失败必须分类为 provider_error、parse_failed、patch_rejected、stale、policy_blocked 或
  consistency_violation。

这个 bounded loop 的目标是验证真实 provider 在多轮反馈中不会破坏 runtime 边界，而不是
证明业务任务完成。

## 7. 观测和报告

建议新增 `runtime real-smoke` 或等价 report helper。第一版也可以先使用 runbook 命令组合，
但最终应能输出 bounded report：

```json
{
  "job_id": "rjob_xxx",
  "provider": "codex-config",
  "decision_smoke": {
    "called": true,
    "parse_status": "parsed",
    "validation_status": "accepted",
    "applied": false
  },
  "one_step_advance": {
    "patch_status": "applied",
    "graph_revision_after": 1
  },
  "real_compaction": {
    "status": "compacted",
    "fallback_used": false,
    "checkpoint_validator_status": "accepted"
  },
  "consistency": {
    "status": "passed",
    "violation_count": 0
  },
  "secrets_leaked": false
}
```

Report 不应包含完整 prompt、完整 raw response、API key 或完整 decision transcript。

## 8. 测试策略

默认测试仍然离线。

必须有 deterministic tests 覆盖：

- real-smoke report builder 对 fake real provider 的 success / parse_failed / provider_error；
- `provider-smoke --execute` validate-but-no-apply；
- real compaction provider fake output accepted / rejected / fallback / no-fallback；
- secret redaction；
- consistency checker 在 real-smoke 后仍可运行。

真实模型调用只能作为手动 smoke 或显式集成测试，不进入默认 pytest。

## 9. 验收标准

Phase 4G1 MVP 完成时必须满足：

- 有明确的隔离 runbook；
- dry-run 不调用模型、不落库；
- execute smoke 调用真实模型但不 apply；
- one-step apply 仍经过 validator；
- real compaction smoke 经过 checkpoint validator；
- provider_error / parse_failed / rejected / stale 都有结构化审计；
- API key 不进入 DB、logs 或 CLI output；
- 默认测试不依赖网络或真实模型；
- smoke 结果可以被 operator 用来判断下一步是否进入真实 provider bounded loop。

## 10. 与后续阶段的关系

Phase 4G1 通过后，再进入：

```text
Phase 4G2 Real Provider Bounded Loop with Synthetic Worker Evidence
      |
      v
Phase 4G3 Real Worker Lane Smoke
      |
      v
Phase 4H Dashboard Runtime UI
```

Phase 4G1 只证明真实模型源能通过 runtime 边界。它不证明真实 worker 稳定，也不证明真实
长任务质量。真实 worker 和真实长任务必须在 provider smoke 稳定后再接入。

## 11. 总结

Phase 4G1 是从 deterministic runtime baseline 走向真实模型运行的第一道门。

它的核心不是让模型“更会做任务”，而是验证真实模型源在 Hermes Runtime Kernel 中仍然
保持 no-tools、proposal-only、validator-protected、auditable 和 recoverable。只有这个
边界被真实模型 smoke 证明后，才适合继续做真实 provider bounded loop 和真实 worker lane
smoke。
