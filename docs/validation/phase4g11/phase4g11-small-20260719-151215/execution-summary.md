# Phase 4G11 Small 真实验证执行总结

## 1. 最终结论

最终 run `phase4g11-small-20260719-151215` 完成了真实模型源、真实 Codex worker 和真实
Decision Provider 的 closed-loop coordination：

| 维度 | 结果 |
| --- | --- |
| Runtime correctness | 通过，consistency `passed`，0 violation，0 warning |
| Closed-loop orchestration | 通过，2 checkpoint、2 directive、2 ACK |
| Worker continuity | 通过，parser/renderer 各 resume 原 session 1 次 |
| Durable contribution | 通过，2 份隔离 patch 均冻结、校验并被 primary 接受 |
| Task capability | 通过，goal `satisfied`，最终 unittest 1/1 通过 |
| Evaluator | 未创建，本任务不要求独立 oracle |

成功 run 使用 `gpt-5.6-sol`。模型源的 WebSocket 在验证窗口内长期断流，因此最终 run 仅在
隔离 Codex home 中关闭 `supports_websockets`，base URL、API key、model 和其他 worker
配置保持不变。凭据和 base URL 未进入归档。

## 2. 任务与拓扑

Small repository 包含 parser、renderer 和一个端到端 pipeline test。目标是把 parser 输出从
字符串升级为 `{'kind': 'word', 'text': value}`，并让 renderer 消费该共享 contract。

Runtime 使用三个 durable node：

```text
parser-contract (isolated worktree) -----\
                                        +--> pipeline-integration (shared workspace)
renderer-contract (isolated worktree) --/
```

这不是为了增加 agent 数量。两个 child 的 write scope 不重叠，但 renderer 在不知道 parser
最终 contract 时不能可靠实现，因此测试要求先到达 safe point，再由全局状态驱动后续执行。

## 3. 最终执行过程

### 3.1 First slice

Parser 与 renderer 并行启动两个真实 Codex thread：

- parser 修改并验证 `src/token_parser.py`，提交 `shared_contract_changed` checkpoint；
- renderer 只检查 `src/token_renderer.py`，不猜测 token shape，提交
  `blocking_dependency` checkpoint；
- 两个 checkpoint 经过 canonical ingest 后，node 进入 `waiting_coordination`；
- checkpoint 没有写入 goal completion 事实。

### 3.2 Global decision

Decision Provider 同时读取两个 active responsibility 的 global snapshot。第一份 patch 额外
向 `waiting_dependency` integration node 发 directive，被本地 validator 拒绝，graph 未变化。
第二份 patch 只包含两个合法 `issue_directive`，应用后 graph revision 从 1 变为 2。

这个过程证明 validator feedback 可以修正 Provider 输出，同时错误 proposal 不会成为事实。

### 3.3 Same-session resume 与 ACK

Runtime 将 directive 写入 DB mailbox，再 materialize 两个 resume task：

- parser session `019f7937-fb41-7362-b89a-85b8cb075136`，`resume_count=1`；
- renderer session `019f7937-fb43-7bf0-8f80-2b1abda6f24e`，`resume_count=1`；
- 两个 worker 的 terminal receipt 均包含各自 `consumed_directive_ids`；
- 两条 directive 最终均为 `acknowledged`，没有 queued/delivered 残留。

### 3.4 Contribution 与 integration

Child terminal receipt 通过后，Runtime 冻结两份 non-authoritative contribution：

| Node | Artifact | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| renderer-contract | `art_0e43516ec94b` | 371 | `908a151ed474eb6de86d4d8d6f520df7b235b39d618bcd2a7ac6daea81c98009` |
| parser-contract | `art_de086f0c8fe8` | 541 | `d4a7848d18929bcb2766285193f1c1557e751f257950c1b444742a6102cd939e` |

Primary integration owner 校验 patch hash、应用两份 patch、将两个 artifact 都归类为 accepted，
运行 `python3 -m unittest discover -s tests`，结果 1 test passed。Runtime 随后写入
`contribution_attribution_verified`，三个 node 均 succeeded，job 进入 done。

## 4. 可观测数据

| 指标 | 值 |
| --- | ---: |
| Durable node | 3 |
| Materialization | 5 |
| Decision round | 2（1 rejected，1 applied） |
| Coordination checkpoint | 2 |
| Directive ACK | 2 |
| Frozen contribution | 2 |
| Worker session | 3 |
| Child same-session resume | 2 |
| Progress ledger fact | 3 |
| 总 wall time | 约 6 分 08 秒 |
| Codex input tokens | 458,209 |
| Codex cached input tokens | 277,248 |
| Cache ratio | 60.5% |
| Codex output tokens | 15,811 |

Token 是 Codex CLI 对五个 turn 的 usage 汇总，不含 Decision Provider token；它用于观察相对成本，
不等同于账单 token。

## 5. 迭代验证中发现的问题

| Attempt | 发现 | 修复或结论 |
| --- | --- | --- |
| `141345` | runner 调用不存在的 `AdvanceResult.to_dict()`；pycache 触发 write-scope violation | 使用 `dataclasses.asdict`；fixture 增加标准 `.gitignore` |
| `141707` | Provider 将 directive `instructions` 输出为字符串 | Profile 明确要求 non-empty string array；validator 保持 strict |
| `142209` | Child 把自然语言成果写入 `accepted_contributions`；runner 不继续 ready recovery | Child prompt 明确 attribution 仅限 artifact ID；runner 增加 bounded recovery loop |
| `143012` | Provider 混入 structure/dependency op；child goal outcome arrays 重叠 | 收紧 coordination-only patch；明确 goal outcome arrays 互斥 |
| `144837` | WebSocket integration 长时间停在 reconnect，人工重发后 recovery 仍超时 | 保留 transport 失败事实；最终验证切换同源 HTTP transport |
| `151215` | 首个 control patch 多发给 waiting dependency node | Validator 拒绝，第二轮修正；Profile 进一步限定 target set 精确相等 |

这些 attempt 没有通过脚本直接改 graph、ledger 或 directive 状态。每次成功状态变化均来自正常
task/run/receipt、Decision Provider patch 和本地 reducer/validator 路径。

## 6. 能力判断

本验证证明的不是“小任务能被三个 Codex 做完”，而是以下跨节点因果链真实成立：

```text
parser 的阶段性语义变化
    -> Runtime global snapshot
    -> Decision Provider 观察全部 active responsibilities
    -> DB-backed directive 改变 renderer 的后续上下文
    -> renderer 在原 session 中继续并 ACK
    -> 两个隔离结果由 primary 负责集成和最终验证
```

这使 Runtime Kernel 不再只是单 worker 外层状态机。它能在 worker terminal completion 之前利用
阶段性语义事实改变另一个 durable responsibility，同时保留权限、workspace、恢复、审计和
最终责任边界。

## 7. 未证明与剩余风险

- 本 run 是受控 Small，不证明 Large/Hard 任务上的收益；本阶段按约束未运行 Large/Hard。
- WebSocket transport 在当前模型源上不稳定；HTTP fallback 成功不代表 WebSocket 服务端问题已修复。
- 首个 Provider patch 仍需一次 validator feedback，说明 Profile 约束需要继续用真实样本评估。
- 当前是 cooperative safe point，不支持向正在生成中的模型 turn 异步注入。
- 没有 external oracle 的普通开发任务仍不默认创建 evaluator。

## 8. 证据索引

- `run-report.json`：Runtime 最终结构化报告；
- `parser-contract.patch.gz`：冻结的 parser contribution 原始字节；
- `renderer-contract.patch.gz`：冻结的 renderer contribution 原始字节；
- Runtime job：`rjob_77f46b628f72`；
- Base revision：`45c8e392c240859f6fc8db1af22d400dbfd92008`。
