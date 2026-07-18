# Hermes Kanban Runtime Kernel Phase 4G10.1

# Clean Runtime Replay

## 1. 背景

Phase 4G10 Arm 2 已证明 Hermes 能在真实 DVC Large 任务中执行 early structure assessment、三个
隔离 durable children、frozen contribution 和原 primary session 集成。但该运行过程中同时发现并
修复了 ownership、resume context、attribution lineage、receipt ingest、ledger 幂等和 lifecycle
reporting 问题。

因此 Arm 2 的结论是：修复后的运行可以恢复并收敛；它还不能单独证明当前 HEAD 从全新状态开始就
不会进入历史 repair 路径。

Phase 4G10.1 不重复比较模型能力，也不追求再次达到 `63/68`。它只验证当前 Runtime 实现能否从
干净状态完成一次有界、无运行中热修的 durable orchestra。

该阶段是冻结路径重放，不是重新决定 DVC Large 是否值得拆分。Phase 4G10 已经在同一锁定实例上
资格化三个责任簇：plots/reporting、stage/pipeline/run-cache、tree/remote/compatibility。Clean Replay
必须把这些簇和 primary-owned shared scope 作为透明的 assessment replay policy 交给 worker重新验证。
共享 CLI/Repo 入口、版本元数据和最终集成由 primary 持有，不能被误算为 child write-scope overlap。
若当前证据真正否定所有安全拆分，运行应明确失败，不得静默退化为单 worker 后仍声称 Clean Replay。

## 2. 目标

Clean Replay 必须从以下状态启动：

- 当前已提交 Runtime HEAD；
- 全新 Hermes DB；
- 全新 worker workspace；
- 全新 node Codex homes；
- 已锁定且通过 base/gold qualification 的同一 DVC Large 实例；
- 与 Phase 4G10 相同的真实模型源和普通 `max` worker profile。

运行必须覆盖：

```text
single primary
    -> read-only early structure assessment
    -> 2-3 isolated durable children
    -> non-empty frozen contributions
    -> original primary session resume
    -> explicit contribution attribution
    -> official evaluator failure
    -> same primary consumes feedback and remediates
    -> second evaluator failure
    -> same primary consumes feedback and remediates
    -> third evaluated candidate
    -> evaluated clean stop
```

如果 official evaluator 在此前已 resolved，Runtime 正常完成，不得为了覆盖 remediation 而制造失败。

## 3. 非目标

Phase 4G10.1 不要求：

- 达到或超过 Phase 4G10 的 `63/68`；
- 运行到 failure plateau；
- 固定 evaluator 总轮数后无条件终止；
- 再次注入 hard worker crash 或 receipt-before-ingest fault；
- 修改 benchmark、oracle、gold patch 或 evaluator；
- 在运行过程中修改 Runtime 源码后继续把同一 run 记为 clean。

## 4. Evaluated Coverage Stop

Clean Replay 的停止条件是语义条件，不是轮数预算。

非 resolved 情况下必须同时满足：

1. 至少三个 completed official evaluator attempts；
2. 至少前两份 evaluator feedback 已由同一 primary backend session 消费；
3. 当前 workspace patch SHA-256 与最新 evaluator 的 candidate patch SHA-256 完全一致；
4. 最新 evaluator feedback coverage 为 `current_failure_complete`；
5. 当前没有未评估的 workspace 修改。

满足后写入：

```text
validation_stopped_after_evaluated_coverage
```

停止只表示 Runtime 验证覆盖已满足，不表示 task resolved，也不写入 goal completion truth。

## 5. Clean Invariants

### 5.1 不使用历史 repair

以下事件必须为零：

- `phase4g8_contribution_attribution_branch_repaired`；
- `phase4g8_receipt_recovery_branch_repaired`；
- `phase4g8_structure_request_branch_repaired`；
- `phase4g8_resume_timeout_repaired`；
- `phase4g8_receipt_recovery_requeued`；
- `runtime_receipt_adapted`。

历史 repair 代码可以继续用于旧 DB migration，但本 run 不得依赖它。

### 5.2 Receipt 与 ledger

所有 Runtime Codex materialization 必须使用 Codex structured final output schema。Fresh、primary resume、
child 和 remediation 使用同一 canonical receipt contract；early assessment 使用独立 structure checkpoint
schema。机器 receipt 是裸 JSON，Markdown 过程文本只用于可读日志，不能作为 completion truth。

以下事实必须为零：

- `receipt_missing`；
- `receipt_invalid`；
- `receipt_recovery_requested`；
- speculative `strategy_update` node；
- duplicate terminal fact；
- duplicate progress ledger fact。

### 5.3 Ownership canary

Runner 在 shared worktree root 下预置一个 sibling directory 和一个指向 root 外文件的 symlink。
创建 children 后必须证明：

- worktree root owner 已切换为 trusted worker lane identity；
- sibling sentinel owner 和内容未变化；
- symlink target owner 和内容未变化；
- 至少两个真正的 child worktree 已创建。

该 canary 只验证 supervisor 文件边界，不进入 worker prompt。

### 5.4 Lifecycle 与 continuity

必须满足：

- controlled daemon restart 发生；
- DB 中至少有两个不同 owner 的 `runtime_supervisor_started`；
- primary backend session 唯一且发生 resume；
- 至少两份 evaluator feedback 由该 primary continuity 消费；
- 后续 attribution 可以引用首次 integration lineage；
- Runtime consistency 为 `0 violation / 0 warning`。

## 6. 报告与证据

必须生成：

- `clean-replay.json`：机器断言和事件计数；
- `clean-replay-summary.md`：中文可读执行过程；
- `arm2-orchestration.json`：节点、session、contribution 和 token 指标；
- `run-report.json`；
- `capability-trace.md/json`；
- `candidate.patch` 与 candidate hash；
- hash-verified raw artifact manifest。

报告必须继续分离：

```text
Runtime correctness
Clean replay invariants
Effective orchestration
Task capability
```

## 7. 清理和发布

只有 raw artifact archive manifest 校验通过后，才能删除 workspace、runtime worktrees、toolchain/cache
等可重建内容。DB、Codex sessions、worker events、provider audit、frozen contributions、报告和 candidate
patch 必须保留。

Clean Replay invariants 全部通过后，作为独立阶段提交并推送。随后才能开始将 durable orchestra
推广到普通 Runtime job；不得一边运行 Clean Replay 一边修改普通 job 产品化代码。
