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

## 3. 现在验证什么

实验问题是：

> 在相同的七轮连续需求中，相比普通独立 review，只允许 `keep / remove /
> merge / simplify / doubt` 的负向回顾，能否在能力不受损的情况下让后续维护更容易？

具体协议见 `cheap-experiment.md`，prompt 见 `deep-review-prompt.md`。

两组：

- 完成完全相同的 R1–R7 scheduler 需求；
- 使用同一套最小 CLI，内部结构自由；
- 每轮先通过当前公开测试，再在 R3、R5、R7 后得到相同次数的独立 review 和 executor 修改机会；
- 使用相同模型、输入材料和近似预算；
- 唯一区别是 reviewer 可以提出的动作。

最终先运行相同的未公开行为变体，再让新的维护 agent 完成“增加任务执行超时”和“删除失败重试能力”两个任务。后续修改成功率、成本、失败次数和改动扩散，比实体数和 LOC 更能说明结构负担。

先跑一对。只有结果有信号但不足以判断时才补第二对；方向冲突时再考虑第三对。这个规模只用于判断是否值得继续，不支持一般性结论。

## 4. 模型、隔离和子代理

第一对实验的 Worker、reviewer、executor 和维护 agent 统一使用 GPT-5.6 Sol、`high` reasoning effort，并记录 provider 实际返回的模型 ID。

每个角色都运行在独立工作目录、独立会话和干净上下文中，不读取宿主机全局 `AGENTS.md`、`CLAUDE.md`、memory、skills、历史 session、Orchestra 文档或另一组结果。只传入该角色当前需要的需求、代码、测试和 prompt。

可以按实际需要使用任意数量子代理，但每个都必须有不同且有用的任务。不要求两组机械地使用相同 agent 数量；记录实际 token、wall time 和未采用工作。

每个 turn 都是有限任务，不自动重试、循环 review 或生成下一轮需求。超时或持续没有实质进展时直接终止并记录，不为实验增加恢复系统。

## 5. 文档状态

- `orchestra-design.md`：方向说明，已降级为待验证假设，不是 Runtime 规范；
- `cheap-experiment.md`：直接的两组低成本实验；
- `deep-review-prompt.md`：普通 review、负向回顾和 executor prompt；
- 本文件：当前结论和下一步。

旧 Runtime Kernel phase 文档是历史实现和实验记录，不应因为存在就自动成为新 Orchestra 的需求。

## 6. 下一步

1. 检查四份 Orchestra 文档是否一致；
2. 在独立临时目录准备最小 CLI 的公开测试和最终测试，不搭实验 infra；
3. 人工驱动第一对实验；
4. 根据能力保持、维护任务结果和实际成本决定停止还是补跑一对。

在结果出现前，不新增数据库、状态机、decision schema、checkpoint、ledger、artifact 协议、节点通信层或其他 Runtime Kernel 机制。
