# Phase 4G10 Runtime Arm 2 执行总结

## 三轴结论

- Runtime correctness：`passed`；
- Effective orchestration：`passed`；
- Task capability：`task-failed`；
- FAIL_TO_PASS：`63/68`；
- PASS_TO_PASS：`242/242`。

`63/68` 仅为 Native Ultra 参考值，不是本实验硬门槛。

## 实际执行图

```text
primary early assessment
├── plots-diffs-output-model
├── stage-runtime-and-run-cache
└── tree-remote-and-streaming
          ↓ frozen contributions
primary original thread resume + integration + evaluator remediation
```

## Orchestra 断言

- PASS `initial_graph_single_primary`
- PASS `accepted_early_structure_checkpoint`
- PASS `two_or_three_durable_children`
- PASS `isolated_child_workspaces_and_sessions`
- PASS `child_scopes_declared`
- PASS `two_nonempty_frozen_contributions`
- PASS `two_contributions_integrated`
- PASS `primary_same_session_resumed`
- PASS `official_evaluator_fixed_revision`
- PASS `candidate_has_contribution_lineage`

## 节点

Primary：`implement-srs-and-validate`，resume `8` 次。

- `plots-diffs-output-model`：state=`succeeded`，workspace=`/tmp/hermes-phase4g10-arm2/iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/runtime-worktrees/plots-diffs-output-model`
- `stage-runtime-and-run-cache`：state=`succeeded`，workspace=`/tmp/hermes-phase4g10-arm2/iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/runtime-worktrees/stage-runtime-and-run-cache`
- `tree-remote-and-streaming`：state=`succeeded`，workspace=`/tmp/hermes-phase4g10-arm2/iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/runtime-worktrees/tree-remote-and-streaming`

Frozen contributions：`3`。

## Official evaluator 进展

| Round | F2P | P2P | Resolved |
| ---: | ---: | ---: | --- |
| 1 | 13/68 | 242/242 | no |
| 2 | 52/68 | 241/242 | no |
| 3 | 54/68 | 242/242 | no |
| 4 | 56/68 | 242/242 | no |
| 5 | 58/68 | 242/242 | no |
| 6 | 63/68 | 242/242 | no |
| 7 | 63/68 | 242/242 | no |

## 成本摘要

- 运行 wall time：`14830s`；
- durable child 串行总时长：`4062s`；
- durable child 实际并行窗口：`1501s`；
- 并行节省上界：`2561s`；
- implementation input tokens：`101444575`；
- cached input tokens：`97608448`；
- cache ratio：`0.962185`；
- output tokens：`452575`；
- context compactions：`6`。

本报告只证明 durable nodes 被真实创建、并行执行并进入最终 candidate；最终任务仍未 resolved。
