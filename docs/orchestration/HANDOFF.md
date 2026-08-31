# Orchestra 方向接手说明

> 当前状态：三次负向 review 实验已结束，该机制未证明稳定净收益。最新方向是 episodic Orchestra：阶段内由完整 Worker 自治，阶段之间由跨 activation 无会话继承的独立战略 Agent 基于持久项目状态重新判断路线。当前仅形成设计，尚未实现 Runtime。

## 1. 当前要解决的问题

成熟 coding agent 可以持续完成一条条明确需求，但长期连续开发可能形成自我强化的结构增长：Agent 先增加辅助机制，再把由它产生的测试、schema、文档和迁移当成独立需求，导致后续工作只能继续维护和扩展这些机制。

Hermes Runtime Kernel 本身出现过这种情况。历史对照还显示，多节点 Runtime 相比连贯单 Worker 消耗了更多时间和 token，却没有得到更好的任务质量。因此不能再用 orchestration 活跃度证明价值。

## 2. 当前判断

以下结论可以保留：

- Worker 应是完整 Codex / Claude Code，不需要由外层拆成固定角色；
- 外层不应重复实现 Worker 内部 subagent 管理；
- 语义方向由模型判断，凭证、不可逆操作和真实数据边界由代码保护；
- 更多节点、review、checkpoint 和事件不是成功指标；
- 在建设 Orchestra 前，应先验证它相对普通独立 review 是否提供实际增量。

以下说法已经撤回：

- “否定是 Orchestra 唯一且已经成立的核心能力”；
- “一次成功删除即可验证设计”；
- “一次没有删除即可证伪设计”；
- “竞争式并行几乎免费”；
- “Worker 内部协调成本为零”；
- “命名实体数可以作为深审触发、实质证据和 cleanup 验收”；
- “历史设计记录天然能提供无偏的长期判断”。

## 3. 实验验证了什么

三次实验验证的问题是：

> 在相同的七轮连续需求中，相比普通独立 review，只允许 `keep / remove /
> merge / simplify / doubt` 的负向回顾，能否在能力不受损的情况下让后续维护更容易？

两组都在 R3、R5、R7 通过公开测试后获得相同次数的 reviewer 和 executor。唯一有意差异是 reviewer 可以提出什么动作。主要结果看最终能力和新的维护 agent 完成后续修改的真实成本，而不是 review 数量、LOC 或命名实体数。

共同 prompt 见 `deep-review-prompt.md`。

## 4. 第一对：scheduler

第一对协议见 `cheap-experiment.md`，已经完整运行。

主要结果：

- 两组 R1–R7 公开测试均通过；
- 最终有效隐藏行为维度均通过；
- 两组“增加执行 timeout”和“删除 retry”维护任务均正确完成，无旧能力回归；
- 两个维护任务合计，负向组 wall time 少约 38%，成本少约 56%；
- 负向 review 主开发轨迹没有更便宜，wall time 反而更长，收益出现在最终结构和后续维护；
- 负向轨迹多次出现“Worker 加入旧 schema 迁移，后续负向 review 删除，下一轮 Worker 又加入”的机制棘轮；
- 只有一对轨迹，且两组从 R1 起选择了不同的 JSON/SQLite 架构和测试策略，不能把差异全部归因于 reviewer。

该结果是明显但仍有混杂因素的正向信号，因此不建设 Orchestra，而是补跑第二对确认。

## 5. 第二对：JSON 决策规则解释器

完整规格和结果见 `rule-interpreter-experiment.md`。第二对已经完整运行。

结果：

- 两组 R1–R7 公开测试全部通过；
- 两组最终 10 项隐藏行为测试全部通过；
- 两组“三值缺失语义”和“删除 explain”维护任务均正确完成；
- 两个维护任务合计，负向组 wall time 少约 37%，成本少约 35%；
- 普通 review 和负向 review 各自发现并修复了一个真实边界问题。

方向与第一对一致，但原因仍不干净。普通组在 R1、第一次 review 前就生成了项目内测试，最终 907 行；负向组从 R1 起没有项目内测试。两组最终实现代码本身几乎同规模：566 行与 571 行。普通维护 agent 的额外成本主要包括同步修改或删除项目内测试，这一差异不能归因于后续 review。

第二对的负向 review 只执行了两个极小 cleanup、一个真实边界修复和一个 no-op，没有删除测试。因此当前证据支持“较小、没有大量 Agent 自建测试负担的成品更容易维护”，但还没有证明负向 review 是原因。

## 6. 模型、隔离和运行方式（归档）

三次实验沿用相同的隔离原则：

- Worker、reviewer、executor 和维护 agent 统一使用 GPT-5.6 Sol、`high` reasoning effort；
- Bubblewrap 严格隔离；
- 每个角色独立工作目录、HOME、Claude 配置和 session；
- 不读取宿主机全局 `AGENTS.md`、`CLAUDE.md`、memory、skills、历史会话、Orchestra 文档或另一组结果；
- `--bare`、`--safe-mode`、关闭 skills、plugins 和 MCP；
- 每个 agent turn 都是有限任务，不自动 retry、循环 review 或生成下一轮需求；
- 公开测试和最终测试在两组对应运行前写好；
- evaluator 结果不回灌给最终 Worker；
- 不搭实验平台或 Runtime 基础设施。

Worker 可以按实际需要使用子代理，但每个子代理必须承担不同且有用的工作。记录实际模型、token、wall time、成本、失败尝试和未采用工作。

## 7. 文档状态

- `orchestra-design.md`：三次实验后的主结论、已排除职责和仍待讨论的最小外层边界；
- `cheap-experiment.md`：第一对 scheduler 实验归档；
- `rule-interpreter-experiment.md`：第二对规则解释器实验归档；
- `controlled-stockroom-experiment.md`：第三次受控库存实验归档；
- `deep-review-prompt.md`：实验使用过的 review prompt 归档，不是 Runtime 流程；
- 本文件：当前接手摘要和最终判断。

旧 Runtime Kernel phase 文档是历史实现和实验记录，不应因为存在就自动成为新 Orchestra 的需求。

## 8. 第三次受控实验结论

第三次协议和结果见 `controlled-stockroom-experiment.md`。实验已完整运行。

共同 Worker 完成 R1–R3 后，两组从完全相同的单文件基线开始，无项目内测试差异。消除 treatment 前混杂后：

- 两组 R1–R7 和最终有效隐藏行为均通过；
- 负向组 treatment 主轨迹 wall time 高约 7%，成本高约 17%；
- 最终负向组代码少约 9%；
- 新增盘点批次维护任务两组都因相同 CLI 误解失败；
- 删除预留维护任务两组都成功，成本几乎相同。

因此停止“负向 review 是 Orchestra 核心能力”的实验方向，不建设自动负向 review 或 cleanup Runtime。

## 9. 最新 Orchestra 方向

Hermes 的真实长期失败仍未被上述小型 review 实验覆盖：Human 给出远大 Goal 后，同一个 Worker 连续自审下一阶段，长期围绕自身结构修补，逐渐忘记推进端到端目标，而 Human 被迫持续充当纠偏者。

最新主设计见 `orchestra-design.md`：

- Orchestra 是跨 activation 无会话继承、基于持久项目状态运行的 episodic 战略 Agent；
- 每个项目决策边界启动 fresh Orchestra session；
- 单次 activation 内可以完整观察、调查、使用 Agent、判断和调度；
- 阶段内交给完整 Worker 自治；
- 写回经核实的项目事实和当前调度结果，不保存历史推理叙事；
- 连续性属于项目状态，推理属于单次 episode。

当前只形成文档和待验证协议，不实现新的 Runtime。下一步若继续，应做真实长期对照：Worker 自管理阶段路线 vs episodic Orchestra 管理阶段路线，Human 只提供初始目标和预定的重大方向变化。
