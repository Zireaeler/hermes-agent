# Phase 4G9 真实验证索引

Phase 4G9 在已 qualification 的 DVC Large 任务上，对比 native Codex parent/subagent
orchestration 与未来 Hermes Runtime 系统级 orchestration。

## Native Arm 1 完整迭代运行

Run：`phase4g9-arm1-iterative-20260717`

- 结果：`task-failed`，未 resolved；
- Best candidate：Round 10；
- FAIL_TO_PASS：`63/68`；
- PASS_TO_PASS：`242/242`；
- 12 个 candidate，12 次 official evaluator；
- 同一 parent thread 消费 11 轮 evaluator feedback；
- 54 个 native implementation/audit subagents；
- 停止原因：Round 10 至 12 保持同一 5 条 official failure，operator 请求停止。

文件：

- [可读执行总结](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/execution-summary.md)
- [完整能力过程](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/capability-trace.md)
- [架构结论](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/architecture-conclusion.md)
- [Artifact 目录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/artifact-catalog.md)
- [结构化报告](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/run-report.json)
- [Best candidate patch](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-iterative-20260717/candidate.patch)

该 run 未读取 gold 或 protected test source，但读取了全局 `/tmp` 中两个旧实验 artifact，因此标记
为 `historical-artifact-contaminated`。原始 sessions、worker events 和 evaluator invocations 已进入
verified stable archive，未被删除。

## Native Arm 1 单轮预备运行

Run：`phase4g9-arm1-native-20260717`

- 结果：unresolved；
- FAIL_TO_PASS：`7/68`；
- PASS_TO_PASS：`242/242`；
- 一个 parent 和 8 个 implementation/audit subagents；
- official evaluator invocations：1；
- evaluator feedback turns：0。

文件：

- [Artifact 目录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-native-20260717/artifact-catalog.md)
- [可读执行总结](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-native-20260717/execution-summary.md)
- [架构结论](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-native-20260717/arm1-architecture-conclusion.md)
- [结构化报告](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-native-20260717/run-report.json)
- [Candidate patch](iterative__dvc_1.0.0a1_1.0.0a2/phase4g9-arm1-native-20260717/candidate.patch)

这是旧 one-shot preliminary evidence。Derived evidence extraction 后，原始 isolated Codex
sessions 和 worker event stream 被删除，当前 host 已无法恢复；不得用它替代上方完整迭代运行。
