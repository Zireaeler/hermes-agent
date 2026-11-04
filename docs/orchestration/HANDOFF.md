# orchestra 当前接手说明

> 本文件只记录当前状态和下一步。稳定原则见 [`orchestra-design.md`](orchestra-design.md)，首版行为见 [`v1/design.md`](v1/design.md)，具体代码计划见 [`v1/implementation.md`](v1/implementation.md)，真实目标见 [`v1/targets.md`](v1/targets.md)。

## 2026 年 11 月 4 日本地 Codex 移交

本次移交保存此前云端尚未提交的自动运行、调用档案、项目互斥、有限团队与 Codex 执行控制实现，以及对应测试和文档。使用分支 `codex/orchestra-auto-runtime`；旧 `feature-kanban-runtime-kernel` 分支的基础提交不能代表这批云端工作已经同步。

研究能力接入目前只有已交付的完整设计：[`v1/research-integration.md`](v1/research-integration.md)。随后刚启动的少量提示试改已按用户要求撤销，不能把本次推送当作研究能力接入完成；Worker 研究纪律、任务知识交接和对应行为验证仍留待本地 Codex 实施。

接手目标可直接设置为：

```text
按照 docs/orchestration/v1/research-integration.md 完成研究能力接入。
增强 Worker 的条件式研究纪律，以及 Orchestra 对证据、条件和失败范围的保留与后继任务交接。
保持 state.md 为精简的当前项目判断，复用现有角色、运行器、权限、预算和会话生命周期。
完成必要的单元与机械回归检查，并更新实施记录。
PET 实际任务及研究效果留待后续单独验收，本次不运行 PET，也不启动真实模型长跑。
```

接手时注意：

- 先读本节与研究接入设计；下方保留的是首版历史接手记录，不能把其中旧的“一对一／股票模拟下一步”当成新的实施目标。
- 主系统提示位于 `hermes_cli/orchestra_v1.py`；Worker 固定约束位于 `orchestra_v1_worker.py`；团队提示与运行分别位于 `orchestra_v1_team_decision.py` 和 `orchestra_v1_team.py`。
- 团队 `keep` 不传新指令；改任务须先请求交回并确认终态，再 fresh 判断并 start。新 state 不会自动注入活动 Worker。
- 当前团队只写各自任务目录；没有通用多人共享主代码目录的合并调度。本次研究接入不应顺手扩大写权限。
- 既有 4 根 Worker、每棵树最多 3 个并发后代的数量来自此前对照实验资源配置，不是矩阵或固定角色架构；本次不改这些实现上限。
- Codex/provider 配置与认证在仓库外，用户提供的 `config.toml` 不提交。本地应使用自己的可用配置，不复制云实例的绝对路径、认证或旧活动会话。
- 没有必须额外安装到源码树的仓库外实现文件；原始研究方案和另一模型意见可作为参考附件，当前采纳设计以仓库文档为准。

本次推送前的检查结果与限制见 `v1/implementation.md` 的移交记录。未运行真实模型或 PET 验收。

以下保留首版历史接手记录。

## 当前状态

**Orchestra v1 已实现，并于 2026-09-01 通过真实三轮一对一闭环验收。**

当前已有：

- 每轮使用全新 `AIAgent` 的真实 `decide`；
- 人类意图、当前项目判断、任务、最近结果与 Git 事实的自动组装；
- 只限目标仓库的临时读取与搜索工具；
- 七个可直接查看和编辑的项目控制文件，其中 `intent.md` 只由人类维护；
- Codex app-server 的新建、恢复、中断、事件读取和最终消息收集；
- 新任务在新 thread 就绪后切换、继续任务恢复当前 thread 的实际边界；
- 每次 worker 运行都带有固定任务范围和结果说明约束；
- `init`、`decide`、`run-worker`、`step`、`run` 和 `status` 前台命令；
- `run` 在预算内自动衔接 worker 返回后的 fresh 决策，执行中周期只读观察，必要时中断并等待终态后重判；
- 同项目写入互斥，调用快照、用时及通知日志按需可查；
- 真实三轮闭环和自动化测试记录。

详细实现与验收结果见 [`v1/implementation.md`](v1/implementation.md) 第 10 节。股票模拟系统仍未初始化，它现在是闭环通过后的下一阶段，而不是用于代替 Orchestra 实现的演示。

## 首版固定边界

- 首版严格一对一，任何时刻只有一个活动 worker；
- 不管理 worker 内部子代理；
- 不建设多 worker、执行图、任务数据库和节点协议；
- orchestra 默认只读检查业务代码；
- 同一明确任务继续使用原 worker 会话；
- 实质不同的新任务启动新 worker 会话；
- 人类意图只来自 `intent.md`，Orchestra 当前判断只保存在一份可整体重写的 `state.md`；
- 不做字段校验、版本、迁移、兼容和自动修复；
- 人类显式启动前台运行；运行区间内自动决策，等待、询问、停止、用户中断及预算边界结束区间，不运行无人值守守护进程。

## 首版实现选择

orchestra 直接复用 Hermes 的 `run_agent.AIAgent`，每次 `decide` 创建全新实例并关闭普通记忆、人格和上下文文件。

worker 首版只接 Codex app-server，不建立通用 worker 接口。需要实现：

```text
thread/start
thread/resume
turn/start
turn/interrupt
事件流读取
最终消息收集
```

首版命令：

```text
python scripts/orchestra_v1.py init --project <path> --goal-file <path>
python scripts/orchestra_v1.py decide --project <path>
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path>
python scripts/orchestra_v1.py status --project <path>
```

这些命令已经实现并通过 CLI、单元、回归和真实闭环验证。

## 当前下一步

首版实现和机械验收已经完成。下一步按 [`v1/rollout.md`](v1/rollout.md) 初始化股票模拟系统，把它作为第一个真实长期目标，逐轮校准：

- Orchestra 的项目状态是否保持最小充分；
- 新任务与继续任务边界是否合适；
- worker 结果中哪些线索需要独立核实；
- 哪些变化应触发下一次人工决策轮。

不要在进入真实项目时顺手扩张多 worker、后台循环、状态协议或通用运行时。

## 最小控制材料

```text
$HERMES_HOME/orchestra/<project-key>/
├── intent.md
├── state.md
├── task.md
├── result.md
├── decision.txt
├── worker-thread.txt
└── last-orchestra-output.md
```

`intent.md` 是人类意图唯一来源，只有人类直接编辑；`state.md` 只保存 Orchestra 当前项目判断。其他文件用于当前任务、最近结果和机械运行。控制目录必须位于目标项目之外，避免 worker 工作区获得写权限。

## 当前文档

- [`README.md`](README.md)：文档入口与阅读顺序；
- [`orchestra-design.md`](orchestra-design.md)：顶层设计；
- [`v1/design.md`](v1/design.md)：首版行为设计；
- [`v1/implementation.md`](v1/implementation.md)：首版实现选择、实际代码记录与验收结果；
- [`v1/rollout.md`](v1/rollout.md)：从实现闭环到股票项目的落地顺序；
- [`v1/targets.md`](v1/targets.md)：股票模拟系统目标定义；
- [`experiments/`](experiments/)：已结束的历史实验归档。

本文件不是新的设计事实源。出现冲突时，按上述顺序读取。

## 首版完成情况

以下条件已经全部满足：

- `decide` 实际启动全新 orchestra 会话；
- 项目状态和 worker 任务可以自动保存；
- 一个真实 Codex worker 可以新建和恢复；
- worker 结果可以进入下一轮；
- 新任务与继续任务边界实际工作；
- 连续三个真实决策轮不需要人工复制粘贴材料；
- `等待`、`询问人类` 和 `停止` 不会启动 worker。

实际三轮 thread 边界、文件结果和测试数量记录在 [`v1/implementation.md`](v1/implementation.md) 第 10 节。股票模拟系统仍只是尚未初始化的下一阶段目标。
