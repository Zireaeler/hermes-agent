# Orchestra 设计：Episodic 战略层

## 0. 顶层定义

> **Orchestra 是跨 activation 无会话继承、但基于持久项目状态运行的 episodic 战略 Agent。它在项目决策边界被唤醒，每次独立重建最小充分上下文，在单次 activation 内完成观察、调查、判断和调度，并将经核实的项目事实与当前调度结果写回项目状态，而不延续自身的历史推理叙事。持久项目状态是可直接重写的当前事实集合，不是 Orchestra 会话日志、决策账本或需要兼容迁移的协议。**

架构原则：

> **连续性属于项目状态，推理属于单次 episode。**

Orchestra 不替 Worker 编码，也不把 Worker 拆成 planner、coder、tester。它负责跨阶段保持长期目标、选择下一项阶段成果、判断路线是否仍有效，并防止项目长期陷入执行 Worker 自己形成的局部视野循环。

当前文档是一版待验证设计，不是已批准实现的 Runtime 规范。第一版先用手工协议和独立 Agent session 验证，不新增 Orchestra 基础设施。

## 1. 为什么完整 Worker 仍然不够

完整 Claude Code/Codex Worker 能很好地完成一个明确、有限、可验证的工程任务。它可以自行：

- 阅读项目；
- 制定阶段内方案；
- 使用子代理；
- 编码和测试；
- 修复当前失败；
- 做必要的局部重构。

但这不意味着同一个 Worker 能长期管理自己形成的项目路线。

Hermes 过去的实际循环是：

```text
Human 给出远大 Goal
→ Worker 从当前代码选择一个阶段
→ Worker 实现并测试
→ Worker 自审下一阶段
→ 继续修补刚建立的结构
```

长期结果表明，这个循环容易形成自指视野：

```text
上一阶段创建的机制
→ 成为下一阶段最显眼的问题
→ Worker 继续补一致性、恢复和兼容
→ 新机制产生更多局部责任
→ 原始产品目标逐渐退出注意力中心
```

每个阶段可能都正确完成，但整个项目没有取得新的端到端成果。Human 若不持续阅读实现、提醒原目标和纠偏，Worker 很难独立跳出自己建立的概念世界。

Human 不应该长期充当项目经理。Human 应只负责初始长期目标、后续阶段性的重大方向和真正无法由事实裁决的产品选择。

Orchestra 的必要性来自这个长期视野缺口，而不是 Worker 不会实现代码。

## 2. 三次 review 实验说明了什么

三次实验验证的是一个窄问题：受限负向 review 是否比普通独立 review 稳定降低后续维护成本。

最终受控实验没有证明负向 review 有稳定净收益。因此：

- 负向 cleanup 不是 Orchestra 的核心能力；
- 不建立固定 review 节点；
- 不建立自动 cleanup 流水线；
- 不用 LOC、实体数或复杂度 gate 管理路线。

但这些实验没有验证长期 program-level Orchestra。它们没有模拟：

- 持续数天或数周的真实项目；
- Human 不持续纠偏；
- Worker 自己连续决定十几个阶段；
- 中途注入重大需求变化；
- 旧路线应被整体放弃；
- 项目是否持续产生用户可观察成果。

因此 review 实验只能排除“负向 review 是核心能力”，不能排除独立的跨阶段路线管理。

实验归档：

- `cheap-experiment.md`
- `rule-interpreter-experiment.md`
- `controlled-stockroom-experiment.md`
- `deep-review-prompt.md`

## 3. 角色边界

### 3.1 Human

Human 提供：

- 长期目标；
- 关键成功体验和可观察结果；
- 产品、安全和外部协议边界；
- 后续阶段性的重大方向变化；
- 事实无法裁决的价值选择。

Human 不负责：

- 每阶段决定下一步；
- 持续阅读 Worker 总结；
- 判断 Worker 是否陷入局部循环；
- 逐轮提醒原目标；
- 清理 Agent 自建机制；
- 为 Worker 编写详细实现任务。

### 3.2 Orchestra

Orchestra 是长期项目导演，负责阶段之间的战略 episode：

- 从 Human 当前目标重新落地项目意图；
- 独立观察当前可运行产品和仓库事实；
- 判断项目是否真正推进总目标；
- 识别局部自洽但整体停滞的路线；
- 选择下一项有外部意义的阶段成果；
- 决定继续、重定向、丢弃、停止或询问 Human；
- 把经核实的项目事实写回持久项目状态。

Orchestra 不负责阶段内实现。

### 3.3 Worker

Worker 接收一个有意义、有限、可验证的阶段 Brief，并在阶段内完整自治：

- 理解当前阶段；
- 阅读代码；
- 决定实现；
- 自行拆任务；
- 调用内部子代理；
- 编码、测试和修复；
- 做阶段内必要重构；
- 交付代码、运行结果和新事实。

Orchestra 不微操 Worker，不规定文件、类、子代理数量或实现步骤。

### 3.4 Harness

Harness 只处理机械边界：

- session 和进程生命周期；
- Human 消息和外部事件投递；
- sandbox、权限和 credentials；
- 硬预算、超时和取消；
- 后台任务完成通知。

Harness 不做项目语义判断。

## 4. Episodic 运行形态

### 4.1 跨 activation 不继承会话

每次 Orchestra 在阶段边界被唤醒时：

- 创建新的 Agent session；
- 不 resume 上一轮 Orchestra conversation；
- 不携带旧 assistant message、工具 transcript 或推理链；
- 不为了复用旧 prompt cache 而塞回完整历史；
- 从持久项目状态和当前事实重新组装请求体。

这样避免 Orchestra 自己维护上一轮路线、辩护和叙事一致性。

### 4.2 单次 activation 内是完整 agentic loop

Fresh activation 不等于单次模型分类。

一个 episode 内可以：

1. 阅读 Human Intent；
2. 检查项目和 Git；
3. 运行当前产品；
4. 查看端到端结果；
5. 调用独立探索 Agent；
6. 比较候选阶段；
7. 读取精简阶段事实；
8. 让 critic 尝试推翻路线；
9. 形成阶段 Brief 或其他战略决定。

本次 activation 内正常保留工具历史和上下文。输出战略决定并写回状态后，session 结束。

### 4.3 Worker session 与 Orchestra session 不同

```text
Worker：阶段内连续 session
Orchestra：阶段之间 fresh session
```

Worker 需要保留当前实现、测试失败和子代理状态；Orchestra 需要在阶段边界摆脱旧路线的认知锚定。

## 5. 持久项目状态

持久项目状态提供长期连续性，但不保存推理叙事。

### 5.1 Human Intent

优先保留 Human 原话或忠实转述：

- 长期目标；
- 当前重大方向；
- 可观察成功结果；
- 真实约束；
- 后续明确修改和撤回。

Worker 或 Orchestra 的实现不能反向扩张 Human Intent。

### 5.2 已核实产品事实

例如：

- 当前真实可运行的端到端能力；
- 已确认的用户流程；
- 部署、CI 或用户反馈事实；
- provider/harness 已经提供的能力；
- 当前真实阻塞。

代码、Git 和测试可直接恢复的细节不重复抄写。

### 5.3 精简阶段结果

每个阶段只保存几项：

- 阶段想证明或交付什么；
- 实际产生了什么外部结果；
- 哪个关键假设成立或失败；
- 当前路线继续、停止或被否定。

不保存完整阶段推理、候选争论和设计辩护。

### 5.4 已停止路线

只记录有实际依据的停止结果：

```text
路线 / 原假设 / 实际证据 / 停止结论
```

防止新的 Orchestra 重复已经失败的实验。

### 5.5 当前产品缺口

从 Human Goal 看尚未具备的外部能力，不是代码 TODO 列表。

### 5.6 Pending Human Decisions

只保存真正等待 Human 的产品方向、外部承诺或不可逆选择。

### 5.7 当前阶段调度结果

只保存当前阶段 Brief、状态和验收条件。阶段结束后可以直接替换，不升级成永久 contract。

## 6. 不持久化什么

禁止把以下材料作为 Orchestra 长期状态：

- 完整 Orchestra transcript；
- chain of thought；
- 旧工具调用历史；
- 所有候选路线的长篇比较；
- 上一轮 Orchestra 的设计辩护；
- Worker 的完整会话；
- 每轮 review 输出；
- “上一轮建议的下一阶段”；
- 已投入 token 作为继续路线的理由；
- phase graph、concept surface 或 decision ledger。

原则：

> 持久化经核实的结果和事实，不持久化认知轨迹。

## 7. 最小充分上下文组装

每次 activation 的 request body 至少包含：

1. Orchestra 稳定职责和禁止项；
2. Human 当前 Intent；
3. 当前产品和仓库事实；
4. 最近 Worker 的实际产物；
5. 当前外部变化；
6. 精简阶段结果和已停止路线；
7. 本次需要作出的战略决定。

不直接拼接上一轮 conversation。

### 7.1 两段式上下文揭示

为了减少锚定，单次 episode 内先盲评、后读历史。

#### 第一段：独立观察

先提供：

- Human 当前目标；
- 当前产品；
- 当前代码；
- 当前端到端事实；
- 最近 Worker 产物。

暂不提供：

- 上一轮为什么选择当前路线；
- Worker 对下一步的建议；
- 已投入多少时间；
- 详细阶段历史。

Orchestra 先回答：

```text
当前真实完成了什么？
距离 Human Goal 最大缺口是什么？
当前路线是否产生外部成果？
如果今天首次接手，下一阶段会做什么？
```

#### 第二段：历史校正

再提供精简阶段结果和已停止路线，检查：

- 是否重复已失败路线；
- 最近是否连续阶段没有外部增量；
- 历史事实是否推翻初步建议；
- 当前路线是否来自 Agent 自建结构。

最后才输出阶段决定。

## 8. 项目决策边界与触发条件

Orchestra 不常驻，也不检查每个 Worker action。典型触发：

- 当前阶段 Worker 完成；
- 当前阶段确认阻塞或失效；
- Human 发来重大方向变化；
- 关键实验或外部事件产生结果；
- 连续阶段没有外部成果；
- Worker 建议继续加固同一内部机制；
- 即将投入大规模架构路线；
- 达到预定战略重判点；
- Worker 提出真正产品级选择；
- 项目可能已经达到长期目标。

普通测试失败、局部实现选择和子代理调度不触发 Orchestra。

## 9. 单次 Orchestra episode

### Step 1：Ground Goal

回答：

- Human 最终要什么外部结果？
- 当前重大方向是什么？
- 哪些边界来自 Human 或外部世界？
- 哪些只是 Agent 自己创建的？

### Step 2：Observe Product

独立检查：

- 当前真正能运行什么；
- 哪些用户流程已成立；
- 哪些端到端结果已验证；
- 最近阶段是否产生外部能力；
- 最大产品缺口是什么。

### Step 3：Generate Few Candidates

只产生少量候选阶段，不生成完整 roadmap。

每个候选必须说明：

- 外部成果；
- 为什么现在做；
- 对应哪个长期目标缺口；
- 完成后获得什么新能力或信息；
- 不做会阻塞什么已确认事项。

### Step 4：Reject Self-Generated Work

对内部机制执行反事实检查：

> 如果此前 Worker 没有创建这个结构，Human Goal 现在还会要求做这件事吗？

如果不会，不把它自动升级为阶段目标。

### Step 5：Select One Milestone

只选择当前一个阶段成果。最多保留少量候选方向，不冻结长期 roadmap。

### Step 6：Issue Worker Brief

把阶段成果交给完整 Worker。

### Step 7：Evaluate Outcome

阶段结束后独立检查验收结果和总目标推进，而不是只接受 Worker 自述。

### Step 8：Write Back Facts

只写回：

- 已验证成果；
- 新事实；
- 成立或失败的假设；
- 停止路线；
- 当前调度决定；
- Pending Human Decision。

然后结束本次 Orchestra session。

## 10. 阶段 Brief

阶段 Brief 应短而完整：

```markdown
## 阶段目标
本阶段要取得的外部成果

## 为什么现在做
它推进长期目标的原因

## 当前事实
Worker 需要知道的已核实项目事实

## 验收结果
可运行、可观察、可验证的完成标准

## 边界
本阶段明确不做什么；哪些决定需要 Human
```

不规定：

- 文件和类设计；
- Worker 内部步骤；
- 子代理数量；
- 测试实现方式；
- 局部 review 流程。

## 11. 战略决定

单次 episode 最终只需表达以下语义之一：

- `ADVANCE`：阶段有效，选择下一项成果；
- `CONTINUE`：阶段成果尚未完成，继续同一目标；
- `REDIRECT`：当前路线失效，改方向；
- `DISCARD`：本阶段产物无价值，删除或回退；
- `WAIT`：真实外部依赖未满足；
- `ASK_HUMAN`：出现事实无法裁决的重大选择；
- `STOP`：长期目标完成或继续投入无价值。

第一版不把这些动作固化为 versioned schema 或状态机。它们只是战略输出语义。

## 12. 防止视野循环

### 12.1 下一阶段必须有外部依据

至少对应一项：

- Human 当前目标；
- Human 重大方向；
- 用户可观察能力缺口；
- 真实端到端失败；
- 外部协议或发布边界；
- 取得上述成果不可避免的当前依赖。

以下不能单独成为新阶段理由：

- 代码已有这个结构；
- 已有测试覆盖它；
- 上一阶段留下 TODO；
- 继续完善会更稳妥；
- 已投入很多 token。

### 12.2 监测外部增量

如果连续阶段：

- 修改同一内部子系统；
- 增加测试、schema、恢复和兼容路径；
- 没有新用户流程；
- 没有验证新关键假设；
- 没有减少真实失败；

下一次 Orchestra 必须重新 framing，不沿用 Worker 的下一步建议。

### 12.3 Worker 总结只作为线索

Worker self-report 必须与代码、测试和运行结果区分。Orchestra 先独立观察，再读取 Worker 对下一阶段的意见。

### 12.4 必要时临时调用独立观察者

只在以下场景调用新鲜 critic：

- 连续阶段无外部成果；
- Worker 建议继续加固同一机制；
- 即将大规模投入；
- Human 重大方向变化；
- Orchestra 不确定是否被旧路线锚定。

Critic 回答路线问题，不做一般 code review。

## 13. Human 介入边界

只在以下情况询问 Human：

- 多个产品方向都合理且事实无法裁决；
- 要建立新的外部兼容或产品承诺；
- 涉及不可逆发布或数据风险；
- 长期目标彼此冲突；
- 价值和成本取舍本身属于 Human 偏好。

不因内部实现、普通 bug、测试失败、文件结构或 Agent 自建 contract 询问 Human。

## 14. 多 Agent 使用方式

Orchestra 可以在单次 episode 临时使用少量 Agent：

- 从产品目标看最大缺口；
- 独立运行当前产品检查事实；
- 寻找当前路线以外的替代方向；
- critic 尝试推翻候选阶段。

它不：

- 固定建立十几个角色；
- 分发文件级 coding 任务；
- 管理 Worker 内部子代理；
- 建设投票、共识或长期节点团队。

实现型子代理仍由 Worker 管理。

## 15. 第一版形态

第一版不实现新的 Runtime Kernel。

可由当前主 Claude Code 会话手工扮演 orchestration harness：

1. 临时组装 Orchestra request body；
2. 启动 fresh Orchestra Agent；
3. Orchestra 完成单次 episode 并产出阶段 Brief；
4. 启动完整 Worker；
5. Worker 完成阶段；
6. 收集当前事实；
7. 下一阶段重新启动 fresh Orchestra Agent。

只需要普通项目文件或本次会话材料保存最小事实。不新增数据库、事件协议、checkpoint 或 decision ledger。

## 16. 验证方案

真正需要验证的假设是：

> 在长期、复杂、需求会变化且 Human 不持续纠偏的真实项目中，episodic Orchestra 是否比 Worker 自管理更稳定地推进原始目标、减少局部视野循环并交付更多端到端成果？

### 对照 A：Worker 自管理

```text
Human 长期目标
→ Worker 完成阶段
→ Worker 自审并决定下一阶段
→ 连续推进
```

这接近 Hermes 过去的实际方式。

### 对照 B：Episodic Orchestra

```text
Human 长期目标
→ fresh Orchestra 选择阶段成果
→ 完整 Worker 执行
→ fresh Orchestra 独立重判路线
```

两组 Human 都不持续监管，只在预定点注入相同重大需求变化。

### 主要观察

- 最终端到端用户成果；
- 有多少阶段直接推进长期目标；
- 有多少阶段修补 Agent 自建机制；
- 重大需求变化后多久放弃旧路线；
- Human 被迫纠偏次数；
- 最终产品是否可用；
- 总成本和 wall time。

不以 LOC、阶段数、review 数或内部测试数量作为主要成功指标。

## 17. 明确非目标

第一版不建设：

- 固定 phase 类型或 phase binding；
- roadmap 冻结；
- decision schema、session 或 ledger；
- checkpoint、snapshot 或 compaction；
- route version 和 reconcile；
- artifact 协议和 lineage；
- concept/dependency surface；
- 自动负向 review 或 cleanup；
- 固定 reviewer；
- Worker registry；
- 外层 subagent scheduler；
- 节点通信层；
- 持久推理历史。

只有长期对照实验出现经确认的实际缺口时，才考虑对应的最小自动化。
