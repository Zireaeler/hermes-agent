# orchestra 文档

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
   
   可直接实施的首版设计。说明如何复用 Hermes 的 `AIAgent` 和现有 worker 能力、保存哪些本地材料、如何启动每轮、如何处理失败以及按什么顺序写代码。

4. [`v1/rollout.md`](v1/rollout.md)
   
   真实项目中的落地和调试顺序。它不是形式化胜负实验，也不是完整项目演示方案。

5. [`v1/targets.md`](v1/targets.md)
   
   宏大目标的选择标准、历史校准材料和第一批真实目标。当前首选是多供应商视频任务能力。

6. [`HANDOFF.md`](HANDOFF.md)
   
   当前接手摘要，只记录当前状态和下一步，不是新的设计事实源。与上面文档冲突时，以上面文档为准。

## 当前首版范围

```text
一个全新的 orchestra 决策轮
→ 一个当前明确任务
→ 一个完整 worker
→ 任务结果或项目级新事实
→ 下一次全新的 orchestra 决策轮
```

首版不研究多 worker，不建立新的运行时内核，不把单 worker 长期目标涣散重新作为待证明假设。当前任务是把一对一 orchestra 的上下文、项目决策边界、worker 任务边界和推进说明在真实项目中调通。

## 历史实验

[`experiments/`](experiments/) 保存三次负向评审实验和使用过的提示词。

这些实验已经结束，只用于说明为什么不把固定负向评审、清理流水线和结构复杂度门槛作为 orchestra 核心能力。实验文档不是首版运行流程。
