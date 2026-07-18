# Phase 4G10.1 Clean Runtime Replay 执行总结

## 1. 结论

Run：`phase4g10-clean-large-ccf9c3e173`

| 结论轴 | 结果 | 含义 |
| --- | --- | --- |
| Runtime correctness | `passed` | consistency、checkpoint、固定 revision evaluator、事实幂等和进程边界均通过 |
| Clean replay invariants | `passed` | 当前提交的 HEAD 从全新 DB/workspace 完成运行，不依赖历史热修路径 |
| Effective orchestration | `passed` | primary 真实拆出 3 个隔离 durable children，并集成 3 份 frozen contributions |
| Task capability | `task-failed` | 最终 evaluator 为 F2P `54/68`、P2P `241/242`，没有 resolved |

本次测试的核心结果不是 `54/68`。它证明的是：Phase 4G10 运行中修复过的 durable orchestra 路径，
已经能从当前提交的 Runtime HEAD 干净重放，不需要修改运行中的代码或数据库。

## 2. 实际执行图

```text
primary: implement-srs-for-official-evaluator
  |
  +-- read-only early structure assessment
  |     |
  |     +-- reporting-plots-templates-completions
  |     +-- stage-pipeline-run-cache
  |     +-- tree-remote-import-transfer
  |             |
  |             +-- 3 isolated worktrees
  |             +-- 3 independent Codex sessions/processes
  |             +-- 3 scope-verified frozen patches
  |
  +-- original primary session resumes
  |     +-- verifies contribution hashes
  |     +-- applies and adapts all 3 contributions
  |     +-- owns shared CLI/Repo/schema integration
  |
  +-- evaluator round 1: 7/68
  +-- same primary consumes full feedback and remediates
  +-- evaluator round 2: 53/68
  +-- same primary consumes full feedback and remediates
  +-- evaluator round 3: 54/68
  +-- evaluated coverage stop
```

Runtime 一共创建 `7` 个 execution nodes：

- `1` 个 primary implementation node；
- `3` 个 durable child implementation nodes；
- `3` 个固定 revision verification nodes；
- `0` 个 strategy-update nodes。

Primary 使用唯一 backend session
`019f763b-99b0-7943-8a47-bbf36a9d0607`，发生 `4` 次 resume。三个 child 分别使用独立 session，
没有借用 primary 的隐藏上下文。

## 3. Early Assessment 如何拆分

Primary 首先只读审查仓库和 SRS，并确认三个低耦合责任簇：

| Child | 责任 | Frozen patch | Changed files |
| --- | --- | ---: | ---: |
| `reporting-plots-templates-completions` | plots、diff、metrics/params、模板和 completion | 28,516 bytes | 16 |
| `stage-pipeline-run-cache` | stage lifecycle、pipeline、run-cache | 36,160 bytes | 18 |
| `tree-remote-import-transfer` | tree/remote、import/update、transfer、compatibility | 31,955 bytes | 23 |

中央 CLI/Repo 绑定、跨责任簇 schema/serialization、版本元数据、共享 fixture 和最终 broad validation
保留给 primary。这样 child 的 declared write scope 可以隔离，同时不会把共享入口错误分给多个 writer。

三个 child 在同一时刻开始。它们的串行 wall time 合计 `6,265s`，实际并行窗口 `2,484s`，
并行节省上界 `3,781s`。三份 patch 均为非空、hash 固定、scope verified，且最终都被 primary 标记为
`modified_contributions`，没有 contribution 被静默丢弃。

## 4. Primary 集成

三个 child 完成后，Runtime 恢复原 primary thread，而不是创建新的 integration worker。Primary：

1. 重新校验三份 patch 的 SHA-256；
2. 审查并应用全部 frozen contributions；
3. 处理 child scope 之外的共享入口和兼容层；
4. 对 contribution 做 accepted/modified/rejected 归因；
5. 运行 focused、unit 和 broad verification；
6. 以 `candidate_ready` 将固定 workspace revision 交给 evaluator。

最终 attribution lineage 保留三份 artifact ID，并在后续两次 remediation resume 后继续有效，没有要求
primary 每轮重新触碰所有 child 文件来证明归因。

## 5. Evaluator 与修复闭环

| Round | F2P | P2P | 结果 |
| ---: | ---: | ---: | --- |
| 1 | `7/68` | `242/242` | 暴露大量目标版本 API 偏差 |
| 2 | `53/68` | `241/242` | 第一轮反馈带来显著提升，同时出现 1 个回归 |
| 3 | `54/68` | `241/242` | 仅新增 1 个 F2P，通过有界 coverage stop 结束 |

两份 evaluator failure bundle 均包含完整失败列表和结构化诊断，并由同一 primary session 消费。
第一次 remediation 主要补齐 run-cache、update、SCM、stage 和 plural plots API；第二次集中处理 plots、diff
和 stage path conversion。Worker 本地 evaluator-shaped tests 均通过，但 fixed evaluator 仍保留 14 个 F2P
和 1 个 P2P failure，说明 worker-visible local tests 不能替代官方 oracle。

第三次 evaluator 后没有继续追逐边际分数。Runtime 校验当前 workspace patch hash 与最新 evaluator candidate
hash 完全一致，写入 `validation_stopped_after_evaluated_coverage` 后停止。该事件只表示验证覆盖完成，
不把 benchmark goal 标记为 satisfied。

## 6. Clean Invariants

以下路径均为零：

- `_repair_resume_*` 和其他历史 repair events；
- `receipt_missing`、`receipt_invalid`、`receipt_recovery_requested`；
- speculative strategy node；
- duplicate terminal fact；
- duplicate progress-ledger fact；
- compaction deterministic fallback；
- credential scan hit。

其他关键结果：

- Runtime consistency：`0 violation / 0 warning`；
- supervisor owner 数：`2`，证明 daemon restart lineage 来自 DB；
- ownership canary：全部检查通过；
- checkpoint chain：有效；
- source revision 前后均为 `f617ca11826c6095153626e81f557a99ebdbcbce` 且 clean；
- context compactions：`4`，fallback=`0`。

Coverage stop 时 reducer 已创建但尚未派发 primary attempt 5。该 task 保持 `ready`，没有 task run、PID 或
额外模型调用；Runtime job 因 benchmark 未 resolved 仍是可恢复 active state。报告没有把它伪装成
terminal completion。Clean Replay runner 的验证生命周期已经结束，但原 job 仍可由 operator 显式续跑。

## 7. 成本与时间

- 开始：`2026-07-19 01:16:51 CST`；
- 结束：`2026-07-19 04:27:32 CST`；
- wall time：`11,441s`（约 3 小时 10 分）；
- decision rounds：`2`；
- graph patches：`2`；
- materialized task runs：`10`；
- aggregate input tokens：`85,057,485`；
- cached input tokens：`80,740,864`；
- cache ratio：`0.949251`；
- output tokens：`378,662`。

这些 token 数是 Codex CLI 累计 usage，不等于 provider 账单中的唯一 uncached token。高 cache ratio 说明
恢复原 primary session 确实复用了大量上下文，但 primary 在两次 remediation 中仍承担了主要成本。

## 8. 架构判断

本次运行证明系统级 orchestra 不再等价于“一个 worker 埋头执行到底”：三个 durable children 在独立
worktree 和进程中并行完成了可归因贡献，原 primary 负责共享入口和最终集成，Runtime 负责持久化、权限、
恢复和固定 revision 验证。

它没有证明 Runtime 的任务能力优于 Native Ultra，也没有达到先前 `63/68` 参考分数。更准确的结论是：

- durable split 的执行和恢复机制已经可靠；
- 三份 child 贡献确实进入候选，不是只增加可审计流程；
- 首轮 evaluator 后同一 primary 能显著修正集成结果；
- 第二轮 remediation 的边际收益很低，说明有界停止比十几轮循环合理；
- task quality 仍受责任切分质量、child 对目标版本 API 的理解和 primary 集成能力限制。

因此 Phase 4G10.1 可以作为阶段二产品化的前置证据，但不能把 benchmark evaluator 变成普通开发任务的
默认门禁。普通 Runtime job 只有在存在外部 oracle 或明确 independent-verification contract 时才应启用它。

