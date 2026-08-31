# Orchestra 设计：Episodic 项目战略层

## 0. 顶层定义

> **Orchestra 是运行在项目决策边界上的 episodic 战略 Agent。每次 activation 都从当前 Human Intent、项目模型、本轮事实变化和按需证据重新组装最小充分上下文；单次 activation 内允许完整 agentic loop，但跨 activation 不继承对话历史、推理过程和旧策略叙事。长期存在的是 Orchestra 的项目责任与持久项目模型，不是一条长期模型 session。**

架构原则：

> **连续性属于项目状态，推理属于单次 episode；局部执行连续性属于 Workstream Worker。**

整体结构：

```text
Human
  │  长期意图、重大方向、价值取舍
  ▼
Fresh Orchestra activation
  │  项目级观察、路线判断、Workstream 拓扑调整
  ▼
Elastic Worker Pool：1..N
  ├─ Codex / Claude Code Worker A
  │    └─ Worker 自己的 SubAgent 系统
  ├─ Codex / Claude Code Worker B
  │    └─ Worker 自己的 SubAgent 系统
  └─ ...
```

默认只有一个完整 Worker。只有项目自然形成两个或更多低协调耦合、可独立维持局部决策循环的推进面时，Orchestra 才扩展为多个 Worker。

本文档是一版待验证设计，不是已批准实现的 Runtime 规范。第一版优先用 Hermes 已有能力、普通项目文件和独立 Agent session 做手工实验，不先建设新的数据库、状态机或协议层。

### 0.1 术语

`activation` / `episode`：一次 fresh Orchestra session，从读取当前项目状态开始，到写回新的项目判断和调度结果结束。

`项目决策边界`：出现足以改变项目方向、Workstream 拓扑、重大假设或停止判断的新事实。它不是固定 phase，也不是每次 Worker turn。

`Workstream`：一个成熟 Worker 可以连续占有并完整推进的项目工作面。Worker 在其中自行理解代码、规划、调用 SubAgent、实现、测试和修复。

`项目模型`：Orchestra 为了判断“项目现在应推进什么”而持有的当前状态。它不是完整仓库摘要，不是历史对话，也不是冻结合同。

## 1. 为什么完整 Worker 仍然不够

完整 Claude Code/Codex Worker 能很好地完成一个明确、有限、可验证的工程任务。它可以自行：

- 阅读项目；
- 制定局部方案；
- 使用内部 SubAgent；
- 编码和测试；
- 修复当前失败；
- 做必要的局部重构；
- 在一个明确目标内完成并行拆分和结果整合。

因此 Orchestra 不应再次把同一个明确目标拆成 planner、coder、tester、reviewer，或把文件级任务分给多个外层 Worker。

但一个 Worker 能完成明确任务，不等于它适合长期管理自己形成的项目路线。Hermes 过去的实际循环是：

```text
Human 给出远大 Goal
→ Worker 从当前代码选择下一步
→ Worker 实现并测试
→ Worker 从刚建立的结构中选择下一步
→ 继续修补、加固和兼容该结构
```

这容易形成自指视野：

```text
上一轮创建的机制
→ 成为下一轮最显眼的问题
→ Worker 继续补一致性、恢复和兼容
→ 新机制产生更多局部责任
→ 原始产品目标逐渐退出注意力中心
```

每个局部任务都可能正确完成，但整个项目没有产生新的端到端能力、没有消除关键不确定性，也没有更接近可交付状态。

Orchestra 的必要性来自这个项目级视野缺口，而不是 Worker 不会实现代码。

## 2. 旧 Runtime Kernel 与 review 实验分别说明了什么

旧 Runtime Kernel 做对了一件重要的事：项目级连续性不能寄生在任一 Worker session 中，项目事实、全局判断和 Worker 局部执行上下文应当分离。

它的问题是把项目推进过度形式化为 goal contract、progress ledger、goal gap、execution graph、patch 和 validator。系统随后逐渐从“推进项目”转向“维护自身运行时机制的一致性”。因此本设计保留项目级独立上下文，不继承旧 Kernel 的合同、账本、图补丁和恢复协议。

三次 review 实验验证的是一个更窄的问题：额外负向 review 是否能稳定降低一个明确任务的后续维护成本。最终受控实验没有证明稳定净收益。因此：

- 负向 cleanup 不是 Orchestra 的核心能力；
- 不建立固定 review 节点；
- 不建立 planner/coder/tester/reviewer 流水线；
- 不用 LOC、实体数或复杂度 gate 管理路线；
- 不介入成熟 Worker 的阶段内执行循环。

但这些实验没有覆盖长期路线选择、多个低耦合推进面、承重假设失效、需求变化和 Worker 拓扑调整。因此它们不能否定项目级 Orchestra。

实验归档：

- `cheap-experiment.md`
- `rule-interpreter-experiment.md`
- `controlled-stockroom-experiment.md`
- `deep-review-prompt.md`

## 3. 角色边界

### 3.1 Human

Human 提供：

- 长期意图；
- 关键成功体验和可观察结果；
- 产品、安全和外部协议边界；
- 后续重大方向变化；
- 事实无法裁决的价值选择。

Human 不负责：

- 持续决定每个局部下一步；
- 阅读每个 Worker turn；
- 判断 Worker 是否陷入局部视野循环；
- 为 Worker 编写详细实现计划；
- 在多个成熟 Worker 之间做日常协调。

### 3.2 Orchestra

Orchestra 负责项目级战略 episode：

- 从 Human 当前 Intent 重新落地项目目的；
- 基于证据更新当前项目模型；
- 判断项目为什么仍未收敛；
- 选择、继续、重定向、暂停或终止 Workstream；
- 判断是否存在真正可独立推进的多个工作面；
- 在协调耦合升高时收缩并行、合并或串行化 Workstream；
- 识别局部自洽但整体停滞的路线；
- 决定何时请求 Human、等待或停止；
- 写回精简的项目状态变化和当前调度结果。

Orchestra 不负责 Workstream 内部实现，也不管理 Worker 内部 SubAgent。

### 3.3 Worker

Worker 接收一个有意义、相对完整、可验证的 Workstream Brief，并在该工作面内完整自治：

- 理解目标和代码；
- 决定实现策略；
- 自行拆任务；
- 调用内部 SubAgent；
- 编码、测试和修复；
- 做工作面内必要重构；
- 返回可验证产物和会影响项目级判断的新事实。

Worker 不需要把普通局部计划、命令历史、每次失败和内部 SubAgent transcript 上报给 Orchestra。

### 3.4 Harness

Harness 只处理机械边界：

- Agent session、进程和 workspace 生命周期；
- Human 消息和外部事件投递；
- Worker run 与 Workstream 的映射；
- sandbox、权限、credentials 和不可逆操作边界；
- 硬预算、超时、取消和进程恢复；
- Git、CI、测试、运行结果和 Worker 产物的引用；
- 在项目决策边界唤醒 fresh Orchestra activation。

Harness 不判断项目应做什么，不按固定规则拆任务，不用 validator 替代战略判断。

## 4. Episodic 运行形态

### 4.1 跨 activation 不继承会话

每次 Orchestra 在项目决策边界被唤醒时：

- 创建新的 Agent session；
- 不 resume 上一轮 Orchestra conversation；
- 不携带旧 assistant message、工具 transcript 或推理链；
- 不把上一轮计划和辩护重新伪装成当前事实；
- 从持久项目模型、本轮变化和按需证据重新组装请求体。

这样避免 Orchestra 自己成为第二个长期维护旧路线叙事的 Worker。

### 4.2 单次 activation 内允许完整 agentic loop

Fresh activation 不等于单次分类调用。一个 episode 内可以：

1. 读取当前 Human Intent 和项目模型；
2. 检查与本轮战略判断直接相关的 Git、代码和产物；
3. 运行产品、测试或关键实验；
4. 调用少量临时探索 Agent；
5. 比较候选项目推进方向；
6. 读取精简历史残留并进行校正；
7. 决定 Workstream 拓扑和下一步；
8. 写回项目状态变化和 Workstream Brief。

Orchestra 不应在每次 activation 全量重新理解整个仓库。它先使用项目模型和证据引用，只对影响本轮战略判断的承重事实做按需核实。

### 4.3 Fresh session 不等于放弃前缀缓存

不能复用的是旧模型生成的对话和策略叙事，不是所有稳定输入。

可以保持稳定并由模型服务缓存的前缀包括：

- Orchestra 职责和禁止项；
- 项目 Charter；
- 状态字段的语义；
- 输出要求。

每次变化的后缀包括：

- 当前项目模型；
- 最新事件 delta；
- 当前 Workstream 状态；
- 本轮按需证据。

### 4.4 Worker session 按 Workstream 连续

```text
Orchestra：fresh-by-default
Worker：stateful-by-workstream
Project Model：persistent
```

同一 Workstream 目标、承重假设和共享边界仍然稳定时，默认继续原 Worker session，以保留代码理解、调试状态和内部 SubAgent 结果。

以下情况可以启动 fresh Worker session，而不是机械 resume：

- Workstream 被实质重定义；
- 承重假设被推翻；
- 共享边界发生重大变化；
- 原 session 多轮没有实质项目增量；
- Worker 持续维护已失效的局部叙事；
- 需要真正独立的验证或替代方向。

Worker session 是局部执行缓存，不是项目事实源。

## 5. 持久项目模型

持久项目模型提供长期连续性。它是可直接修订的当前状态，不是 append-only decision ledger、冻结 contract 或需要长期兼容迁移的 Runtime schema。

但它也不能只保存“已核实事实”。Fresh Orchestra 要避免随机摇摆，还需要知道当前承重假设、未闭合矛盾和 Workstream 关系。项目模型至少包含以下内容。

### 5.1 Human Intent

优先保留 Human 原话或忠实转述：

- 长期目的；
- 当前重大方向；
- 可观察成功结果；
- 真实约束；
- 后续明确修改和撤回；
- 已确认的非目标。

Worker 或 Orchestra 的实现不能反向扩张 Human Intent。

### 5.2 Observed Project Reality

只保存影响项目级判断的当前事实，例如：

- 已真实运行或验证的端到端能力；
- 已确认的用户流程；
- 生产、部署、CI、benchmark 或用户反馈事实；
- 已形成的外部协议和数据承诺；
- 当前真实失败和阻塞；
- 哪些结果已经集成，哪些仍是孤立产物。

事实应能指向代码 revision、测试、日志、运行结果或 Human 决定。代码和 Git 可直接恢复的普通细节不重复抄写。事实若因后续改动可能失效，应在使用时重新核实，而不是永久视为真理。

### 5.3 Load-bearing Assumptions

保存一旦错误会使多个工作面失效的项目级假设，例如：

- 某现有抽象可以成为统一扩展边界；
- 某外部协议能够映射到统一任务语义；
- 某共享接口在多个 Workstream 之间保持稳定；
- 当前反馈手段足以判断实现是否正确。

每个承重假设只需保留：

```text
假设 / 当前依据 / 失效信号
```

这不是推理历史，而是下一次 fresh activation 必须能够检验的因果残留。

### 5.4 Convergence Residuals

保存 Orchestra 当前对“项目为什么仍未成立”的精简解释，例如：

- 核心能力尚未形成端到端闭环；
- 两个已有能力尚未集成；
- 关键协议语义仍不明确；
- 目前无法观察真实失败模式；
- 一条路线局部持续成功，但没有减少项目级风险。

Residual 不是初始 Goal 的机械拆分，也不是代码 TODO。它可以随新证据被重写、合并或删除。

### 5.5 Active Workstreams 与拓扑

每个活动 Workstream 只保存项目级信息：

```text
希望产生的 Project Delta
当前 Worker session / workspace
局部决策所有权
依赖的承重假设
与其他 Workstream 的共享边界
何种变化必须通知 Orchestra
当前是否仍可独立推进
```

不保存 Worker 内部任务树、文件计划和 SubAgent 结构。

### 5.6 精简结果与已停止路线

每次重要结果只保存：

- 原本想产生什么项目变化；
- 实际产生了什么可验证变化；
- 哪个承重假设成立、失败或仍未知；
- 对当前 Residual 和 Workstream 拓扑造成什么影响。

只有实际成本较高、未来容易重复的失败路线才保留停止记录：

```text
路线 / 原假设 / 反证 / 当前结论
```

不保留所有候选路线和普通局部失败。

### 5.7 Pending Human Decisions

只保存真正等待 Human 的产品方向、外部承诺、不可逆风险或价值取舍。

### 5.8 Attention Conditions

每次 Orchestra activation 结束时，声明下一次值得被唤醒的条件，例如：

- 某 Workstream 完成、阻塞或推翻承重假设；
- 某共享边界发生变化；
- 关键 CI、实验或外部事件完成；
- Human Intent 发生变化；
- 多个 Worker 开始争用同一决策面；
- 项目可能已经满足停止条件。

Harness 只负责投递事件，不解释事件意味着什么。

## 6. 事实、判断和决定必须区分

Fresh session 只有在输入没有把旧判断伪装成事实时才真正有意义。项目模型中的内容应在语义上区分：

```text
Human commitment
Observed fact
Inference
Load-bearing assumption
Current decision
Open uncertainty
```

不要求第一版建立正式 schema，但文档和 prompt 必须保持这种边界。

上一轮决定可以保留为精简、可证伪的 decision residue：

```text
当前方向
依赖的承重假设
预期应观察到的信号
明确的失效条件
仍未解决的风险
```

它不是权威，也不是辩护记录。改变方向应由新增证据、已触发的失效条件或 fresh inspection 发现的项目级矛盾驱动，而不是仅因新 session 产生了不同措辞。

## 7. 不持久化什么

禁止把以下材料作为 Orchestra 长期状态：

- 完整 Orchestra transcript；
- chain of thought；
- 旧工具调用历史；
- 所有候选路线的长篇比较；
- 上一轮 Orchestra 的设计辩护；
- Worker 的完整会话；
- Worker 内部 SubAgent transcript；
- 每轮 review 输出；
- 普通局部实现计划和失败记录；
- 已投入 token 或时间作为继续路线的理由；
- phase graph、concept surface 或 append-only decision ledger。

原则：

> **持久化当前项目模型和可证伪的决定残留，不持久化认知轨迹。**

## 8. 最小充分上下文组装

每次 activation 的上下文分为四层：

```text
Stable Prefix
  Orchestra 职责、项目 Charter、硬边界、输出要求

Current Project Model
  Reality、Assumptions、Residuals、Active Workstreams

Latest Delta
  自上次项目决策以来的新事实、Human 变化和 Worker 结果

On-demand Evidence
  本轮战略判断实际需要的代码、diff、测试、日志和运行结果
```

不直接拼接上一轮 conversation，也不全量复制 Worker transcript。

### 8.1 两段式上下文揭示

两段式不是“先什么都不知道，再看完整历史”，而是：

```text
第一段：对旧策略和 Agent 叙事盲，对当前事实不盲
第二段：揭示精简 decision residue 和解释性材料，进行校正
```

#### 第一段：strategy-blind inspection

先提供：

- Human 当前 Intent；
- 当前项目模型中的事实部分；
- 当前产品和相关代码；
- 原始 diff、测试、CI、日志、运行结果；
- 最近 Worker 的实际产物引用。

暂不提供：

- 上一轮 Orchestra 为什么选择当前路线；
- 上一轮计划和设计辩护；
- Worker 对自己结果的总结性解释；
- Worker 对下一步的建议；
- 已投入多少成本；
- 详细阶段历史。

Orchestra 先形成并固定一个初步判断：

```text
当前真实完成了什么？
项目为什么仍未收敛？
哪些事实或假设最承重？
当前 Workstream 应继续、暂停、合并、终止还是扩展？
如果今天首次接手，最有价值的下一项 Project Delta 是什么？
```

第一段可以按需检查证据，但不应先读 Worker 的叙事摘要。

#### 第二段：history-aware reconciliation

随后只揭示：

- 上一轮 decision residue；
- Load-bearing Assumptions 及失效条件；
- 精简重要结果；
- 已停止路线；
- Worker 对结果和下一步的解释。

Orchestra 将第一段判断与历史残留和新增事实比较，最终选择：

```text
保留原方向
修正边界或假设
反转方向
先做有限判别实验
暂停、合并或终止 Workstream
扩展新的独立 Workstream
请求 Human
停止项目
```

第二段的作用是校正，不是要求 fresh Agent 继承旧策略。

## 9. Workstream 与多 Worker

### 9.1 默认一个 Worker

默认 `N = 1`，因为成熟 Worker 在连续上下文中已经能完成一个明确目标下的内部拆解、并行调查、实现、测试和整合。

Orchestra 不应把同一个 coherent Workstream 再拆给多个外层 Worker。

### 9.2 何时扩展为多个 Worker

只有出现多个可独立维持局部决策循环的推进面时才扩容。判断重点不是目录是否不同，而是协调耦合是否足够低：

- 双方是否需要频繁共享局部推理；
- 一方的普通决策是否经常使另一方失效；
- 是否同时修改同一组承重抽象或共享协议；
- 是否能各自产生独立、可验证的 Project Delta；
- 是否只需在少数明确边界交换结果；
- 暂停其中一个时，另一个是否仍能有效推进。

适合多 Worker 的典型情况：

- 多个已形成稳定边界的正交工作面；
- 两条互斥路线的独立原型，用于消除项目级不确定性；
- 主实现继续推进时，另一个 Worker 独立调查外部协议或建立故障复现；
- 多仓库、部署环境或 SDK 之间边界已经明确的工作。

不适合多 Worker 的情况：

- 只是因为任务很大；
- 多个 Worker 需要持续修改同一核心抽象；
- 架构仍未形成，任务边界只是猜测；
- 工作主要依赖连续理解、调试和即时集成；
- 最终瓶颈是一个高度耦合的集成问题。

### 9.3 拓扑动态变化

Workstream 拓扑不是预先冻结的 DAG：

```text
发现独立工作面 → START 新 Worker
协调耦合升高   → PAUSE、MERGE 或串行化
承重假设失效   → STEER、CANCEL 或替换 session
方向被证伪     → CLOSE / DISCARD 对应路线
边界稳定       → 再次允许扩展
```

Orchestra 管理的是项目级局部决策中心，不是文件级任务队列。

### 9.4 两种 SubAgent 不应混淆

Worker 内部 SubAgent：服务于同一个 Workstream，由父 Worker即时整合，Orchestra 不管理。

Orchestra episode 内临时探索 Agent：只为战略判断读取事实、比较路线或反证，不成为长期固定角色。

Execution Worker Pool：由 Orchestra 按项目级独立 Workstream 动态配置，每个 Worker 都是成熟 Agent 实现。

## 10. 项目决策边界与触发条件

Orchestra 不常驻，也不检查每个 Worker action。典型触发：

- 某 Workstream 产生候选项目级结果；
- Workstream 确认阻塞、失效或推翻承重假设；
- Human 发来重大方向变化；
- 关键实验、CI 或外部事件产生结果；
- Workstream 之间的共享边界发生变化；
- 多个 Worker 的协调耦合明显上升；
- 连续执行没有产生实质 Project Delta；
- Worker 建议继续加固同一内部机制；
- 即将投入大规模或不可逆架构路线；
- 项目可能已经达到长期目标。

普通测试失败、局部实现选择、Worker 内部 SubAgent 调度和普通 commit 不触发 Orchestra。

## 11. 单次 Orchestra episode

### Step 1：Ground Intent

回答：

- Human 最终需要什么项目结果？
- 当前重大方向和外部边界是什么？
- 哪些目标来自 Human，哪些只是 Agent 自建结构？

### Step 2：Inspect Current Reality

执行 strategy-blind inspection：

- 当前真正能运行什么；
- 哪些端到端结果已验证；
- 自上次决策后项目现实发生了什么变化；
- 当前主要 Residual 和承重假设是什么；
- 活动 Workstream 是否仍然独立有效。

### Step 3：Generate Few Project Moves

只产生少量候选项目动作，不生成完整 roadmap。候选可以是：

- 继续或重定向现有 Workstream；
- 启动新的独立 Workstream；
- 暂停或合并高耦合 Workstream；
- 做一个有限判别实验；
- 建立当前确实缺失的反馈能力；
- 请求 Human；
- 停止投入。

### Step 4：Reconcile History

揭示精简 decision residue、停止路线和 Worker 解释，检查初步判断是否遗漏历史反证、重复失败路线或误解当前边界。

### Step 5：Choose Project Delta Portfolio

默认选择一个主要 Project Delta。只有存在真实低协调耦合时，才同时选择多个 Workstream Delta。

不冻结长期 roadmap，只决定当前值得运行的工作面和下一次战略关注条件。

### Step 6：Issue Workstream Actions

对活动 Workstream 作出继续、steer、暂停、合并、关闭、取消或新建决定，并为需要运行的 Worker 生成 Brief。

### Step 7：Patch Project Model

写回：

- 新增或失效的事实；
- 新增、修正或被推翻的承重假设；
- 新增、关闭或重写的 Residual；
- Workstream 拓扑变化；
- Pending Human Decisions；
- 下一次 Attention Conditions。

然后结束本次 Orchestra session。

## 12. Orchestra 输出

单次 episode 至少产生三类逻辑输出。第一版可以用 Markdown 表达，不建立 versioned schema。

### 12.1 Project Model Patch

```text
Facts added / invalidated
Assumptions added / revised / invalidated
Residuals added / closed / reframed
Workstreams started / steered / paused / merged / closed
Pending Human Decisions
```

### 12.2 Workstream Actions 与 Brief

项目级动作语义可以包括：

```text
START
CONTINUE
STEER
PAUSE
MERGE
CLOSE
CANCEL
WAIT
ASK_HUMAN
STOP
```

这些不是固定状态机，只是清楚表达战略意图。

### 12.3 Attention Conditions

说明下一次什么时候值得重新激活 Orchestra，而不是固定时间轮询或每个 Worker turn 都介入。

## 13. Workstream Brief

Brief 应短而完整：

```markdown
## Desired Project Delta
本 Workstream 希望让项目现实发生什么变化

## Why Now
为什么这是当前有价值的项目推进面

## Relevant Facts
与本工作面相关的已核实事实和证据引用

## Working Assumptions
当前允许 Worker 使用的承重假设及其失效信号

## Owned Decision Surface
Worker 可以自主决定的局部范围

## Shared Boundaries
与其他 Workstream 或外部协议共享、不得单方面改变的边界

## Must Preserve
已有能力和真实约束

## Evidence Required
完成后需要返回的可复核结果

## Report When
哪些项目级变化需要提前通知 Orchestra
```

不规定：

- 文件和类设计；
- Worker 内部步骤；
- SubAgent 数量；
- 测试实现方式；
- 局部 review 流程。

## 14. 什么算有效 Project Delta

下一项工作必须有项目级因果依据，但不要求每一轮都立刻产生用户可见 UI 或功能。

合法 Project Delta 至少属于一类：

- 新增用户可观察能力；
- 修复真实端到端失败；
- 集成两个已经孤立存在的能力；
- 消除一个承重不确定性；
- 建立后续判断必需、当前确实缺失的反馈或验证能力；
- 建立多个 Workstream 独立推进所必需的共享边界；
- 降低已确认的不可逆风险；
- 删除或停止已经被事实证明无价值的路线。

不能单独成为新工作理由：

- 代码已经有这个结构；
- 现有测试覆盖它；
- 上一轮留下 TODO；
- 继续完善会更严谨；
- 已投入很多 token；
- 新机制自身又产生了迁移、兼容、恢复或审计责任。

对内部机制执行的反事实检查应是：

> **即使不考虑这个机制自身已经存在，它是否仍然由 Human Intent、真实外部约束、已确认失败或承重不确定性直接要求？**

若答案是否定的，不把该机制的继续完善自动升级为项目目标。

## 15. 防止视野循环

### 15.1 Worker 总结只作为第二阶段线索

Worker self-report 必须与代码、测试、运行结果和外部事实区分。Orchestra 先独立观察原始证据，再读取 Worker 对结果和下一步的解释。

### 15.2 连续无 Project Delta 时重新 framing

如果连续执行：

- 修改同一内部子系统；
- 增加 schema、恢复、兼容和审计机制；
- 没有新增端到端能力；
- 没有验证关键假设；
- 没有减少真实失败或不确定性；

下一次 Orchestra 必须重新 framing，不默认沿用 Worker 的下一步建议。

### 15.3 允许路线稳定，不鼓励随机反转

Fresh session 的目的是减少旧叙事锚定，不是让每轮随机换方向。

当前方向默认可以继续，除非：

- 新证据触发既有失效条件；
- fresh inspection 发现此前遗漏的项目级矛盾；
- Workstream 的协调耦合、成本或价值发生实质变化；
- Human Intent 已改变。

### 15.4 临时 critic 不是永久 review 节点

只在连续无项目增量、即将大规模投入、存在路线锁定风险或 Human 重大方向变化时，临时调用独立观察者。Critic 回答路线问题，不做一般 code review，也不成为固定流程。

## 16. Human 介入边界

只在以下情况询问 Human：

- 多个产品方向都合理且事实无法裁决；
- 要建立新的外部兼容或产品承诺；
- 涉及不可逆发布、数据或权限风险；
- 长期目标彼此冲突；
- 价值、成本和时间取舍本身属于 Human 偏好。

不因内部实现、普通 bug、测试失败、文件结构或 Agent 自建 contract 询问 Human。

## 17. 第一版实验形态

第一版不实现新的 Runtime Kernel，可以由 Hermes 现有能力和当前主会话手工扮演 harness：

1. 用普通项目文件维护精简 Project Model；
2. 收集 Human 变化、Git/CI/运行结果和 Worker 产物引用；
3. 启动 fresh Orchestra Agent，执行两段式 episode；
4. Orchestra 输出 Project Model Patch、Workstream Actions 和 Brief；
5. 默认启动或恢复一个完整 Worker；
6. 出现真实低协调耦合时，允许启动第二个或更多独立 Worker session/workspace；
7. Worker 完成、阻塞或改变共享边界后，再启动新的 fresh Orchestra activation。

第一版只需要一个很小的活动 Workstream 清单，不建设通用 Worker registry、数据库、事件协议、checkpoint 或 decision ledger。

## 18. 验证方案

真正需要验证的假设是：

> 在长期、复杂、需求会变化且 Human 不持续纠偏的真实项目中，episodic Orchestra 是否比 Worker 自管理更稳定地维护项目方向、正确配置 1..N 个成熟 Worker，并交付更多端到端项目成果？

### 18.1 对照组

对照 A：单一 Worker 自管理

```text
Human 长期目标
→ Worker 完成当前工作
→ Worker 自己选择下一步
→ 连续推进
```

对照 B：单 Worker episodic Orchestra

```text
Human 长期目标
→ fresh Orchestra 选择一个 Workstream Delta
→ Worker 执行
→ fresh Orchestra 重新判断
```

对照 C：弹性 Workstream Orchestra

```text
Human 长期目标
→ fresh Orchestra 维护项目模型和 Workstream 拓扑
→ 默认一个 Worker，必要时扩展为多个成熟 Worker
→ fresh Orchestra 根据新证据合并、暂停或重定向
```

### 18.2 测试项目必须包含

- 连续 8～15 次真实需求或约束变化；
- 至少两个自然形成的低协调耦合推进面；
- 一个早期合理但最终错误的承重假设；
- 一次共享边界变化，使原并行工作应被合并或串行化；
- 一次局部任务成功但整体项目没有闭环；
- 一次需要暂停、终止或替换 Workstream 的情况；
- Human 只在预定点提供相同重大方向变化，不持续监管。

### 18.3 主要观察

- 最终端到端项目成果；
- 旧能力保留率和回归；
- 有多少 Worker run 产生真实 Project Delta；
- 过时假设下产生的无效工作量；
- 需求变化后恢复正确方向的速度；
- 是否正确识别低协调耦合 Workstream；
- 耦合升高后是否及时收缩并行；
- 共享边界冲突和重复实现；
- Human 被迫纠偏次数；
- 总成本和 wall time。

不以 LOC、阶段数、Agent 数、review 数或内部测试数量作为主要成功指标。

### 18.4 否证条件

出现以下结果时，应认为当前 Orchestra 设计没有证明价值：

- 只是生成更多文档、Brief 和 Worker run；
- 下一步选择不优于 Worker 自管理；
- 多 Worker 主要增加同步和集成成本；
- Fresh activation 频繁随机改变路线；
- Project Model 与真实仓库持续偏离；
- Human 仍需持续告诉系统下一步做什么；
- 相同预算下单一成熟 Worker 获得更好的最终项目结果。

## 19. 明确非目标

第一版不建设：

- 固定 phase 类型或 phase binding；
- 完整 roadmap 冻结；
- goal contract、progress ledger 或 goal gap reducer；
- versioned decision schema 或 append-only decision ledger；
- checkpoint、snapshot 或 compaction 系统；
- graph patch validator；
- artifact lineage 和复杂 provenance 协议；
- concept/dependency surface；
- 自动负向 review 或 cleanup；
- 固定 reviewer、planner、coder、tester 角色；
- 文件级外层任务拆分器；
- Worker 内部 SubAgent 管理；
- worker peer-to-peer 长期通信层；
- 持久推理历史。

只有长期对照实验出现经确认的实际缺口时，才考虑对应的最小自动化。