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
- Native Arm 1：`phase4g9-arm1-native-20260717`，unresolved，F2P `7/68`，
  P2P `242/242`。

Phase 4G9 Arm 1 保留了详细 derived report，但 cleanup 期间丢失了原始 Codex session 和
worker event files；对应 artifact catalog 已明确记录该事实。
