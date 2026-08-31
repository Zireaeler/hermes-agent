# orchestra 当前接手说明

> 本文件只记录当前状态和下一步。稳定原则见 [`orchestra-design.md`](orchestra-design.md)，首版行为见 [`v1/design.md`](v1/design.md)，具体代码计划见 [`v1/implementation.md`](v1/implementation.md)，真实目标见 [`v1/targets.md`](v1/targets.md)。

## 当前状态

**当前仓库里还没有可运行的 orchestra。**

已有内容全部是设计：

- orchestra 每轮使用全新会话；
- 从当前人类意图、项目状态、最近结果和按需证据重新组装上下文；
- 单轮内允许完整代理循环；
- 跨轮不继承旧对话、推理过程和策略辩护；
- 每轮最多选择一个当前任务；
- 任务内部交给完整 worker 自治；
- 长期连续性属于可直接重写的项目状态。

股票模拟系统只是首个真实长期目标，不能在 orchestra 代码尚不存在时直接开始运行。

## 首版固定边界

- 首版严格一对一，任何时刻只有一个活动 worker；
- 不管理 worker 内部子代理；
- 不建设多 worker、执行图、任务数据库和节点协议；
- orchestra 默认只读检查业务代码；
- 同一明确任务继续使用原 worker 会话；
- 实质不同的新任务启动新 worker 会话；
- 项目状态最多是一份可整体重写的自由格式 Markdown；
- 不做字段校验、版本、迁移、兼容和自动修复；
- 人类显式触发每个决策轮，不运行后台自治循环。

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
python scripts/orchestra_v1.py decide --project <path> [--human <text>]
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path> [--human <text>]
python scripts/orchestra_v1.py status --project <path>
```

当前这些命令尚未实现。

## 当前开工顺序

严格按以下顺序：

1. 编写最小 Codex app-server 接入，验证新建、执行、恢复、中断和结果收集；
2. 实现控制目录、`init`、`status`、原子文件写入和输出解析；
3. 接入全新的 Hermes orchestra，实现 `decide`；
4. 接入 `run-worker` 和交互式 `step`；
5. 在极小临时仓库连续完成三个机械决策轮；
6. 只有闭环通过后，才初始化股票模拟系统并开始真实调试。

不要先写股票系统代码来假装 orchestra 已经存在。

## 最小控制材料

```text
$HERMES_HOME/orchestra/<project-key>/
├── state.md
├── task.md
├── result.md
├── decision.txt
├── worker-thread.txt
└── last-orchestra-output.md
```

只有 `state.md` 是长期项目语义材料。其他文件用于当前任务、最近结果和机械运行。

## 当前文档

- [`README.md`](README.md)：文档入口与阅读顺序；
- [`orchestra-design.md`](orchestra-design.md)：顶层设计；
- [`v1/design.md`](v1/design.md)：首版行为设计；
- [`v1/implementation.md`](v1/implementation.md)：可直接开工的代码计划；
- [`v1/rollout.md`](v1/rollout.md)：从实现闭环到股票项目的落地顺序；
- [`v1/targets.md`](v1/targets.md)：股票模拟系统目标定义；
- [`experiments/`](experiments/)：已结束的历史实验归档。

本文件不是新的设计事实源。出现冲突时，按上述顺序读取。

## 首版完成定义

只有同时满足以下条件，才可以声称已有首版 orchestra：

- `decide` 实际启动全新 orchestra 会话；
- 项目状态和 worker 任务可以自动保存；
- 一个真实 Codex worker 可以新建和恢复；
- worker 结果可以进入下一轮；
- 新任务与继续任务边界实际工作；
- 连续三个决策轮不需要人工复制粘贴材料；
- `等待`、`询问人类` 和 `停止` 不会启动 worker。

在此之前，股票模拟系统仍然只是目标材料，不是已经开始的 orchestra 运行。