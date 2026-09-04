# Orchestra v1 项目规范与结构约束

> 本文约束 Orchestra v1 的工程组织和维护方式。产品行为以 [`../orchestra-design.md`](../orchestra-design.md)、[`design.md`](design.md) 和 [`worker-design.md`](worker-design.md) 为准；本文不重新定义产品语义。

## 1. 适用范围

本文适用于：

- `hermes_cli/orchestra_v1.py`；
- `hermes_cli/orchestra_v1_control.py`；
- `hermes_cli/orchestra_v1_decision.py`；
- `hermes_cli/orchestra_v1_worker.py`；
- `hermes_cli/orchestra_v1_codex.py`；
- `scripts/orchestra_v1.py`；
- 对应的 Orchestra 单元测试和真实 Codex 集成测试；
- `docs/orchestration/` 下的 Orchestra 文档。

若修改共享 `hermes_cli/codex_worker.py` 中的兼容导出或公共 Codex 能力，也必须遵守本文的职责边界。

旧 Runtime Kernel 设计和 phase 文档不是 Orchestra v1 的默认前置上下文。不得因为代码位于同一分支，就从旧 Runtime 的合同、账本、节点、receipt、恢复或评审机制派生 Orchestra 需求。

## 2. 文档权威顺序

出现冲突时按以下顺序处理：

1. `orchestra-design.md`：顶层角色、长期原则和首版范围；
2. `v1/design.md`：项目状态、决策方法、任务边界和运行节奏；
3. `v1/worker-design.md`：worker 上下文、子代理、验证和结果；
4. `v1/project-rules.md`：代码、测试和文档组织；
5. `v1/implementation.md`：已经落地的事实与验收记录；
6. `v1/rollout.md`、`v1/targets.md`：当前落地方法和真实目标。

`implementation.md` 不能因为记录了现有实现，就反过来把偶然结构升级为长期设计。若实现与前三项设计冲突，应先判断是实现偏离，还是设计确实需要修改。

## 3. 当前模块职责

当前代码已经按真实职责拆分，不应再把它描述为单文件首版。

```text
hermes_cli/orchestra_v1.py
  fresh Orchestra 构造、仓库只读工具与单轮应用协调

hermes_cli/orchestra_v1_control.py
  控制目录、七个控制文件、原子写入、Git 机械事实和 status

hermes_cli/orchestra_v1_decision.py
  决定类型、请求组装、输出解析与决策数据

hermes_cli/orchestra_v1_worker.py
  worker 固定约束、任务/thread 边界和结果落盘

hermes_cli/orchestra_v1_codex.py
  Orchestra 专属 Codex app-server 通信、恢复前压缩和业务 turn

scripts/orchestra_v1.py
  参数解析、前台确认、输出和退出码
```

这些模块的边界应保持稳定，但不是不可修改的冻结架构。若真实职责变化，可以调整；不得仅为形式对称继续拆出没有独立行为的文件。

## 4. 不可妥协的工程原则

### 4.1 每个模块只有一个主要变化原因

状态文件规则、决策语义、模型调用、仓库工具、worker 生命周期和 Codex 协议是不同职责。新增行为应放在它真正改变的模块中。

不得因为某个文件已有相似 helper，就继续把不相关职责塞入其中。

### 4.2 CLI 保持薄层

`scripts/orchestra_v1.py` 只负责：

- 参数；
- 调用应用接口；
- 人类确认；
- 面向人的显示；
- 退出码。

不得在 CLI 中实现：

- 项目状态语义；
- orchestra prompt；
- worker 会话判断；
- Codex JSONL 协议；
- 自动项目推进规则。

### 4.3 Orchestra 策略不得进入 Codex 通信模块

`orchestra_v1_codex.py` 只负责完成一个 Codex thread/turn 的机械通信和恢复。它不得判断：

- 当前任务是否值得做；
- 应继续还是开始新产品方向；
- worker 是否完成项目目标；
- 哪些结果应进入 `state.md`；
- 是否需要调查、实验或询问人类。

同样，`orchestra_v1_worker.py` 可以处理“开始新任务”和“继续当前任务”的机械边界，但不能从任务文本相似度推断项目方向。

### 4.4 控制材料保持简单文件

不得把七个控制文件逐步升级为：

- 固定字段数据库；
- append-only 事件账本；
- 版本化 schema；
- 状态迁移；
- 自动合并和修复；
- 多副本一致性协议；
- 因格式偶发变化而产生的第二个解析或修复 agent。

`intent.md` 和 `state.md` 的健康主要通过内容重写与删除维护，不通过新运行时机制维护。

### 4.5 不为未来多 worker 预建抽象

首版只接一个活动 worker。不得预先增加：

- worker registry；
- 调度器；
- mailbox；
- worker 间通信；
- 工作区合并协议；
- 通用 backend 插件接口；
- 外层任务图。

只有独立的多 worker 研究已经证明具体边界后，才讨论对应最小实现。

### 4.6 不用过度防御替代真实行为

必要的机械保护包括：

- 原子文件替换；
- 路径不能逃出目标仓库；
- 新 thread 成功后才切换当前 ID；
- 解析失败不覆盖旧状态；
- 超时和中断不伪装成完成。

默认不增加：

- 哈希链和证据包；
- 多层 gate；
- 对每个输出建立 validator；
- 为一次失败增加自动重试状态机；
- 全量数据或仓库审计；
- 为偶发格式问题建立兼容矩阵。

新增保护前必须说明它防止的真实故障、最小适用范围和停止扩张条件。

### 4.7 说明文字使用中文

新增或修改的规范、说明、代码注释和 docstring 使用中文。代码标识、协议字段、命令、路径、模型/provider 名称和外部原文可以保留英文。

## 5. 拆分与合并文件的判断

不设置机械行数上限。出现以下情况时，应调整职责边界：

- 新行为有独立数据类型、错误处理和测试；
- 修改一个行为需要穿过多个无关区域；
- 一个文件持续成为不同职责的冲突点；
- 测试开始需要完全不同的 fixture；
- 文件名无法解释其中主要内容。

不得通过以下方式假装结构良好：

- 只有一层转发的空包装；
- 没有第二个真实调用方的通用接口；
- 为可能出现的 provider 或 worker 预留插件；
- 把一个清楚的短函数拆成多个只为满足目录对称的模块。

反过来，如果多个极小文件只共同实现一个不可独立理解的行为，可以合并。目标是清楚的变化边界，不是文件越多越好。

## 6. worker 相关改动规则

修改 worker 任务边界、恢复提示、子代理指导、验证原则或结果格式前，必须先阅读 [`worker-design.md`](worker-design.md)。

实现应保持：

- 当前任务和项目规则高于旧会话叙事；
- 恢复或压缩后重新锚定当前任务；
- 不管理 worker 内部子代理的数量和步骤；
- 不把子代理报告自动提升为事实；
- 验证足以支持当前结论后停止；
- 最终结果保持精简；
- 不用结构化字段决定是否触发 orchestra。

worker 固定提示不应复制全部设计文档。只保留真正需要每次执行时生效的行为边界，其余通过目标仓库 `AGENTS.md` 和当前 `task.md` 提供。

## 7. 测试规则

### 7.1 测试按职责对应

测试应能直接定位到：

- 控制文件和路径；
- 决策请求与解析；
- fresh orchestra 会话；
- 仓库只读工具；
- worker 新建、恢复和结果落盘；
- Codex app-server 通信与压缩。

不得把所有新用例重新堆回一个测试文件。

### 7.2 测试验证行为，不冻结偶然结构

优先测试：

- 人类意图所有权；
- 旧 orchestra 历史不进入新决策轮；
- 解析失败不破坏当前状态；
- worker 会话边界；
- 中断和超时；
- 当前任务重新注入；
- 输出和结果的实际可用性。

避免测试：

- 私有 helper 的偶然调用顺序；
- 完整 prompt 字符串逐字相等；
- 目录结构和内部实现无法改变；
- 只为提高覆盖率而重复同一行为。

固定提示中的承重规则可以检查关键短语存在，但不应把整份提示冻结为快照。

### 7.3 真实模型测试独立

真实 Codex/模型测试与纯单元测试分开，默认快速测试不依赖网络、额度和外部认证。真实测试用于验证接口和恢复路径，不把模型一次输出的具体文本当成稳定断言。

## 8. 文档职责

- `orchestra-design.md`：顶层设计与长期原则；
- `v1/design.md`：首版项目状态、上下文和决策方法；
- `v1/worker-design.md`：worker 任务内行为；
- `v1/project-rules.md`：工程组织约束；
- `v1/implementation.md`：已实施事实、测试和真实故障记录；
- `v1/rollout.md`：真实项目落地与校准；
- `v1/targets.md`：首个真实目标；
- `HANDOFF.md`：当前接手摘要。

不得在 `implementation.md` 中继续写未来设计，也不得在 `design.md` 中堆运行日志和测试数量。出现新的长期主题时，可以建立一个职责明确的文档，但必须从 README 建立入口，不能复制已有规则形成第二事实源。

## 9. 什么时候先改文档

以下变化先修改设计文档，再改代码：

- orchestra 与 worker 的职责边界改变；
- 项目状态的所有权或语义改变；
- 新增长期输入材料；
- worker 会话边界改变；
- 新增自动项目决策边界；
- 引入多 worker；
- 引入固定独立评审；
- 改变首版明确非目标。

普通缺陷修复、实现拆分和不改变行为的重构，只需更新 `implementation.md` 中确实需要记录的事实。

## 10. 提交前检查

每次 Orchestra v1 改动提交前确认：

1. 改动是否直接服务当前真实问题？
2. 是否把项目语义放进了机械模块？
3. 是否因为现有文件方便而混入第二个职责？
4. 是否建立了没有当前真实调用方的抽象？
5. 是否把一次故障扩张成通用恢复、校验或审计机制？
6. 测试是否验证行为而不是冻结偶然实现？
7. 文档是否放在正确职责中？
8. 是否继续保持一对一边界？
9. 是否可以通过更小的状态、任务或提示词修改解决，而无需新增代码？
10. 改动完成后，项目是否更容易理解和维护，而不是只是机制更多？

若无法清楚回答，应先收缩改动。