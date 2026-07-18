# Phase 4G10 Early Structure Assessment 与 Durable Orchestra 验证

Phase 4G10 用同一个 SWE-EVO DVC Large 任务验证：Hermes Runtime Kernel 能否在 primary 完成真实
仓库审查后，创建少量隔离 durable workers，并将其贡献重新交给原 primary thread 集成。

## Phase 4G10.1 Clean Replay

Run：`phase4g10-clean-large-ccf9c3e173`

Phase 4G10.1 已使用提交 `f617ca1`、全新 DB/workspace 和同一冻结 DVC Large 实例完成 Clean Runtime
Replay。四轴结果为：

| 结论轴 | 结果 |
| --- | --- |
| Runtime correctness | `passed` |
| Clean replay invariants | `passed` |
| Effective orchestration | `passed` |
| Task capability | `task-failed`，最终 F2P `54/68`、P2P `241/242` |

该 run 真实完成 `1 primary -> 3 parallel durable children -> 3 frozen contributions -> original primary
integration -> 3 evaluator rounds / 2 same-session remediations`。历史 repair、receipt recovery、strategy node、
重复 ledger/terminal fact 和 consistency 异常均为零。它不以 resolved 或再次达到 `63/68` 为门槛。

- [Clean Replay 中文执行总结](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-clean-large-ccf9c3e173/execution-summary.md)
- [Clean Replay 完整过程](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-clean-large-ccf9c3e173/capability-trace.md)
- [Clean Replay machine assertions](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-clean-large-ccf9c3e173/clean-replay.json)
- [Clean Replay Artifact 目录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-clean-large-ccf9c3e173/artifact-catalog.md)

以下 Phase 4G10 Arm 2 是 Clean Replay 之前的历史对照运行。

## 普通 Runtime Job Smoke

Run：`rjob_74527b2a65c0`

Phase 4G10 的能力已通过普通 `runtime create` 和 production worker lane 路径完成一次非 benchmark smoke：
primary 只读评估后创建两个隔离 child，冻结两份 contribution，再恢复原 primary session 完成集成。
最终本地测试 `8/8`，goal satisfied，consistency 无异常，且 evaluator node 数量为 `0`。

- [普通 Runtime Smoke 中文总结](ordinary-runtime-smoke-rjob_74527b2a65c0/execution-summary.md)
- [普通 Runtime Smoke 结构化摘要](ordinary-runtime-smoke-rjob_74527b2a65c0/orchestration-summary.json)

## 正式结论

Run：`phase4g10-arm2-large-059ea4b541`

| 结论轴 | 结果 | 含义 |
| --- | --- | --- |
| Runtime correctness | `passed` | consistency、checkpoint chain、固定 revision evaluator 和幂等事实最终均通过 |
| Effective orchestration | `passed` | 真实创建 3 个隔离 child，3 份非空 patch 均进入 integrated candidate |
| Task capability | `task-failed` | official evaluator 最终为 F2P `63/68`、P2P `242/242`，未 resolved |

`63/68` 是对照结果，不是 Phase 4G10 的硬门槛。这里判定 orchestration 有效，依据是执行结构和
可归因贡献确实发生，而不是分数碰巧达到某个阈值。

## 实际结构

```text
primary: implement-srs-and-validate
  └── early read-only structure assessment
      ├── plots-diffs-output-model          38,778 bytes
      ├── stage-runtime-and-run-cache       25,151 bytes
      └── tree-remote-and-streaming         12,898 bytes
             ↓ three frozen contributions
      original primary thread resumes as integration owner
             ↓
      fixed evaluator and same-thread remediation
```

三个 child 同时开始，分别使用独立 worktree 和 Codex thread。它们的串行执行总时长为 `4062s`，
实际并行窗口为 `1501s`，理论并行节省上界为 `2561s`。Primary 对三份 patch 的最终分类均为
`modified`，没有 rejected contribution。

## 能力进展

```text
13 -> 52 -> 54 -> 56 -> 58 -> 63 -> 63 / 68
```

所有轮次最终均保持或恢复到 P2P `242/242`。第 6、7 轮是同一组 5 个 command diff failure，
因此使用经过 candidate hash 校验的 operator stop 正常归档，而不是按固定轮数强制终止。

## 三臂参考

| Run | 执行结构 | Best F2P | P2P | Wall time | Implementation input | Cache ratio |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Phase 4G8 ordinary Runtime | primary 后迟到 strategy expansion | `58/68` primary peak，final `55/68` | `241/242` | `15,526s` | 不同旧协议，不直接比较 | - |
| Phase 4G9 Native Ultra | 1 parent + 54 ephemeral subagents | `63/68` | `242/242` | `21,994s` | `245,410,202` | `0.965349` |
| Phase 4G10 Runtime Arm 2 | 1 primary + 3 durable children | `63/68` | `242/242` | `14,830s` | `101,444,575` | `0.962185` |

Arm 2 在本次观测中与 Native Ultra 得分相同，耗时和累计 input token 更低。但这不是严格的性能
优越性证明：两次运行的 Runtime 版本、故障修复和隔离历史不同，Native run 还存在历史 artifact
污染。可以确认的是，Runtime durable split 没有导致最终质量下降，并且不是单 worker 伪装。

## 文件

- [可读执行总结](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/execution-summary.md)
- [完整执行过程](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/capability-trace.md)
- [架构结论](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/architecture-conclusion.md)
- [Artifact 目录](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/artifact-catalog.md)
- [结构化 orchestra 报告](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/arm2-orchestration.json)
- [Runtime 报告](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/run-report.json)
- [最终 candidate patch](iterative__dvc_1.0.0a1_1.0.0a2/phase4g10-arm2-large-059ea4b541/candidate.patch)

Benchmark evaluator 只因为该任务预先提供固定 oracle 才有独立意义。Phase 4G10 不把这种 evaluator
推广为普通开发任务的默认节点。
