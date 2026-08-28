# Orchestra 方向接手说明

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

## 3. 正在验证什么

实验问题是：

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

## 6. 模型、隔离和运行方式

第二对沿用第一对已经验证的条件：

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

- `orchestra-design.md`：方向说明，已降级为待验证假设，不是 Runtime 规范；
- `cheap-experiment.md`：第一对 scheduler 协议和简要结果；
- `rule-interpreter-experiment.md`：第二对完整协议；
- `deep-review-prompt.md`：普通 review、负向回顾和 executor prompt；
- 本文件：当前判断和下一步。

旧 Runtime Kernel phase 文档是历史实现和实验记录，不应因为存在就自动成为新 Orchestra 的需求。

## 8. 第三次受控实验与最终判断

第三次协议和结果见 `controlled-stockroom-experiment.md`。实验已完整运行。

共同 Worker 完成 R1–R3 后，两组从完全相同的单文件基线开始，无项目内测试差异。消除 treatment 前混杂后：

- 两组 R1–R7 和最终有效隐藏行为均通过；
- 负向组 treatment 主轨迹 wall time 高约 7%，成本高约 17%；
- 最终负向组代码少约 9%；
- 新增盘点批次维护任务两组都因相同 CLI 误解失败；
- 删除预留维护任务两组都成功，成本几乎相同。

因此前两对显著维护优势不能归因于负向 review；它们主要与 treatment 前项目内测试策略分叉重合。受控实验没有证明负向 review 相对普通独立 review 有稳定净增量。

最终决定：停止实验，不建设 Orchestra Runtime、自动负向 review 系统或新的 orchestration 基础设施。保留已经写入全局 Agent 规则的直接反劫持原则即可：不让 Agent 自建 contract、schema、测试、迁移和流程反过来成为新需求。
