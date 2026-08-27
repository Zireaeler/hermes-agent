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

完整规格见 `rule-interpreter-experiment.md`。

第二对改用完全确定的问题：

- 无持久状态；
- 无真实时间；
- 无并发和进程终止；
- policy、input 和 output 都是 JSON；
- 统一 CLI 外部黑盒测试；
- R1–R7 从原子条件发展到组合、多规则、类型操作、数组量词和解释树。

最终只测试已有行为变体，不增加隐藏功能。两个独立维护任务是：

1. 把缺失路径从二值 `false` 改为三值 `true/false/unknown`；
2. 彻底删除 R7 的 explain 能力及只为它存在的结构。

这两个任务方向相反：一个检验语义是否散落，一个检验能力能否直接删除，避免实验天然奖励“抽象更多”或“代码更短”。

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

## 8. 下一步

1. 写好第二对 R1–R7 公开测试和最终隐藏测试；
2. 验证新的实验目录仍满足严格隔离；
3. 初始化普通组和负向组空目录；
4. 人工驱动 R1–R7，在 R3、R5、R7 review；
5. 运行最终测试和两个独立维护任务；
6. 比较两对方向是否一致，再决定停止还是继续研究。

在第二对结果出现前，不新增数据库、状态机、decision schema、checkpoint、ledger、artifact 协议、节点通信层或其他 Runtime Kernel 机制。
