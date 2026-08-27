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
- 在 R2、R4、R6 后得到相同次数的独立 review 和 executor 修改机会；
- 使用相同模型、输入材料和近似预算；
- 唯一区别是 reviewer 可以提出的动作。

最终先检查能力，再让新的维护 agent 完成“增加任务执行超时”和“删除失败重试能力”两个任务。后续修改成功率、成本、失败次数和改动扩散，比实体数和 LOC 更能说明结构负担。

先跑两对；结果冲突时再补一对。这个规模只用于判断是否值得继续，不支持一般性结论。

## 4. 模型和子代理

- 普通实现、review 和分析默认使用 `terra-high` 及以下；
- Luna 用于简单查找和机械核验；
- Sol 只在多个 Terra 对关键结论冲突且测试无法裁决时使用；
- 可以按实际需要使用任意数量子代理，但每个都必须有不同且有用的任务；
- 记录实际 token、wall time、未采用工作和 provider 返回的模型 ID；
- 不要求两组机械地使用相同 agent 数量，只保证相同外部机会和相近资源。

当前 Agent 工具的模型别名曾全部实际路由到 `gpt-5.6-sol`。运行实验前应先用一次最小调用确认 Terra/Luna 是否真的生效；如果没有，两组统一使用同一个实际模型。

## 5. 文档状态

- `orchestra-design.md`：方向说明，已降级为待验证假设，不是 Runtime 规范；
- `cheap-experiment.md`：直接的两组低成本实验；
- `deep-review-prompt.md`：普通 review、负向回顾和 executor prompt；
- 本文件：当前结论和下一步。

旧 Runtime Kernel phase 文档是历史实现和实验记录，不应因为存在就自动成为新 Orchestra 的需求。

## 6. 下一步

1. 检查四份 Orchestra 文档是否一致；
2. 只做运行实验必需的测试和最薄执行准备；
3. 跑两对实验；
4. 根据能力保持、维护任务结果和实际成本决定停止还是继续。

在结果出现前，不新增数据库、状态机、decision schema、checkpoint、ledger、artifact 协议、节点通信层或其他 Runtime Kernel 机制。
