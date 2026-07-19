# Phase 4G12 Small 真实验证执行总结

## 1. 最终结论

最终 run `phase4g12-small-20260719-161352` 证明 Runtime Kernel 已具备最小可用的
evidence-driven active graph mutation：初始 graph 没有 legacy adapter node；parser worker 在
非终态 semantic checkpoint 中发现独立 gap；真实 Decision Provider 根据全局 snapshot 创建新
durable node、补 dependency，并同时改变两个既有 active node 的下一段执行。

| 维度 | 结果 |
| --- | --- |
| Runtime correctness | 通过，consistency 0 violation / 0 warning |
| Dynamic topology mutation | 通过，`legacy-token-adapter` 由 event 22 candidate 动态创建 |
| Active-node control | 通过，parser/renderer 收到 directive 并恢复原 session |
| Directive delivery | 通过，3 条 directive 全部 acknowledged |
| Durable contribution | 通过，3 份 isolated patch 均冻结并由 primary 接受 |
| Task capability | 通过，goal satisfied，最终 unittest 2/2 通过 |
| Evaluator | 未创建；本任务没有独立 oracle 要求 |

准确结论是：

> Runtime 已能在 cooperative safe point 根据一个 worker 的阶段性语义 evidence 修改 active
> execution graph。它既能改变已有责任的下一段 context，也能新增独立、可恢复的 durable
> responsibility，并纳入原 integration owner 的完成闭环。

这里的“即时”是下一次 coordination epoch 生效，不是向正在生成 token 的模型 turn 异步注入。

## 2. 任务与初始拓扑

Small repository 是一个 token pipeline：

- parser 初始返回 lowercase string；
- renderer 初始只能 join string；
- tests 要求统一的 `{'kind': 'word', 'text': value}` contract；
- legacy fixture 还要求 `src.token_compat.adapt_legacy_tokens`，但该模块不存在。

初始 graph 只包含：

```text
parser-contract ---------\
                          +--> pipeline-integration
renderer-contract -------/
```

`legacy-token-adapter` 没有预置。初始 node key 为：

```text
parser-contract
pipeline-integration
renderer-contract
```

因此最终出现第四个 node 不能归因于 fixture 静态拆分。

## 3. 第一阶段：真实 worker 产生结构证据

parser 与 renderer 在两个 isolated worktree 和两个独立 Codex thread 中并行执行第一段工作。

### 3.1 Parser checkpoint

Parser：

1. 检查 pipeline test；
2. 只修改 `src/token_parser.py`；
3. 定义并验证 versioned word-token contract；
4. 运行 full discovery，得到 `ModuleNotFoundError: src.token_compat`；
5. 将该缺口作为 `gap_discovered` finding；
6. 产生一个 non-authoritative responsibility candidate。

Candidate 关键字段：

```text
candidate_key: legacy-token-adapter
reason_type: execution_discovered_gap
declared_write_scope: src/token_compat.py
goal_item: versioned-token-pipeline
integration_owner: pipeline-integration
evidence: workspace:path:tests/test_pipeline.py
```

Parser 没有直接创建 node、修改 graph、写 ledger 或声明 goal 完成。

### 3.2 Renderer checkpoint

Renderer：

1. 检查 renderer 与 pipeline test；
2. 不修改 workspace；
3. 报告当前 string renderer 与 versioned token contract 冲突；
4. 进入 `blocking_dependency` safe point；
5. 按受控责任边界保持 `responsibility_candidates=[]`。

两个 checkpoint canonical ingest 后，parser/renderer 均进入 `waiting_coordination`，没有写入
completion ledger fact。

## 4. 第一轮全局 Decision：动态扩图

Decision Provider 同时看到：

- 当前 Goal Contract；
- 初始三节点 graph；
- parser 与 renderer 的 active responsibility；
- 两个 coordination checkpoint；
- parser candidate 的 goal、scope、owner 和 evidence；
- 当前 graph revision 1。

Provider 首次生成的 patch 即通过本地 validator，没有 validator retry：

```text
create_node legacy-token-adapter
    source_responsibility_ref = event:22#responsibility:legacy-token-adapter

add_dependency
    legacy-token-adapter -> pipeline-integration

issue_directive -> parser-contract
issue_directive -> renderer-contract
```

Decomposition 使用：

```text
type: execution_discovered_gap
nodes: [legacy-token-adapter]
evidence_refs: [event:22]
integration_owner_node_key: pipeline-integration
```

该原子 patch 将 graph revision 从 1 提升到 2。最终拓扑变为：

```text
parser-contract ----------------\
renderer-contract ---------------+--> pipeline-integration
legacy-token-adapter ------------/
```

这一步是本阶段相对 Phase 4G11 的核心新增证据。Phase 4G11 只改变已有 node context；本 run 的
Decision Provider 真实创建了运行前不存在的责任节点。

## 5. 第二阶段：三个 child 执行

### 5.1 Existing session resume

Parser 和 renderer 收到 DB-backed directive 后恢复原 Codex thread：

| Node | 执行模式 | Resume count | 结果 |
| --- | --- | ---: | --- |
| parser-contract | 原 session resume | 1 | succeeded |
| renderer-contract | 原 session resume | 1 | succeeded |

Parser 保持 parser-only scope；renderer 只修改 `src/token_renderer.py`，把 token object 渲染为
`kind:text`。两条 terminal receipt 均包含对应 `consumed_directive_ids`，directive 进入
`acknowledged`。

### 5.2 Dynamic child

`legacy-token-adapter` 使用新的 isolated worktree 和 fresh Codex thread。它只创建
`src/token_compat.py`，把 legacy string 转换为统一 token object。

它的 isolated full suite 仍然看到 base parser/renderer，因此报告 integration risk，而没有越过
scope 修改 sibling 文件。该 node 在 safe point 提交第三个 coordination checkpoint。

### 5.3 第二轮全局 Decision

第二轮 Provider 没有再次扩图，只向 adapter 发一条 routing-only `continue` directive：

- 保留当前 adapter patch；
- 不进入 parser/renderer scope；
- isolated full-suite failure 不被误判为 adapter failure；
- 交由既有 primary integration owner 组合三份 contribution。

Graph revision 从 2 提升到 3。Adapter 恢复同一 Codex thread，ACK directive，随后 succeeded。

## 6. Contribution 与最终集成

三个 child patch 都由 Runtime 冻结：

| Node | Artifact | Bytes | Patch SHA-256 |
| --- | --- | ---: | --- |
| renderer-contract | `art_82247e5809c2` | 371 | `7d80086a05bcfa6ff4fcc48ae43dc34aef1d0855b2fd49d5cd139b138c90b357` |
| parser-contract | `art_c3c9e599ef58` | 545 | `ac8e4e8326eeaf9d482364923b709d1acac8f1f4333d1fd9d178157f8812f7bf` |
| legacy-token-adapter | `art_baddf0f2023a` | 298 | `d7dd16e1d8a86fb03f49aed902a66f288c86630a78a465bcd575aeff40a17bd4` |

Primary integration owner：

1. 校验三份 patch hash；
2. 在 shared workspace 应用三份 patch；
3. 将三个 artifact 全部列入 `accepted_contributions`；
4. 没有 modified 或 rejected contribution；
5. 运行 `python -m unittest discover -s tests`；
6. 2 tests 通过；
7. Runtime 写入 `contribution_attribution_verified`；
8. goal item 进入 `satisfied`，job 进入 `done`。

## 7. 可观测数据

| 指标 | 值 |
| --- | ---: |
| 初始 durable node | 3 |
| 最终 durable node | 4 |
| 动态创建 node | 1 |
| Materialization | 7 |
| Decision round | 2 |
| Applied graph patch | 3（含 fixture 初始 patch） |
| Coordination checkpoint | 3 |
| Responsibility candidate | 1 |
| Directive | 3，全部 acknowledged |
| Frozen contribution | 3 |
| Worker session | 4 |
| Same-session resume | 3 |
| Progress ledger fact | 4 |
| 总 wall time | 约 6 分 19 秒 |
| Codex turn | 7 |
| Codex input tokens | 997,620 |
| Codex cached input tokens | 785,920 |
| Cache ratio | 约 78.8% |
| Codex output tokens | 32,187 |

Token 是 Codex CLI 的 turn usage 汇总，不含 Decision Provider token。这个 Small run 成本明显偏高，
说明 active coordination 和 durable isolation 的价值已经可证明，但还不能据此证明它比 native
single-worker execution 更高效。

## 8. 迭代 attempt 与系统改进

最终 green run 之前保留了四次失败事实：

| Attempt | 分类 | 发现 | 修正 |
| --- | --- | --- | --- |
| `160314` | Runtime contract / fixture | renderer 也提出 candidate，并错误地把 isolated self 设为 integration owner | Runtime footer 增加明确的 `runtime_integration_owner_node_key`；validator 保持拒绝错误 owner |
| `160609` | Infrastructure invalid | 模型源 WebSocket 连续重连，未进入业务执行 | 终止无效 run；同源改用已验证的 HTTP transport |
| `160822` | Validator 过严 | parser checkpoint 顶层是 `shared_contract_changed`，内部已有 `gap_discovered` finding，candidate 被误拒绝 | candidate 改为必须引用结构性 finding evidence，不再只看顶层 kind |
| `161118` | Runner environment | worker checkpoint 已通过，但系统 Python 缺少 `httpx`，Decision Provider 未启动 | 正式 runner 使用项目 `.venv/bin/python` |
| `161352` | Valid | 完整 topology mutation、resume、ACK、freeze、integration、completion | 通过 |

这些失败没有通过脚本直接修改 graph、directive、ledger 或 completion。最终 run 的所有成功事实都来自
正常 worker task/run/receipt、Decision Provider patch 和本地 reducer/validator 路径。

## 9. 这次证明了什么

本 run 证明以下因果链真实成立：

```text
parser 发现 out-of-scope durable gap
    -> canonical responsibility candidate
    -> Runtime 全局 active graph snapshot
    -> Decision Provider 创建新 child + dependency
    -> parser/renderer 收到 active-node directive
    -> existing sessions resume and ACK
    -> dynamic child 独立执行并 checkpoint
    -> 第二轮 Runtime routing
    -> 三份 contribution 冻结
    -> primary 集成并完成 goal
```

所以当前系统已经不只是单 worker 外层状态机，也不只是预置多节点后等待全部结束。DB 中的阶段性语义
状态实际改变了 graph topology 和其他 active responsibility 的下一段执行。

## 10. 尚未证明

- 本 run 是受控 Small，不证明任意 Large 任务都应拆分；
- candidate 的存在不强制扩图，普通任务仍应优先由 coherent worker 完成；
- 不支持 mid-turn async injection；
- 不证明 Runtime orchestra 的最终质量优于 native communicating subagents；
- 不证明 997K input-token 成本合理；
- Dynamic node 的长期 crash recovery、capability boundary 和跨进程 daemon takeover 仍需复用既有
  production validation，而不是由本 Small 单独推断。

## 11. 证据索引

- Runtime job：`rjob_8ef1528aa3e1`；
- Base revision：`b7eb9eef76a247bc3625dc5ec25f2e582c254cc4`；
- 结构化报告：`run-report.json`；
- 三份公开压缩 patch：`parser-contract.patch.gz`、`renderer-contract.patch.gz`、
  `legacy-token-adapter.patch.gz`；
- 原始证据归档：
  `/root/hermes-validation-artifacts/phase4g12/dynamic-small/phase4g12-small-20260719-161352`；
- Verified manifest SHA-256：
  `33e2c05c4e586a1c9106b27dfa78e2059151e55218201f4e509ff2a3cb5a588f`。

## 12. 回归验证

本阶段完成后运行 Runtime/Kanban/Phase 4G 相关测试：

```text
410 passed in 56.69s
```

全仓 `pytest -q` 另行启动，但在约 4% 时因机器既有的空 `/tmp/.git` 目录导致 7 个 LSP workspace
测试把临时目录误判为 Git workspace。该目录时间早于本阶段，未擅自删除；全仓 run 因而终止，不能
报告为 green。失败集中在 `tests/agent/lsp`，与本阶段修改文件和 410 项 Runtime 回归无关。
