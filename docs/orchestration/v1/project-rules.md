# Orchestra v1 项目规范与结构约束

> 本文是 Orchestra v1 的独立工程规范。修改 Orchestra v1 的代码、测试或文档前必须先阅读本文；它约束实现组织方式，不替代 [`design.md`](design.md) 中的产品行为边界。

## 1. 适用范围

本文适用于：

- `hermes_cli/orchestra_v1.py` 及其后续拆出的 `hermes_cli/orchestra_v1/` 包；
- `scripts/orchestra_v1.py`；
- Orchestra v1 在 `hermes_cli/codex_worker.py` 中使用或新增的接入代码；
- `tests/test_orchestra_v1.py`、`tests/test_codex_worker.py` 中的 Orchestra 用例及其后续拆分文件；
- `docs/orchestration/` 下的 Orchestra v1 文档。

产品行为以 `orchestra-design.md` 和 `v1/design.md` 为准；工程组织以本文为准；`implementation.md` 只记录已经落地的事实，不能反过来成为继续堆叠旧结构的理由。

旧 Runtime Kernel 设计和 phase 文档不是 Orchestra v1 的默认前置上下文，不得因为代码位于同一分支而从中派生 Orchestra 需求。

## 2. 不可妥协的结构原则

### 2.1 禁止把 Orchestra 当作单文件堆放区

不得因为现有入口位于 `hermes_cli/orchestra_v1.py`，就把后续状态存取、模型调用、仓库检查、决策解析、worker 协调和 CLI 行为继续追加到同一个文件。

每个模块应只有一个主要变化原因。能够独立解释、独立测试、独立替换的职责应位于独立模块，而不是依靠一个不断增长的文件和大量私有 helper 维持表面上的“简单”。

### 2.2 CLI 必须保持薄层

`scripts/orchestra_v1.py` 只负责：

- 参数解析；
- 调用应用层接口；
- 前台确认；
- 面向人的输出与退出码。

CLI 文件不得承载状态持久化、Orchestra prompt 组装、模型配置解析、Codex 协议处理或项目决策规则。

### 2.3 按真实职责拆分核心代码

以下职责彼此独立，出现持续修改时应分别落到模块中：

- 控制目录与原子文件操作；
- Git 与项目机械事实；
- 决策请求、输出解析和决策数据类型；
- fresh Orchestra Agent 的构造与单轮执行；
- repository-scoped 只读工具；
- worker thread 的新建、恢复和结果落盘；
- Codex app-server 通信。

这不是要求提前创建空目录、空接口或通用框架。只有真实代码出现时才建对应模块，但不得为了少建文件而混合已经清楚分离的职责。

### 2.4 共享 Codex 模块不得吸收 Orchestra 策略

`hermes_cli/codex_worker.py` 可以保留通用的 Codex app-server transport 和 turn 执行能力，但不得继续吸收 Orchestra 的决定语义、控制文件规则、项目状态或任务边界。

Orchestra 专属行为属于 Orchestra 模块。只有同时被其他真实调用方使用的底层 Codex 能力，才应进入共享 Codex 模块。

### 2.5 测试按职责镜像拆分

不得把所有 Orchestra 测试永久堆进一个测试文件。源代码拆出独立职责时，测试应同步按对应职责拆分；真实 provider/app-server 集成测试应与纯单元测试分开。

测试文件的划分应帮助定位行为，而不是按阶段、人员或临时任务命名。

### 2.6 文档各自只承担一种职责

- `design.md`：产品行为和边界；
- `project-rules.md`：工程组织约束；
- `implementation.md`：已实施事实和验收结果；
- `rollout.md`：落地顺序；
- `targets.md`：真实目标。

不得把未来设计、实现日志、项目规范、调试流水和验收证据继续混写到一个无限增长的 Markdown 文件中。出现新的长期主题时，为该主题建立明确命名的文档，并从 README 建立入口。

### 2.7 说明文字必须使用中文

Orchestra v1 新增或修改的说明性、规范性文档、代码注释和 docstring 必须使用中文。代码标识、命令、路径、协议字段、模型/provider 名称和引用的外部原文可以保留英文，但解释这些内容的文字必须使用中文。

## 3. 何时必须拆文件

不设置机械的行数上限。出现以下任一情况时，应在同一改动中先拆出所触及的职责，再增加新行为：

- 新功能引入第二个可独立测试的职责；
- 一组 helper 拥有自己的数据类型、错误处理或不变量；
- 修改一个行为需要同时穿过文件中多个无关区域；
- 单元测试开始需要按不同子系统建立互不相同的 fixture；
- 同一个文件持续成为无关改动的冲突点；
- 文件名已经无法准确说明其中主要内容。

不得通过只有一层转发的空包装、无真实使用方的抽象接口或预留插件系统来假装完成拆分。拆分后的模块必须直接承载真实职责。

## 4. 建议的自然演进结构

当下一阶段真实改动触及现有混合职责时，优先向以下结构演进：

```text
hermes_cli/orchestra_v1/
├── __init__.py       # 稳定公开入口
├── control.py        # 控制目录、文件与项目机械事实
├── decision.py       # 决策请求、解析与数据类型
├── agent.py          # fresh Orchestra Agent 单轮执行
├── repository.py     # 仓库范围只读能力
└── worker.py         # Orchestra 到 Codex worker 的任务/thread 协调

scripts/orchestra_v1.py

tests/orchestra_v1/
├── test_control.py
├── test_decision.py
├── test_agent.py
├── test_repository.py
└── test_worker.py
```

这是一条按真实修改逐步到达的方向，不要求一次性制造空壳文件。若实际职责产生更准确的命名，可以调整结构，但不得退回“所有功能放一个主文件”的做法。

## 5. 现有首版代码的过渡规则

当前首版已经落在 `hermes_cli/orchestra_v1.py` 和两个集中测试文件中。这是首轮落地事实，不是后续继续堆叠的许可。

自本文生效后：

- 单一职责内的小型缺陷修复可以直接修改原文件；
- 下一项实质功能若触及混合职责，必须同时把该职责迁入独立模块；
- 拆分应保持现有公开调用方式可用，不要求调用方一次性改写；
- 不做与当前改动无关的全仓库重构；
- 不把拆分升级为通用 Orchestra 框架、插件系统或多 worker 抽象。

## 6. 提交前检查

每次 Orchestra v1 改动提交前确认：

1. 新行为是否被放进了与其职责一致的模块；
2. 是否只是因为“已有这个大文件”而继续向其中追加；
3. CLI、Orchestra 策略和 Codex transport 是否仍然分层；
4. 测试是否能直接对应被修改的职责；
5. 文档是否仍各自承担明确且单一的用途；
6. 是否引入了没有当前真实需求的抽象或空壳。

若第 2 项为“是”，或者第 1、3、4、5 项无法明确回答，应先调整文件组织再提交。
