# Phase 4G9 真实验证索引

Phase 4G9 在已 qualification 的 DVC Large 任务上，对比 native Codex parent/subagent
orchestration 与未来 Hermes Runtime 系统级 orchestration。

## Native Arm 1

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

Derived evidence extraction 后，原始 isolated Codex sessions 和 worker event stream 被删除，
当前 host 已无法恢复。Per-run artifact catalog 明确区分 preserved 与 lost evidence。
