# Phase 4G9 Arm 1 架构结论

## 结论

Native Codex Ultra orchestra 在复杂 brownfield 演进任务上是有效的执行系统，而不只是一个
“会开 subagent 的单 worker”。最强证据不是 54 个 subagent 的数量，而是 Round 3 在同一 parent
消费 official feedback 后，通过三个并行 cluster 和 parent integration 将 F2P 从 `14/68` 提升到
`54/68`。最终 best 达到 `63/68`、P2P `242/242`。

它同时没有完成任务。最后 5 条精确 command diff 契约在 Round 10 至 12 完全不变，说明高频通信、
更多 child 和更多本地测试不能自动消除不可观测的目标契约歧义。

因此当前能得出的结论是：

1. native parent/subagent 通信对耦合代码任务有显著价值；
2. 这种价值不等于最终质量保证；
3. Hermes Runtime 只有在最终质量不低于 native Arm 1 的前提下，证明 durable isolation、恢复和
   责任边界降低了执行成本或失败风险，才有架构价值；
4. 目前不能宣称 Hermes Runtime 比 native orchestra 更强，因为 Arm 2 尚未在相同协议下运行。

## Native Ultra 的优势

### 1. 局部发现可以立即改变分工

Parent 不需要等待 durable receipt 或新的全局 planning round。一个 subagent 的发现可以直接通过
`send_message` 影响另一个仍在运行的 agent，parent 也可以在 slot 释放后立刻把实现责任切换为审计、
回归或根因分析。`250` 次 message 和 `13` 次 follow-up 说明这种通道被实际使用。

### 2. Shared workspace 降低集成延迟

Subagent 能立即看到其他线程的修改，parent 可以持续读取 merged state、补共享入口并运行全局测试。
对于 plots、diff、stage、CLI 这类存在交叉调用的任务，这比完全隔离后再交换 patch 更直接。

### 3. Parent 保留全局责任

有效部分不是简单并发，而是 parent 同时承担：任务分解、局部实现、冲突处理、全局 requirement
mapping、merged diff review、测试和最终 candidate 冻结。独立 audits 发现了真实问题，但 parent
没有把 subagent 的“无遗漏”自报当成权威完成事实。

### 4. Child 对 parent transport 阻塞有一定韧性

Parent WebSocket 重连期间，已经启动的 child 仍可继续执行并写入自己的 session。恢复后 parent
继续接收结果，没有丢弃整个执行状态。

## Native Ultra 的弱点

### 1. Shared workspace 不是免费通信

同时写入相邻代码产生 patch preimage 冲突、责任重叠和移动目标。它减少显式 handoff，却增加隐式
协调成本。任务越耦合，这种取舍越合理；但可明确隔离的写域没有必要全部共享。

### 2. Ephemeral subagent 生命周期过短

后续 evaluator 每给出一轮新反馈，parent 往往重新创建一批 child。虽然 root thread 连续，但局部
domain context 没有形成 durable ownership。Round 7 以后剩余问题已高度集中，继续批量创建 child
的边际收益很低。

### 3. Compaction 保证继续运行，但会漂移

7 次 compaction 让六小时任务得以继续，也引入重复读取、重复审查和约束遗漏。实测中，压缩后的
context 未持续遵守“不在 workspace 外写测试 artifact”的约束。稳定 contract 每轮重注入是
Runtime Kernel 可以明确优于单长线程的地方。

### 4. 更多 agent 无法补足缺失 oracle

最后 5 条 evaluator 只暴露 test ID 和有限诊断。Parent 多次改变 dispatch seam、表头、hash、JSON
和空输出语义，本地 probes 均通过，但 official 分数不变。这不是简单的“没有认真修”，而是所有
agent 共享同一不完整外部信息后形成共同盲区。

### 5. 长时间运行需要更强隔离

Worker 在 Round 4 搜索全局 `/tmp` 时读到旧实验 artifact。没有 gold 或 protected source 泄露，
但这仍破坏严格实验隔离。per-run TMPDIR、外部 artifact mount 隔离和 archive-before-cleanup 都应
成为基础设施约束，而不能只写在 prompt 中。

## 对 Runtime Arm 2 的设计要求

Runtime Arm 2 不应以“节点越多越像 orchestra”为目标。更合理的设计是：

```text
primary integration owner
├── 少量可独立验收、可隔离写域的 durable implementation nodes
├── 必要的 independent verification node
└── evidence 驱动的 strategy update / recovery
```

具体要求：

- 初始 worker 应先做一次有限的结构评估，而不是埋头执行到 terminal 才允许报告边界；
- durable node 只用于能声明独立 outcome、write scope、capability 或验证责任的工作；
- 高耦合局部搜索、测试和小修复仍留给单个 worker 内部，不升级成 Runtime nodes；
- 必须有一个明确 integration owner 对共享入口、merged candidate 和最终验证负责；
- 节点间不依赖自由对话，但 receipt 必须携带足够的 evidence、changed scope、已知风险和下一责任；
- 每轮 worker context 重新注入稳定 goal、capability 和 workspace contract，避免 compaction 漂移；
- evaluator feedback 必须完整进入负责该 gap 的既有 worker，不能被 strategy node 截断或稀释；
- 对普通开发任务，没有独立 oracle 时不应机械增加 evaluator。独立验证只有在存在外部标准、
  安全责任或固定验收对象时才构成强门禁。

## 公平对照条件

未来 Arm 2 至少要与本 run 对齐：

- 同一 DVC Large base、dataset revision 和 official image；
- 相同模型源和可比 reasoning 档位；
- 相同完整 evaluator feedback；
- 相同 candidate freezing 与 best-revision 规则；
- 不设固定 evaluator task-round 上限；
- 同等 wall-time 或明确的资源预算；
- 记录 node 数、worker handoff、并行度、token/cache、compaction 和恢复事件；
- 首先比较最终 F2P/P2P，再比较时间和编排成本。

用户关心的核心门槛是：Runtime 可以比 native orchestra 慢，但最终结果不应更差。若 Arm 2 只让
流程更可审计，却在相同反馈下低于 `63/68`，则不能证明系统级 orchestra 对该任务有正价值。

## 本次结论的证据边界

本 run 是高价值行为证据，但不是严格隔离的 capability 排行榜结果：

- worker 未读取 gold patch 或 protected test source；
- worker 读取了两个全局 `/tmp` 历史 artifact；
- Round 2 是 evaluator ownership bug 导致的 infra 空转；
- Round 13 没有 candidate 和 evaluator，不计为失败轮；
- 停止原因是三轮相同 official failure set 后的 operator decision。

这些边界均保留在 `run-report.json`，没有从总结中抹去。

