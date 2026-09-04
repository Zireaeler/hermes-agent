# orchestra 文档

## 当前状态

**Orchestra v1 已实现，并已进入 stock-sim 的真实长期项目试运行。**

当前阶段：

```text
Codex worker 接入：完成
→ 控制材料与命令：完成
→ 全新 orchestra 决策轮：完成
→ 一对一闭环：完成
→ 三轮机械验收：完成
→ stock-sim 真实项目试运行：进行中
```

当前试运行已经覆盖领域调查、真实数据实验、集合竞价事件重放、直接买入和规则驱动买入等连续项目决策。它证明当前闭环可以真实运行并维持多轮项目状态，但不构成 orchestra 优于单 worker 的受控实验结论。

实际代码、模型接入和机械验收记录见 [`v1/implementation.md`](v1/implementation.md)。项目战略效果应继续通过真实运行观察，不以一次正面结果或普通工程收尾证明。

## 当前文档结构

```text
docs/orchestration/
├── README.md
├── orchestra-design.md
├── HANDOFF.md
├── v1/
│   ├── design.md
│   ├── worker-design.md
│   ├── project-rules.md
│   ├── implementation.md
│   ├── rollout.md
│   └── targets.md
└── experiments/
```

## 阅读顺序与职责

1. [`orchestra-design.md`](orchestra-design.md)

   顶层设计。说明 orchestra 为什么存在、三种连续性、项目级决策职责、一对一首版和明确非目标。稳定原则以此为准。

2. [`v1/design.md`](v1/design.md)

   首版详细设计。说明控制材料、项目状态、上下文组装、单次决策方法、任务边界、运行粒度和防止状态腐烂的规则。

3. [`v1/worker-design.md`](v1/worker-design.md)

   worker 详细设计。说明任务内连续上下文、恢复后重新锚定、子代理使用、不确定性处理、验证停止条件和精简结果返回。

4. [`v1/project-rules.md`](v1/project-rules.md)

   首版工程规范。约束代码、测试和文档按真实职责拆分，禁止继续把 Orchestra 功能堆进单个文件或扩张成新 Runtime。

5. [`v1/implementation.md`](v1/implementation.md)

   已实施事实与验收记录。说明当前代码落点、Hermes `AIAgent` 与 Codex app-server 接入、命令、控制文件、测试和真实机械闭环。

6. [`v1/rollout.md`](v1/rollout.md)

   真实项目落地与校准方式。它描述如何运行和观察，不替代设计文档。

7. [`v1/targets.md`](v1/targets.md)

   首个真实目标：历史行情驱动的本地股票模拟交易系统。

8. [`HANDOFF.md`](HANDOFF.md)

   当前状态和下一步摘要，不是新的设计事实源。出现冲突时以前面的设计与工程规范为准。

## 当前首版结构

```text
当前人类意图、项目状态、最近结果和仓库事实
                    ↓
           全新的 orchestra 决策轮
                    ↓
              一个当前完整任务
                    ↓
               一个真实 worker
                    ↓
       可观察结果、证据、失败或承重发现
                    ↓
           下一次全新的 orchestra 决策轮
```

首版只实现：

- `init`；
- `decide`；
- `run-worker`；
- `step`；
- `status`；
- 人类意图与项目状态分离；
- 每轮全新的 orchestra；
- worker 会话按当前任务连续；
- Codex thread 新建、恢复、压缩、中断和结果收集；
- 人类显式启动的一对一闭环。

首版不实现：

- 多 worker；
- 外层任务并行；
- Claude Code 后端；
- 通用 worker 插件系统；
- 守护进程和自动轮询；
- 数据库和执行图；
- append-only 事件账本；
- 状态版本、迁移和校验；
- 自动输出修复；
- 固定两段式上下文；
- 固定评审流水线；
- 持久 orchestra 推理历史。

## 当前重点

后续工作不应继续堆叠 Orchestra 运行时功能，而应在真实项目中观察：

- fresh orchestra 是否能从当前材料恢复正确项目局面；
- 是否正确区分调查、判别实验、实施和人类选择；
- 是否只处理最显眼、最容易闭合的 Git、测试和文档问题；
- worker 恢复或压缩后是否仍以当前任务为中心；
- worker 子代理是否减少噪声而不是增加整合成本；
- `intent.md` 和 `state.md` 是否保持当前化；
- `result.md` 是否足够精简又能支持下一次判断；
- 新证据能否推翻旧方向和已有投入。

只有重复出现、无法通过状态、任务说明和提示词解决的机械缺口，才进入代码实现。

## 历史实验

[`experiments/`](experiments/) 保存三次负向评审实验和使用过的提示词。

这些实验已经结束，只用于说明为什么不把固定负向评审、清理流水线和结构复杂度门槛作为 orchestra 核心能力。实验文档不是首版运行流程。