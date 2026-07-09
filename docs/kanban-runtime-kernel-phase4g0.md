# Hermes Kanban Runtime Kernel Phase 4G0：Runtime Memory Hints

Phase 4G0 的目标是为 runtime kernel 增加一层轻量、可人工审查、可回滚的经验提示
机制。它不是复杂学习系统，也不是第二套事实源；它更接近 Claude Code / Codex 的
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

Phase 4G0 的第一版只做 Runtime Memory Hints：

- runtime 在特定事件后生成 memory candidate；
- candidate 写入 Markdown；
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

## 4. Scope 分层

建议第一版支持四类 scope。

### 4.1 Runtime-global

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

### 4.2 Workspace / Repo

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

### 4.3 Domain / Job-family

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

### 4.4 Job-local

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

## 5. 权威性顺序

Memory scope 的加载优先级建议为：

```text
job-local checkpoint
workspace memory
domain memory
runtime-global memory
```

但权威性顺序必须是：

```text
DB facts / goal contract / progress ledger / validator / capability policy
      >
runtime guidance
      >
memory hints
```

也就是说，即使 workspace memory 很相关，也不能覆盖 DB、validator 或 policy。

## 5.1 Memory Trust Policy

Scope 只描述适用范围，不等于信任级别。第一版需要一套简单 trust policy：

```text
runtime-global/user-home accepted memory
      >
workspace accepted memory
      >
domain accepted memory
      >
candidate memory
```

规则：

- runtime-global/user-home memory 默认较可信，但必须抽象、短小，不包含项目事实；
- workspace memory 默认只对当前 workspace 生效；
- repo 中提交的 workspace memory 仍是 non-authoritative，必要时可以要求 operator
  opt-in；
- domain memory 是弱 hint，不能覆盖 workspace memory；
- candidate memory 永不注入；
- deprecated memory 永不注入；
- 解析失败的 memory 永不注入。

如果 workspace memory 来自不可信 repo 或外部 checkout，loader 应允许配置为
`workspace_memory_requires_opt_in=true`。第一版可以先记录 warning，不阻塞实现。

## 6. 文件布局

建议第一版使用 Markdown 文件。

### 6.1 Runtime guidance

稳定强规则：

```text
docs/runtime-guidance.md
~/.hermes/runtime-guidance.md
```

Guidance 应短小，每次注入 provider request 的稳定前缀。

### 6.2 Memory index

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

### 6.3 Topic files

Topic 文件：

```text
.hermes/runtime-memory/validator-boundaries.md
.hermes/runtime-memory/project-debugging-patterns.md
.hermes/runtime-memory/human-preferences.md
.hermes/runtime-memory/domains/backtesting.md
```

每个 topic 可以包含 accepted 和 deprecated sections。

### 6.4 Candidate files

自动生成候选：

```text
.hermes/runtime-memory/candidates/
```

Candidate 默认不提交、不注入、不影响运行时决策。Operator review 后可以 promote
到 accepted topic。

## 7. Memory Entry 格式

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

允许的 scope_type：

```text
global
workspace
domain
job
```

## 8. Candidate 生成触发

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

## 9. Promote / Deprecate 流程

第一版可以是人工文件编辑，不需要 UI。

Promote：

```text
candidates/foo.md
      |
      v
accepted topic file
```

Deprecate：

- 将 entry 移到 `Deprecated` section；
- 或把 `Status` 改为 `deprecated`；
- deprecated entry 不应注入 provider request；
- deprecated entry 可保留 provenance，用于解释旧决策。

Promote 后必须保留 source refs，不能只复制 lesson。

## 10. Provider Input 组成

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

## 11. Loader 设计

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

## 12. Event Types

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

## 13. Observability

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

## 14. 实现顺序

### Step 1：文档和文件约定

- 新增 Phase 4G0 文档；
- 更新 roadmap；
- 更新 AGENTS 约束；
- 定义 memory scope 和 entry format。

### Step 2：guidance / index loader

- 实现 runtime guidance loader；
- 实现 `MEMORY.md` index loader；
- 实现 workspace/global/domain 搜索路径；
- 不做 embedding。

### Step 3：hint selection

- 基于 objective、goal gaps、node types、validator rejection keywords 选择 topic；
- topic 内只选 accepted entry；
- 限制 max hints 和 token budget。

### Step 4：provider request 注入

- 在 decision provider request 中加入 selected hints；
- 标记 non-authoritative；
- 写入 `decision_segment_entries`。

### Step 5：candidate writer

- job done / validator rejection / anti-stuck recovery / human decision 后写 candidate；
- candidate 默认 local；
- candidate 必须带 provenance。

### Step 6：observability / tests

- runtime inspect 暴露 memory usage；
- focused tests 覆盖 scope、candidate、accepted、deprecated、non-authoritative。

## 15. 测试要求

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
- hint 注入不会改变 DB facts；
- hint 注入不会绕过 validator；
- hint 注入不会授权 capability；
- provider request 不全量包含所有 topic；
- hint usage 写入 decision segment entry；
- validator rejection 后可以生成 candidate；
- candidate 没有 provenance 时拒绝写入；
- 默认测试离线，不依赖真实 provider、网络或 embedding。

## 16. 完成定义

Phase 4G0 MVP 完成时必须满足：

- runtime 有 guidance / index / topic / candidate 的 Markdown 布局；
- memory entry 有明确 scope、status、applies_when、lesson、evidence；
- provider request 可以按 scope 和 gap 选择少量 accepted hints；
- candidate 默认不进入 provider request；
- memory parser 有硬约束，解析失败不注入；
- memory injection 有 token budget；
- candidate redaction 防止写入敏感原文；
- memory hint usage 可审计；
- memory 不影响 reducer、validator、completion、readiness、capability policy；
- focused tests 证明不会全量注入和不会跨 workspace 污染。

Phase 4G0 完成后，再进入：

```text
Phase 4G Synthetic Long-Run Soak and Real Compaction Smoke
Phase 4H Dashboard Runtime UI
```
