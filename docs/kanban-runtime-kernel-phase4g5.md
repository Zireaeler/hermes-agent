# Hermes Kanban Runtime Kernel Phase 4G5：真实 Compaction Candidate 质量

Phase 4G5 用于补齐 Runtime Kernel 真实模型验证矩阵中的 L3：真实
`RuntimeCompactionProvider` 生成的 checkpoint candidate 在不使用 deterministic fallback 的
情况下通过 checkpoint validator，并完成 decision segment rollover。

本阶段解决的是 provider 输出契约质量，不改变 compaction 的权威边界。数据库事实、goal
contract、progress ledger 和 checkpoint validator 仍是唯一裁决方；checkpoint 仍然只是当前
job 的非权威推理上下文，不是 runtime fact。

## 1. 背景

Phase 4A 已实现真实 compaction provider 的调用、JSON 解析、validator、fallback 和审计路径；
Phase 4G1 已用真实模型证明 transport 可用，但真实 candidate 因
`open_goal_gaps item lacks provenance` 被 validator 拒绝，最终由 deterministic fallback 完成
checkpoint。

已确认的根因不是数据库缺少来源事实，而是 provider contract 表达不足：

- request 已携带 DB-derived goal gaps、graph frontier、progress ledger 和 artifacts；
- prompt 只声明 `must_include_provenance=true`，没有定义 `source_refs` 的结构；
- prompt 没有给出每类 checkpoint fact 可引用的 provenance catalog；
- parser 只补充 checkpoint metadata，不补充 provenance；
- candidate 解析成功后的 validator rejection 不会进入当前 parse retry。

因此，Phase 4G5 首先增强 provider 可见的结构化契约，而不是让 parser 根据内容猜测或伪造
`source_refs`。

## 2. 目标

Phase 4G5 MVP 必须完成：

- 从当前 DB state 构造 bounded provenance catalog；
- 为所有 checkpoint fact list 提供明确的 item schema 和合法引用类型；
- 明确要求每个非空 fact item 携带非空 `source_refs`；
- 明确禁止模型发明 catalog 中不存在的引用；
- 保持 parser 只负责 JSON/metadata normalization，不自动补造 provenance；
- 固定“缺少 provenance 的合法 JSON 仍会被 validator 拒绝”的确定性回归；
- 用隔离真实模型源完成一次 no-fallback compaction；
- 真实 candidate 通过 validator、创建 accepted checkpoint，并 rollover source segment；
- 将结果以脱敏事实更新到真实集成验证台账。

## 3. 非目标

本阶段不实现：

- worker context compression；
- 跨 job Runtime Memory compaction；
- embedding、RAG 或 memory retrieval；
- 由 parser 自动推断、补齐或改写 `source_refs`；
- compaction provider 写 DB、修改 graph、修改 ledger 或完成 job；
- 将 deterministic fallback 成功记为 L3 通过；
- 为真实模型开放 tool、web search、文件系统或数据库访问；
- 长任务多轮 compaction soak。

## 4. L3 判定

L3 只在以下链路完整成立时通过：

```text
真实 compaction provider
        |
        v
原始模型输出为 checkpoint JSON object
        |
        v
parser 仅执行 JSON 与 metadata normalization
        |
        v
checkpoint validator accepted
        |
        v
accepted checkpoint persisted
        |
        v
source segment compacted + new active segment created
```

必须同时满足：

- `fallback_used == false`；
- checkpoint 中所有非空 fact item 自带合法 `source_refs`；
- `provider_validation.status == accepted`；
- active segment rollover 成功；
- consistency checker 无未解释 violation；
- 隔离报告和 DB 中无 credential 泄漏。

以下结果不算 L3：

- candidate 被拒绝后 deterministic fallback accepted；
- parser 根据 DB 内容自动添加 `source_refs` 后 accepted；
- 测试 fake provider 返回预制的 deterministic checkpoint；
- 只验证模型 transport、JSON parse 或 prompt render 成功。

## 5. Provenance Catalog

`build_compaction_provider_request()` 应从当前 request 已携带的 DB-derived state 构造 bounded
catalog。catalog 只列出本次 candidate 可以使用的引用，不增加新的事实。

最小结构：

```json
{
  "goal_items": [
    {"goal_item_key": "result", "evidence_refs": ["receipt:node:attempt-1"]}
  ],
  "goal_gaps": [
    {"gap_key": "goal-item:result"}
  ],
  "execution_nodes": [
    {"node_key": "implementation"}
  ],
  "artifacts": [
    {"artifact_ref": "artifact:report"}
  ],
  "validator_revisions": [
    {"patch_base_revision": 0}
  ]
}
```

catalog 必须：

- 只来自当前 `db_state`；
- 保持 bounded，不复制完整 transcript；
- 不包含 API key、raw provider response 或完整 worker log；
- 使用 validator 已识别的引用 key；
- 空集合显式输出为空数组，避免模型发明替代引用。

## 6. Checkpoint Fact Schema

provider prompt 必须给出统一引用结构：

```json
{
  "source_refs": [
    {"gap_key": "goal-item:result"}
  ]
}
```

每个 `source_refs` item 只表达一个引用。MVP 支持：

- `{"goal_item_key": "..."}`；
- `{"evidence_ref": "..."}`；
- `{"gap_key": "..."}`；
- `{"node_key": "..."}`；
- `{"artifact_ref": "..."}`；
- `{"patch_base_revision": 0}`。

各 fact list 的最小约束：

| Fact list | 必需内容 | 合法来源 |
| --- | --- | --- |
| `satisfied_goal_items` | goal item、verified/waived 状态、摘要 | `goal_item_key`，有证据时同时引用 `evidence_ref` |
| `open_goal_gaps` | gap key、类型、摘要 | `gap_key` |
| `open_blockers` | 当前 blocker 摘要 | 对应 `gap_key` 或 `node_key` |
| `graph_frontier` | node key、类型、状态、摘要 | `node_key` |
| `artifact_index` | artifact 类型、引用、摘要 | `artifact_ref` |
| `validator_rejection_lessons` | rejection 摘要、base revision | `patch_base_revision` |
| `do_not_repeat` | 可复用 rejection 约束 | `patch_base_revision` |
| 其他事实列表 | 只在 catalog 或 segment entry 有明确来源时填写 | 至少一个 catalog 中的合法引用 |

模型无法为某项找到合法来源时，必须省略该 item 或保持对应 fact list 为空，不能发明引用。

## 7. Parser 与 Validator 边界

`parse_compaction_checkpoint()` 可以：

- 解析纯 JSON 或单一 JSON fence；
- 拒绝 graph patch；
- 检查 metadata 是 object；
- 补充 source segment、profile 和当前 revision metadata。

它不可以：

- 为 fact item 添加 `source_refs`；
- 将 item 文本与 DB 行模糊匹配后生成引用；
- 删除无法验证的 fact item 以帮助 candidate 通过；
- 将 unverified goal item 改成 verified；
- 将 validator rejection 转换成 accepted。

`validate_decision_checkpoint()` 继续独立检查 provenance、引用存在性、revision 和 satisfied
goal verification state。provider contract 改进不能削弱 validator。

## 8. Retry 策略

MVP 第一轮只增强原始 request/prompt，并用真实模型验证。若真实 candidate 仍因可修正的
checkpoint validator 问题被拒绝，再增加 bounded validator-aware repair：

- 最多一次 repair；
- repair 输入只包含原始 contract、原 candidate 和脱敏 validator reason；
- repair 仍然 no-tools、proposal-only；
- repaired candidate 必须重新经过完整 parser 和 validator；
- 原始 candidate、reject reason 和 repair attempt 必须审计；
- repair 不能调用 deterministic provider，也不能由 runtime 改写 candidate。

若 prompt contract 已使首次 candidate 通过，则 Phase 4G5 不引入 repair 协议。

## 9. 测试计划

默认离线测试必须覆盖：

- request 包含 provenance catalog 和 checkpoint fact schema；
- catalog 只包含当前 DB 中存在的 goal item、gap、node、artifact 和 revision；
- prompt 明确每个非空 fact item 必须带 `source_refs`；
- prompt 明确禁止发明引用；
- 历史真实输出形态中缺少 provenance 的 candidate 仍被 validator 拒绝；
- 带 catalog 合法引用的 provider candidate 在 no-fallback 模式 accepted；
- parser 不会自动补齐 `source_refs`；
- rejected candidate 保留 active segment，不产生 accepted checkpoint；
- accepted candidate rollover segment，并保留 provider audit。

真实 smoke 必须使用：

- 独立 `HERMES_HOME` 和 runtime DB；
- 独立 workspace；
- 从主 `.codex` 只读复制出的隔离 `CODEX_HOME`；
- 显式 provider/model/base URL/API key resolution；
- `fallback_to_deterministic=false`；
- bounded timeout 和调用次数；
- 运行前后主 `.codex` 内容哈希比对；
- 隔离 DB、报告和 workspace credential scan。

## 10. 验收标准

Phase 4G5 MVP 完成时：

- 中文阶段文档、roadmap 和真实验证台账一致；
- provenance catalog 与 fact schema 进入可审计 provider input；
- parser 没有 provenance 自动修补逻辑；
- 默认离线回归通过；
- 至少一次真实 no-fallback candidate 通过 checkpoint validator；
- accepted checkpoint 和 segment rollover 均来自真实 provider candidate；
- consistency 为 passed，credential scan 为 0 命中；
- 结果明确区分调用路径、candidate 质量与长期稳定性。

## 11. 后续关系

Phase 4G5 通过只证明单次真实 compaction candidate quality 达到 L3，不证明多轮 compaction
稳定性。后续 Phase 4G6 已把该能力放入 active long-run soak，覆盖多次 segment compaction、
checkpoint context chain、fallback quality degradation、supervisor lease/recovery、goal gap
reopen 和 anti-idle invariant，并完成三轮真实 no-fallback compaction。

## 12. 当前真实验证结果

2026-07-10 已在全新隔离 `HERMES_HOME`、独立 workspace 和从主 `.codex` 只读复制的
`CODEX_HOME` 中完成 L3。测试 job 从 production 初始化进入 `waiting_decision`，包含一个
required goal item、两个 open gap 和空 graph；compaction 使用当前真实模型源、
`token_budget_compaction` profile、`max_retries=0` 和 `fallback_to_deterministic=false`。

真实模型首次返回的 checkpoint candidate 即通过 parser 和 checkpoint validator。candidate
中的两个 `open_goal_gaps` 和一个 `open_blocker` 均自带 catalog 中存在的 `gap_key`
provenance；没有由 parser 补造引用，也没有触发 repair 或 deterministic fallback。

最终结果：

- provider parse status 为 `parsed`；
- provider validation 和 checkpoint validator status 均为 `accepted`；
- `fallback_used=false`；
- source segment 为 `compacted`，新 segment 为 `active`；
- consistency 为 `passed`，0 violations、0 warnings；
- 隔离 runtime DB/workspace credential scan 为 0 命中；
- 主 `.codex/config.toml` 和 `auth.json` 运行前后哈希不变。
- Runtime Kernel 与 CLI 离线回归 239 项通过，Runtime observability API 定向测试 1 项通过。

该结果满足 Phase 4G5 单次 candidate L3 门槛。它不证明多 provider、多 profile 或多轮长任务
compaction 的稳定性；其中 bounded multi-profile/multi-cycle 已由 Phase 4G6 验证，数小时与
多 provider soak 仍属于后续发布门槛。
