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

## 当前提示词入口

Orchestra 主系统提示在 [`hermes_cli/orchestra_v1.py`](../../hermes_cli/orchestra_v1.py) 的 `ORCHESTRA_SYSTEM_PROMPT`。普通决策和执行中观察共用该提示；`decide()` 为观察附加调用模式，说明只读、不传递意见和交回控制的语义。人类意图、当前状态与任务结果由 [`orchestra_v1_decision.py`](../../hermes_cli/orchestra_v1_decision.py) 组装，运行预算、唤醒原因和通知片段由 [`orchestra_v1_runtime.py`](../../hermes_cli/orchestra_v1_runtime.py) 提供。worker 固定约束则在 [`orchestra_v1_worker.py`](../../hermes_cli/orchestra_v1_worker.py)，不承担项目阶段选择。

主提示按授权、项目与阶段判断、证据边界、执行中检查、状态与任务输出组织。判断方法以 [`v1/design.md`](v1/design.md) 第 5 节为准；提示进入模型不等于这些行为已经可靠发生，真实检查的结果与限制另记于实施记录。

显式有限团队入口为 `scripts/orchestra_v1.py run-team`，团队提示和分配协议在 [`orchestra_v1_team_decision.py`](../../hermes_cli/orchestra_v1_team_decision.py)，执行循环在 [`orchestra_v1_team.py`](../../hermes_cli/orchestra_v1_team.py)。它按本次授权最多运行 4 个 worker、每棵 worker 树连根最多 4 个并发执行代理；默认一天总预算，没有单 worker 一小时超时或任务轮数上限。原单 worker 命令保持兼容。用户纠正后的 worldBox 测试采用独立研究后再比较旧报告，不能用原基线摘要启动独立阶段。

## 研究能力接入提案

[`v1/research-integration.md`](v1/research-integration.md) 定义条件式研究纪律、关键认识如何进入后继任务、单人与团队路径的接入边界，以及实施与行为验收方案。当前为待实施提案，不代表生产提示已经修改或研究能力已经验证。

## 当前文档结构

```text
docs/orchestration/
├── README.md
├── orchestra-design.md
├── HANDOFF.md
├── v1/
│   ├── design.md
│   ├── worker-design.md
│   ├── research-integration.md
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
- `run`：预算内自动决策、执行与周期只读观察；
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
- 无人值守守护进程与外部事件订阅；
- 数据库和执行图；
- append-only 事件账本；
- 状态版本、迁移和校验；
- 自动输出修复；
- 固定两段式上下文；
- 固定评审流水线；
- 持久 orchestra 推理历史。

## 当前重点

当前已补齐前台自动运行入口、worker 返回后的 fresh 决策、活跃执行的周期观察、交回控制的终态确认、同项目互斥和可检索调用档案。使用方式见 [`v1/rollout.md`](v1/rollout.md#自动运行入口)，行为边界见 [`v1/design.md`](v1/design.md#自动运行)。这解决机械闭环缺口，不代表长期项目判断可靠性已经得到证明。

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
