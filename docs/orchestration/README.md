# orchestra 文档

## 当前状态

**当前只有设计文档，还没有可运行的 orchestra 实现。**

股票模拟系统是首个真实长期目标，但必须等最小一对一闭环实现并通过机械验收后再开始。

当前实施顺序：

```text
Codex worker 接入
→ 控制材料与命令
→ 全新 orchestra 决策轮
→ 一对一闭环
→ 三轮机械验收
→ 股票模拟系统
```

## 当前文档结构

```text
docs/orchestration/
├── README.md
├── orchestra-design.md
├── HANDOFF.md
├── v1/
│   ├── design.md
│   ├── implementation.md
│   ├── rollout.md
│   └── targets.md
└── experiments/
```

## 阅读顺序与职责

1. [`orchestra-design.md`](orchestra-design.md)
   
   顶层设计。说明 orchestra 为什么存在、与 worker 的边界、全新决策轮和一对一首版范围。稳定原则以此为准。

2. [`v1/design.md`](v1/design.md)
   
   首版行为设计。说明项目状态、上下文、项目决策边界、worker 会话边界和推进说明。

3. [`v1/implementation.md`](v1/implementation.md)
   
   当前真正的开工依据。说明首版需要增加哪些代码、如何复用 Hermes `AIAgent`、如何接 Codex app-server、命令和文件边界、实现批次、测试和完成定义。

4. [`v1/rollout.md`](v1/rollout.md)
   
   从“只有设计”到“可运行闭环”，再进入股票模拟系统的落地顺序。

5. [`v1/targets.md`](v1/targets.md)
   
   首个真实目标：历史行情驱动的本地股票模拟交易系统。它不是 orchestra 实现的前置步骤。

6. [`HANDOFF.md`](HANDOFF.md)
   
   当前状态和下一步摘要，不是新的设计事实源。与上面文档冲突时，以上面文档为准。

## 当前首版范围

```text
一个全新的 orchestra 决策轮
→ 一个当前明确任务
→ 一个真实 Codex worker
→ 任务结果或项目级新事实
→ 下一次全新的 orchestra 决策轮
```

首版只实现：

- `init`；
- `decide`；
- `run-worker`；
- `step`；
- `status`；
- 一份可整体重写的项目状态；
- Codex thread 新建、恢复、中断和结果收集；
- 人类显式触发的一对一闭环。

首版不实现：

- 多 worker；
- Claude Code 后端；
- 通用 worker 接口；
- 守护进程和自动轮询；
- 数据库和执行图；
- 状态版本、迁移和校验；
- 自动输出修复；
- 两段式上下文；
- 独立评审流水线；
- 持久 orchestra 推理历史。

## 首版完成定义

以下条件全部满足后，才可以开始股票模拟系统的真实运行：

- `decide` 每轮创建全新的 orchestra 会话；
- 项目状态和 worker 任务可以自动保存；
- 一个真实 Codex worker 可以新建和恢复；
- worker 结果可以进入下一轮；
- 新任务与继续任务边界可以实际运行；
- 在极小仓库连续完成三个决策轮；
- 全程不需要人工复制粘贴控制材料。

## 历史实验

[`experiments/`](experiments/) 保存三次负向评审实验和使用过的提示词。

这些实验已经结束，只用于说明为什么不把固定负向评审、清理流水线和结构复杂度门槛作为 orchestra 核心能力。实验文档不是首版运行流程。