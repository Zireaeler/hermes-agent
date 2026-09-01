# orchestra 文档

## 当前状态

**Orchestra v1 已实现，并于 2026-09-01 通过真实三轮一对一闭环验收。**

当前阶段：

```text
Codex worker 接入：完成
→ 控制材料与命令：完成
→ 全新 orchestra 决策轮：完成
→ 一对一闭环：完成
→ 真实三轮机械验收：完成
→ 股票模拟系统：尚未初始化，是下一阶段
```

实际代码、模型接入、测试数量和三轮 thread 边界记录在 [`v1/implementation.md`](v1/implementation.md) 第 10 节。

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
   
   首版实现与验收记录。说明固定实现选择、实际代码落点、Hermes `AIAgent` 与 Codex app-server 接入、命令和文件边界、测试结果及真实三轮闭环。

4. [`v1/rollout.md`](v1/rollout.md)
   
   已完成首版闭环之后进入股票模拟系统的落地与校准顺序。

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

## 首版完成情况

以下条件已经全部满足：

- `decide` 每轮创建全新的 orchestra 会话；
- 项目状态和 worker 任务可以自动保存；
- 一个真实 Codex worker 可以新建和恢复；
- worker 结果可以进入下一轮；
- 新任务与继续任务边界可以实际运行；
- 在极小仓库连续完成三个真实决策轮；
- 全程不需要人工复制粘贴控制材料。

因此可以进入股票模拟系统的真实运行，但当前尚未执行该初始化。

## 历史实验

[`experiments/`](experiments/) 保存三次负向评审实验和使用过的提示词。

这些实验已经结束，只用于说明为什么不把固定负向评审、清理流水线和结构复杂度门槛作为 orchestra 核心能力。实验文档不是首版运行流程。