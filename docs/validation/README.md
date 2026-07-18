# Hermes Runtime 真实验证索引

本目录是 Runtime 与真实模型源验证在 Git 中可检索的总目录。精简报告保存在 Git；原始
sessions、worker events、Runtime state 和 evaluator artifacts 可以保存在每次 run 的
`artifact-catalog.md` 所指向的持久 artifact store。

证据保留规则见：[Runtime 真实验证证据保留规范](../kanban-runtime-validation-artifact-retention.md)。

新 real run 默认使用 `/root/hermes-validation-artifacts`。Supported cleanup 只有在该目录
存在 verified `manifest.json` 时，才允许删除可重建 run entries。

## Phase 4G8

- [Phase 4G8 验证索引](phase4g8/README.md)
- DVC Large：`phase4g8-large-a101c61de3`，分类为
  `runtime-correct/task-failed`，primary 最佳 `58/68`，最终 `55/68`。

历史 Phase 4G8 runs 创建于 raw-evidence retention 成为硬要求之前。部分 run directory 可能
只保留 reports；在假设 native sessions 或 Runtime DB 仍存在前，必须检查对应 retention
record。

## Phase 4G9

- [Phase 4G9 验证索引](phase4g9/README.md)
- Native Arm 1 完整迭代：`phase4g9-arm1-iterative-20260717`，`task-failed`，best F2P
  `63/68`、P2P `242/242`，12 次 evaluator，54 个 native subagents。

旧单轮预备 run `phase4g9-arm1-native-20260717` 的原始 session 已在早期 cleanup 中丢失；完整
iterative run 已使用 verified stable archive 保留原始 evidence。

## Phase 4G10

- [Phase 4G10 验证索引](phase4g10/README.md)
- Runtime Arm 2：`phase4g10-arm2-large-059ea4b541`；
- 三轴结论：`runtime-correct / orchestration-effective / task-failed`；
- 真实结构：1 primary + 3 durable implementation children；
- 最终 F2P `63/68`、P2P `242/242`；
- 7 次 evaluator，总 wall time `14,830s`；
- 355 个原始 evidence 文件已进入 verified stable archive。
