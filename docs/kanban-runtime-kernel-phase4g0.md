# Hermes Kanban Runtime Kernel Phase 4G0：Runtime Memory Lifecycle

Phase 4G0 的目标是为 runtime kernel 增加一层轻量、可人工审查、可回滚的经验生命
周期。它不是复杂学习系统，也不是第二套事实源；它更接近 Claude Code / Codex 的
`CLAUDE.md`、`AGENTS.md`、`MEMORY.md` 实践：稳定规则放 guidance，项目经验放
memory topic，当前 job 连续性仍由 DB facts、decision session 和 compaction
checkpoint 管理。

Phase 4G0 应在 Phase 4F capability policy 之后、Phase 4G synthetic long-run soak
之前实现。原因是 long-run soak 正好可以验证 memory hints 是否减少重复
validator rejection、noop decision、anti-stuck recovery 和相同失败路径。

## 1. 背景

当前 runtime kernel 已经有：

- DB 事实源；
- goal contract；
- progress ledger；
- execution graph；
- decision session；
- decision session compaction；
- worker recovery；
- capability/security policy 设计。

但系统缺少跨 job 的经验回流能力。每个 job 可以通过 checkpoint 保持当前任务的长期
上下文，但一个 job 中学到的 validator 边界、项目接口失败模式、human preference
或 debug strategy，不能自然地帮助下一个相似 job。

第一版不要实现 `runtime_experience_items`、confidence、promotion score、复杂
embedding store 或自动学习闭环。那会让系统很难验证，也容易把偶然失败升级成长期
规则。

Phase 4G0 的第一版只做 Runtime Memory Lifecycle MVP：

- runtime 在特定事件后生成 memory candidate；
- candidate 写入 Markdown；
- candidate 通过 deterministic schema / provenance / redaction validation；
- candidate 默认不注入未来 job；
- operator 或人工 review 后 promote 到 accepted memory；
- decision provider 构造输入时按 scope 和 goal/gap 检索少量相关 hints；
- hints 以 non-authoritative 形式注入；
- usage outcome 被记录，用于后续判断是否值得数据库化。

## 2. 非目标

Phase 4G0 不把 memory 当作事实源。

Phase 4G0 不让 memory 影响 readiness、completion、blocked、capability policy、
worker recovery、goal item satisfaction 或 patch validator。

Phase 4G0 不做自动 confidence 评分。

Phase 4G0 不要求 embedding 检索。第一版可以用关键词、topic index 和 scope filter。

Phase 4G0 不把所有 memory 全量注入每次 provider request。强规则可以常驻，经验
必须按需加载。

Phase 4G0 不替代 decision session compaction。Checkpoint 管当前 job 的长期连续性；
memory hints 管跨 job 的非权威经验。

## 3. 核心原则

### 3.1 Guidance 和 memory 分层

稳定、强约束、每次都应遵守的规则进入 runtime guidance。

经验性内容进入 memory topic。经验是 hint，不是 hard rule。

示例：

- guidance：`LLM 只能返回 graph patch proposal`；
- guidance：`不得 release_node`；
- guidance：`worker receipt 不是 runtime compaction`；
- memory hint：`在本仓库的 backtest 任务中，data provider 和 engine 容易因字段名不一致失败，优先插入 data contract verifier`。

### 3.2 Scope 必须显式

Memory 条目必须声明适用范围。没有 scope 的 memory 不应注入。

Hermes 是后台 runtime，会跨 job、workspace、worker lane 和模型 provider 运行。
如果 memory 没有 scope，经验会跨项目错误迁移，造成上下文污染。

### 3.3 Candidate 默认不注入

自动生成的经验候选只能进入 candidate 区域。Candidate 不应默认进入 provider
request。只有人工确认或 operator promote 后，才进入 accepted memory。

### 3.4 Hints 非权威

Memory hints 注入 provider request 时必须带明确边界：

```text
These are non-authoritative memory hints. DB state, goal contract,
validator rules, capability policy, and current checkpoint override them.
```

### 3.5 Usage 必须可审计

每次 memory hint 被注入，都应记录 usage event 或 decision segment entry：

- 注入了哪些 topic / entry；
- 当前 job / goal / gap；
- 后续 patch accepted / rejected / noop；
- 是否产生 progress ledger 推进；
- 是否出现 validator rejection。

第一版只记录 outcome，不做自动评分。

Usage 记录应优先落在 `decision_segment_entries`，并与 `decision_id`、goal gap、
provider request 和后续 validator result 对齐。不要另写一个无法关联 patch 结果的
普通日志。

### 3.6 Memory 不能授权 capability

Memory hint 不能影响 Phase 4F 的 capability authorization。

例如 memory 写“这个项目通常允许网络查包”，也只能提示 provider 申请
`network_access` 或提出 human gate。最终是否允许，仍由 runtime capability policy
和 human authorization 决定。Memory 永远不能成为权限来源。

### 3.7 Memory 不进入 checkpoint

Checkpoint 是当前 job 的压缩状态，Runtime Memory 是跨 job 的历史经验。两者不能
混合。

Decision session compaction 可以在 checkpoint 中记录“本轮使用过哪些 memory hint
refs”，用于审计 provider input；但不得把 memory hint 正文写成当前 job 的事实状态，
也不得把 memory lesson 变成 `key_decisions`、`known_failure_boundaries` 或
`do_not_repeat`。

如果某条 memory hint 在当前 job 中被事实验证，应通过 progress ledger、execution
event、artifact 或 human decision 进入 DB，再由 checkpoint 汇总这些当前 job 事实。
Memory 本身不能绕过这条事实链路。

## 4. Memory 生命周期

Runtime Memory 的生命周期是：

```text
runtime execution
      |
      v
memory candidate
      |
      v
validation checks
      |
      v
accepted memory
      |
      v
future decision hint
      |
      v
usage outcome
      |
      v
deprecated memory
```

第一版只把 `candidate`、`accepted`、`deprecated` 作为持久状态。

`validated` 不建议在 Phase 4G0 中实现成独立持久状态。它应被视为 promote 前的
校验步骤，包括 schema validation、provenance check、redaction check 和 scope
check。这样可以保留生命周期语义，但避免第一版引入难以验证的半状态。

### 4.1 Candidate

Candidate 是从 runtime trace 中抽取的候选经验。

候选只应来自可复用模式，而不是“发生过什么”。优先触发来源包括：

- validator 连续拒绝；
- anti-stuck recovery 后产生 progress；
- successful recovery；
- human decision 修改目标或授权 capability；
- milestone completion；
- 复杂任务成功闭环；
- verifier failed 后 debug / strategy_update 修复；
- worker recovery 重试后成功。

低价值事件默认不生成 candidate：

- 普通 node success；
- 普通代码修改；
- 一次性测试失败；
- 没有 recovery pattern 的普通 bug fix；
- 无 validator / verifier / progress ledger 关联的模型总结。

Candidate 默认不进入 provider request，不参与 retrieval，不影响 runtime state。

### 4.2 Validation Checks

Candidate 进入 accepted memory 前必须通过 deterministic validation：

- `Status` / `Scope` / `Applies when` / `Lesson` / `Evidence` / `Use as` 字段存在；
- source refs 指向存在的 job、event、decision、patch、ledger、artifact 或 node；
- sensitive data 已 redacted；
- `Use as` 明确是 non-authoritative decision hint；
- scope 与目标位置相容；
- lesson 没有把一次偶然失败写成硬规则。

Validation checks 只决定 candidate 是否可以 promote。它不能让 candidate 直接进入
provider request。

### 4.3 Accepted

Accepted memory 可以被未来 job 按 scope、goal/gap 和 keyword 检索。

Phase 4G0 MVP 只要求人工或 operator promote。Repeated successful usage 和
automatic validation policy 可以作为未来扩展，不应在第一版自动 promotion。第一版
只记录 usage outcome，不自动调整 confidence 或状态。

Accepted memory 仍然不是事实，不影响 reducer、validator、completion、readiness、
capability policy 或 worker recovery。

### 4.4 Deprecated

以下情况可以进入 deprecated：

- 长期无使用；
- 使用后持续导致 rejected patch；
- 适用范围失效；
- 项目环境变化；
- operator 判断经验已经过时。

Deprecated memory 保留审计和 provenance，但默认不参与 retrieval，也不得注入
provider request。

## 5. Scope 分层

建议第一版支持四类 scope。

### 5.1 Runtime-global

位置：

```text
~/.hermes/runtime-memory/
```

适用：

- 所有 Hermes runtime job。

内容：

- 通用 validator 边界经验；
- runtime patch 写法经验；
- anti-stuck 策略经验；
- 不包含项目事实。

限制：

- 必须短；
- 必须抽象；
- 不允许包含某个 repo 的具体接口、文件路径、测试命令或业务事实；
- 默认弱注入。

### 5.2 Workspace / Repo

位置：

```text
<workspace>/.hermes/runtime-memory/
<workspace>/docs/runtime-memory/
```

适用：

- 当前 workspace / repo。

内容：

- 项目架构经验；
- 测试命令；
- 接口边界；
- 常见失败模式；
- human preference；
- provider / verifier / artifact 约定。

这是最重要的 memory scope。Workspace memory 可以被提交协作，也可以存在本地
`.hermes` 目录。是否提交由项目约定决定。

### 5.3 Domain / Job-family

位置：

```text
~/.hermes/runtime-memory/domains/
<workspace>/.hermes/runtime-memory/domains/
```

适用：

- 相似任务类型，例如 backtesting、CLI migration、provider integration、
  dashboard UI、database migration。

内容：

- 跨项目但同领域的弱经验。

限制：

- 必须弱注入；
- 不能覆盖 workspace memory；
- 不应包含某个项目的具体事实，除非 scope 明确限制到该项目。

### 5.4 Job-local

位置：

```text
runtime DB
decision session checkpoint
job artifacts
```

适用：

- 当前 job。

内容：

- 当前目标解释；
- 当前 open gaps；
- 当前 human decisions；
- 当前 validator rejection lessons；
- 当前 artifact index；
- 当前 do-not-repeat。

Job-local 不是长期 memory。它由 decision session segment 和 checkpoint 管理。
不要把 job-local checkpoint 当成跨 job memory。

## 6. 权威性顺序

Memory scope 的加载优先级与权威性不同。

当前 job 的 checkpoint 属于 job-local context，不是跨 job memory。它在 provider
input 中仍然位于 selected memory hints 之前，用于表达当前任务的压缩状态。

跨 job memory 的选择优先级建议为：

```text
workspace accepted memory
      >
domain accepted memory
      >
runtime-global accepted memory
```

原因是 workspace memory 与当前 repo/工作目录最相关；domain memory 次之；
runtime-global memory 必须抽象、短小、弱注入。

权威性顺序必须是：

```text
DB facts / goal contract / progress ledger / validator / capability policy
      >
runtime guidance
      >
memory hints
```

也就是说，即使 workspace memory 很相关，也不能覆盖 DB、validator 或 policy。

## 6.1 Memory Trust Policy

Scope 只描述适用范围，不等于信任级别。第一版需要一套简单 trust policy：

```text
accepted user-home / runtime-global memory
      >
accepted workspace memory
      >
accepted domain memory
      >
candidate / deprecated / parse-failed memory
```

规则：

- runtime-global/user-home memory 可以更可信，但必须抽象、短小，不包含项目事实；
- workspace memory 默认只对当前 workspace 生效；
- repo 中提交的 workspace memory 仍是 non-authoritative，必要时可以要求 operator
  opt-in；
- domain memory 是弱 hint，不能覆盖 workspace memory；
- candidate memory 永不注入；
- deprecated memory 永不注入；
- parse-failed memory 永不注入。

如果 workspace memory 来自不可信 repo 或外部 checkout，loader 应允许配置为
`workspace_memory_requires_opt_in=true`。第一版可以先记录 warning，不阻塞实现。

## 7. 文件布局

建议第一版使用 Markdown 文件。

### 7.1 Runtime guidance

稳定强规则：

```text
docs/runtime-guidance.md
~/.hermes/runtime-guidance.md
```

Guidance 应短小，每次注入 provider request 的稳定前缀。

### 7.2 Memory index

索引文件：

```text
.hermes/runtime-memory/MEMORY.md
docs/runtime-memory/MEMORY.md
~/.hermes/runtime-memory/MEMORY.md
```

`MEMORY.md` 只做索引，不放大量经验正文。

示例：

```md
# Runtime Memory Index

## Topics

- validator-boundaries.md
  - scope: workspace
  - keywords: validator, patch rejection, graph patch

- project-debugging-patterns.md
  - scope: workspace
  - keywords: tests, verifier, failure recovery

- domains/backtesting.md
  - scope: domain
  - keywords: backtest, market data, strategy runner
```

### 7.3 Topic files

Topic 文件：

```text
.hermes/runtime-memory/validator-boundaries.md
.hermes/runtime-memory/project-debugging-patterns.md
.hermes/runtime-memory/human-preferences.md
.hermes/runtime-memory/domains/backtesting.md
```

每个 topic 可以包含 accepted 和 deprecated sections。

### 7.4 Candidate files

自动生成候选：

```text
.hermes/runtime-memory/candidates/
```

Candidate 默认不提交、不注入、不影响运行时决策。Operator review 后可以 promote
到 accepted topic。

## 8. Memory Entry 格式

第一版使用 Markdown，要求结构固定但不需要 YAML frontmatter。

示例：

```md
## provider-backtest-schema-alignment

Status:
- accepted

Scope:
- scope_type: workspace
- scope_ref: /repo/hermes-agent
- applies_to:
  - paths: ["hermes_cli/", "docs/kanban-runtime-kernel-*"]
  - keywords: ["backtest", "market data", "strategy runner", "provider"]
- excludes:
  - unrelated repositories

Applies when:
- goal mentions backtest, market data, strategy runner, provider, or data ingestion
- graph contains both data provider and backtest engine nodes

Lesson:
- Insert a data contract verifier before end-to-end backtest verification.
- Do not let backtest engine assume raw provider field names directly.

Evidence:
- source_job: rjob_xxx
- source_event: 123
- source_decision: kdec_xxx
- source_patch: gpatch_xxx
- source_validator_rejection: verify-provider-contract failed because provider emitted `close` while engine expected `price`
- recovery: align-provider-contract node fixed the mismatch

Use as:
- non-authoritative decision hint

Source:
- generated_from: runtime_trace
- reviewed_by: operator

Confidence:
- optional; not used for automatic scoring in Phase 4G0

Usage:
- optional; updated from audited usage outcome, not used for automatic promotion in Phase 4G0
```

必须字段：

```text
Status
Scope
Applies when
Lesson
Evidence
Use as
```

Parser 硬约束：

- 缺少 `Status` 不注入；
- 缺少 `Scope` 不注入；
- 缺少 `Applies when` 不注入；
- 缺少 `Lesson` 不注入；
- 缺少 `Evidence` 不注入；
- 缺少 `Use as` 不注入；
- `Status` 不是 `accepted` 不注入；
- `Use as` 不是 non-authoritative hint 不注入；
- `scope_type` 未知不注入；
- `scope_ref` 与当前 workspace/job/domain 不匹配不注入。

允许的 status：

```text
candidate
accepted
deprecated
```

`validated` 不是 Phase 4G0 的持久 status。它是 promote 前 validation checks 的执行
结果。

允许的 scope_type：

```text
global
workspace
domain
job
```

## 9. Memory Read Path

Memory read path 必须由 runtime kernel 控制，不能让 decision provider 随意读取
memory 目录。

正确读取流程：

```text
current goal contract
      +
current open gaps
      +
recent validator/recovery pattern
      |
      v
load small MEMORY.md index
      |
      v
scope / keyword / goal-gap filter
      |
      v
read selected topic entries
      |
      v
parse accepted entries
      |
      v
rank under budget
      |
      v
inject non-authoritative hints
```

禁止读取流程：

```text
runtime-memory/**/*.md
      |
      v
concat all files
      |
      v
provider context
```

`MEMORY.md` index 可以作为轻量入口，但 topic 正文必须按需读取。第一版不需要 memory
tool，也不需要 provider 自行 grep。Runtime kernel 根据 current goal、open gap、
recent failure pattern 和 workspace/domain scope 选择 top K entries。

## 10. Memory Write Path

Memory write path 不从完整 conversation transcript、worker log 或未验证模型输出
生成经验。

允许输入：

- goal contract；
- progress ledger；
- execution events；
- graph patches；
- validator results；
- recovery result；
- final outcome；
- artifact summary；
- human decision metadata。

禁止输入：

- raw provider transcript；
- raw worker log；
- 大段测试输出；
- 未经 validator 或 reducer 归档的模型总结；
- secret、credential、token 或完整外部响应。

Memory candidate 必须表达可复用模式。它应该回答：

- 适用于什么 goal/gap/context；
- 学到了什么 lesson；
- 证据来自哪些 source refs；
- 后续应作为什么级别的 hint 使用。

## 11. Candidate 生成触发

第一版候选生成应尽量 deterministic。

触发点：

- job done；
- validator 连续拒绝；
- anti-stuck recovery 后产生 progress；
- human decision 修改目标或授权能力；
- verifier failed 后 debug / strategy_update 修复；
- compaction fallback 连续发生；
- worker recovery 重试后成功。

候选生成规则：

- 从 DB facts、events、validator result、progress ledger 和 artifact summary 中抽取；
- 不让模型自由编造经验；
- 没有 provenance 不写 candidate；
- 写入前执行 redaction；
- candidate 状态默认 `candidate`；
- candidate 默认写入 local `.hermes/runtime-memory/candidates/`。

Redaction 规则：

- 去除 API key、token、credential、secret 片段；
- 去除完整外部响应和大段日志；
- 去除客户数据、业务敏感原文和不必要的内部 URL；
- 路径只保留必要的相对路径或抽象路径；
- human decision 中的敏感偏好或凭据不得原文写入；
- accepted memory 只保留抽象 lesson 和必要 provenance。

Candidate 即使默认不注入，也不能写入敏感原文。否则后续 promote 或提交时会造成
泄露。

## 12. Promote / Deprecate 流程

第一版可以是人工文件编辑，不需要 UI。

Promote：

```text
candidates/foo.md
      |
      v
validation checks
      |
      v
accepted topic file
```

Phase 4G0 MVP 的 accepted 条件：

- candidate 已通过 schema validation；
- candidate 有 source refs，且 source refs 可追溯；
- applicability 清楚，不是泛泛经验；
- lesson 清晰、可执行、非强制 workflow；
- redaction 已完成；
- `Use as` 是 non-authoritative decision hint；
- 人工或 operator 明确 promote。

以下条件可以作为未来自动 promotion 的输入，但第一版只记录，不自动 promote：

- 相同 pattern 多次出现；
- hint 多次使用后 patch accepted；
- hint 使用后 progress ledger 有推进；
- hint 使用后减少同类 validator rejection。

Deprecate：

- 将 entry 移到 `Deprecated` section；
- 或把 `Status` 改为 `deprecated`；
- deprecated entry 不应注入 provider request；
- deprecated entry 可保留 provenance，用于解释旧决策。

Promote 后必须保留 source refs，不能只复制 lesson。

## 13. Provider Input 组成

Decision provider request 建议组成：

```text
stable runtime contract
runtime guidance
current goal contract
latest validated checkpoint
selected memory hints
short tail
current delta
```

Memory hints 的注入规则：

- guidance 每次注入；
- `MEMORY.md` index 可轻量读取；
- topic 内容按 goal/gap/node/rejection keyword 选择；
- 默认最多注入 3-5 条 hint；
- 每条 hint 有 token budget；
- candidate 不注入；
- deprecated 不注入；
- domain/global hint 弱于 workspace hint；
- hint 必须带 scope 和 provenance 的简短版本。

预算建议：

```text
max_hints: 5
max_tokens_per_hint: 240
max_total_memory_tokens: 1200
max_provider_input_ratio: 0.10
```

如果 budget 不足，选择顺序为：

```text
workspace accepted memory
      >
domain accepted memory
      >
runtime-global accepted memory
```

同一 scope 内优先选择 applies_when 与当前 goal/gap/rejection 最匹配的条目。

禁止：

- 每轮全量 grep 所有 memory；
- 把 memory 当 system prompt hard rule；
- 把 memory 写入 DB fact；
- 用 memory 绕过 validator。

## 14. Loader 设计

建议接口：

```python
load_runtime_guidance(conn, job_id) -> RuntimeGuidance
load_runtime_memory_index(conn, job_id, workspace_path=None) -> MemoryIndex
select_runtime_memory_hints(conn, job_id, delta, *, max_hints=5, budget_tokens=1200) -> list[MemoryHint]
record_memory_hint_usage(conn, job_id, decision_id, hints, outcome=None) -> None
write_runtime_memory_candidate(conn, job_id, trigger_event_id, kind) -> Path
```

第一版可以不建表，只把 usage 写入：

- `decision_segment_entries`；
- `execution_events`；
- provider request audit。

`record_memory_hint_usage()` 至少应记录：

```text
decision_id
segment_id
topic
entry_id
scope_type
scope_ref
goal_gap_keys
node_keys
selected_reason
injected_tokens
provider_request_ref
outcome
```

`outcome` 可以在 validator result 后回填，或追加
`memory_hint_outcome_recorded` event。

## 15. Usage Feedback Metrics

Memory usage 不应只记录 created_at 和 content。第一版至少应支持按 entry 聚合以下
指标，哪怕聚合结果先来自 `decision_segment_entries` 和 events：

```text
retrieved_count
used_in_decision_count
patch_accept_count
patch_reject_count
patch_noop_count
goal_progress_count
validator_rejection_after_use_count
last_used_at
last_outcome
```

这些指标用于未来 ranking、deprecation 和人工 review。Phase 4G0 MVP 不应基于这些
指标自动 promote candidate，也不应让这些指标改变 runtime correctness。

有害或低价值 memory 的处理原则：

- 经常被检索但从不进入 provider input，应降权或缩小 applies_when；
- 经常导致 patch rejected / noop，应进入 operator review；
- 多次使用后无 progress ledger 推进，应考虑 deprecated；
- scope 过宽导致跨 workspace 污染，应拆分或降级为 workspace-local。

## 16. Event Types

建议新增：

```text
memory_candidate_created
memory_candidate_promoted
memory_candidate_deprecated
memory_hint_selected
memory_hint_used
memory_hint_outcome_recorded
```

`memory_hint_used` payload：

```json
{
  "topic": "validator-boundaries.md",
  "entry_id": "provider-backtest-schema-alignment",
  "scope_type": "workspace",
  "scope_ref": "/repo/hermes-agent",
  "decision_id": "kdec_xxx",
  "goal_gap_keys": ["backtest_output:needs_verification"],
  "non_authoritative": true
}
```

## 17. Observability

`runtime_observability_snapshot()` 可以新增：

```json
{
  "memory": {
    "guidance_loaded": true,
    "selected_hints": [],
    "candidate_count": 0,
    "latest_candidate": null,
    "recent_usage": []
  }
}
```

Dashboard 第一版只读即可。

需要回答：

- 当前 decision 使用了哪些 memory hint；
- hint 来自哪个 scope；
- candidate 有没有生成；
- candidate 是否已 promote；
- hint 使用后 patch 是 accepted、rejected 还是 noop。

## 18. 实现顺序

### Step 1：文档和文件约定

- 更新 Phase 4G0 文档；
- 更新 roadmap；
- 更新 AGENTS 约束；
- 定义 memory scope 和 entry format。

### Step 2：guidance / index loader

- 实现 runtime guidance loader；
- 实现 `MEMORY.md` index loader；
- 实现 workspace/global/domain 搜索路径；
- 不做 embedding。

### Step 3：memory read path / hint selection

- 基于 objective、goal gaps、node types、validator rejection keywords 选择 topic；
- topic 内只选 accepted entry；
- 限制 max hints 和 token budget。

### Step 4：provider request 注入

- 在 decision provider request 中加入 selected hints；
- 标记 non-authoritative；
- 写入 `decision_segment_entries`。

### Step 5：memory write path / candidate writer

- job done / validator rejection / anti-stuck recovery / human decision 后写 candidate；
- candidate 默认 local；
- candidate 必须带 provenance。

### Step 6：promotion / usage feedback

- 实现 candidate validation checks；
- 支持人工 promote / deprecate；
- 记录 usage outcome 和聚合指标来源。

### Step 7：observability / tests

- runtime inspect 暴露 memory usage；
- focused tests 覆盖 scope、candidate、accepted、deprecated、non-authoritative。

## 19. 测试要求

必须覆盖：

- guidance 每次注入；
- candidate 不注入；
- candidate 写入前 redaction；
- accepted workspace memory 只对同 workspace 注入；
- workspace memory 可配置为需要 operator opt-in；
- global memory 不包含项目事实；
- domain memory 弱于 workspace memory；
- deprecated memory 不注入；
- 解析缺少 Status / Scope / Evidence / Use as 的条目不注入；
- memory hints 有 max count 和 token budget；
- `MEMORY.md` index 可轻量读取，但 topic 正文按需读取；
- provider request 不全量包含所有 memory topic；
- runtime kernel 控制 memory selection，provider 不自行 grep memory 目录；
- hint 注入不会改变 DB facts；
- hint 注入不会绕过 validator；
- hint 注入不会授权 capability；
- hint 正文不写入 checkpoint payload；
- hint usage 写入 decision segment entry；
- usage feedback 至少能表达 retrieved / used / accepted / rejected / progress；
- validator rejection 后可以生成 candidate；
- candidate 没有 provenance 时拒绝写入；
- 普通 node success 不生成 candidate；
- raw worker log / raw transcript 不生成 candidate；
- candidate 通过 validation checks 前不能 promote；
- accepted memory 才能进入 hint selection；
- usage outcome 不会自动 promote candidate；
- 默认测试离线，不依赖真实 provider、网络或 embedding。

## 20. 完成定义

Phase 4G0 MVP 完成时必须满足：

- runtime 有 guidance / index / topic / candidate 的 Markdown 布局；
- runtime memory lifecycle 明确区分 candidate / validation checks / accepted / deprecated；
- memory entry 有明确 scope、status、applies_when、lesson、evidence；
- candidate 必须通过 schema、provenance、redaction 和 scope validation 才能 promote；
- provider request 可以按 scope 和 gap 选择少量 accepted hints；
- memory read path 使用 index + scope/goal/gap filter + selected topic entries，不全量读取；
- memory write path 只从 DB facts/events/validator/progress/recovery/final outcome 中生成 candidate；
- candidate 默认不进入 provider request；
- deprecated 默认不进入 provider request；
- memory parser 有硬约束，解析失败不注入；
- memory injection 有 token budget；
- candidate redaction 防止写入敏感原文；
- memory hint usage 可审计；
- memory usage metrics 可用于未来 ranking/deprecation，但第一版不自动改变状态；
- memory hint 内容不进入 checkpoint payload，checkpoint 最多记录 memory hint refs；
- usage outcome 第一版只记录，不自动 promotion、不自动 confidence scoring；
- memory 不影响 reducer、validator、completion、readiness、capability policy；
- focused tests 证明不会全量注入和不会跨 workspace 污染。

Phase 4G0 完成后，再进入：

```text
Phase 4G Synthetic Long-Run Soak and Real Compaction Smoke
Phase 4H Dashboard Runtime UI
```
