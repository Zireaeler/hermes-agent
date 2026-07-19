# Phase 4G13 自然 Medium 执行报告

## 1. 结论

本次验证使用冻结的 SWE-EVO `dask__dask_2023.6.1_2023.7.0`，没有向 worker
提供 candidate key、文件划分、gold patch、official test id 或 evaluator 诊断。

Runtime 确实完成了此前缺失的动态 orchestra 主链：

```text
一个 coherent primary
  -> 只读结构评估
  -> 自然发现三个低耦合责任
  -> Decision Provider 基于 checkpoint 扩图
  -> 三个 isolated child 并行实现
```

但本 run 不能证明系统级 orchestra 具有净正价值。三个 child 都完成了实际代码和本地
验证，却因 event-driven prompt 漏掉 receipt 字段语义而无法交付；最终 candidate 由新的
full-workspace recovery worker 从头重建。最终质量与 single-worker arm 完全相同，耗时约为
其 `2.96x`。

因此应分开下结论：

- 动态图编排能力：已真实发生并通过；
- child 并行执行能力：已真实发生；
- contribution 交接与集成：本 run 失败；
- Runtime 最终恢复能力：通过，但主要在补偿自身协议故障；
- 相比 coherent single worker 的净收益：未证明。

## 2. 冻结条件

| 项目 | 值 |
|---|---|
| Instance | `dask__dask_2023.6.1_2023.7.0` |
| Base commit | `85c99bc20abc382774cfb6e5bf5f2db76ac09378` |
| Dataset revision | `9b83d5af943ba7a17567336f5b18239f73960219` |
| 模型 | `gpt-5.6-sol` |
| Reasoning effort | `max` |
| Worker 内部 multi-agent | 禁用 |
| Fault injection | 无 |
| Official evaluator | 每个 arm 恰好一次，不回流 worker |
| Runtime run id | `phase4g13-runtime-medium-8f67fb1c7a` |

Oracle qualification 已在 base/gold 两侧完成，公开 manifest 只保留 hash 和资格事实，不包含
protected test 内容。

## 3. 实际 Execution Graph

```text
implement-2023-7-0-srs
  [primary / early assessment / waiting_dependency]
  |
  +-- runtime-cli-traceback-hardening
  |     [child / receipt protocol failed]
  |
  +-- dataframe-2023-7-semantics-maintenance
  |     [child / succeeded / contribution frozen]
  |
  +-- array-stats-rechunk-docs
        [child / receipt protocol failed]

recover-integrate-2023-7-0-srs
  [strategy_update / reconstructed full task / candidate_ready]
  |
  +-- verify-official-evaluator-resolved-50f821bd
        [official evaluator / failed 2 protected contracts]
```

共有 5 个非评估 worker node，另有 1 个 evaluator node。最终 candidate 并不是三个 child
patch 的集成结果，而是 recovery node 在 primary workspace 中重新实现的结果。

## 4. 时间线

以下为北京时间 2026-07-19：

| 时间 | 事件 |
|---|---|
| 21:37:21 | Runtime job 创建 |
| 21:38:06 | primary 开始只读 early structure assessment |
| 21:43:10 | primary 提交 evidence-backed `expand` checkpoint |
| 21:44:25 | Provider 接受拆分，graph revision 变为 2 |
| 21:44:26 | 三个 isolated child 同时 materialize |
| 21:50:49 | CLI child 完成实现，但 terminal receipt invalid |
| 21:51:11 | Array/docs child 完成实现，但 terminal receipt invalid |
| 21:53:41 | DataFrame child 完成实现，但 terminal receipt invalid |
| 21:55:40-21:55:51 | CLI、Array recovery receipt 再次 invalid，两个 node failed |
| 22:00:16 | DataFrame recovery 成功，冻结唯一 contribution |
| 22:00:16-22:14:51 | recovery patch 经两次 validator rejection、一次 provider timeout 后应用 |
| 22:14:52 | full-workspace recovery worker 启动 |
| 22:34:03 | 固定 candidate revision 后启动唯一 official evaluator |
| 22:35:09 | Evaluator 完成：F2P 3/5，P2P 707/707 |
| 22:35:10 | Harness 写入 evaluated stop，未向 worker 回流诊断 |

总 wall time 为 `3468.978s`，约 57 分 49 秒。

## 5. 自然拆分是否合理

Primary 在约 5 分钟只读审查后提出三个 child：

| Child | 责任 | 声明 write scope |
|---|---|---|
| CLI/traceback | CLI entry point 与 IPython traceback | `dask/cli.py`、`dask/base.py` 及对应测试 |
| DataFrame | from_pandas、rename、astype、first/last、merge tree | `dask/dataframe/**` |
| Array/docs | chisquare docstring 与 rechunk 说明 | Array stats/test 与一份 RST |

该拆分不是按 analysis/implementation/test 阶段拆分，而是按不重叠 subsystem ownership
拆分。Provider 引用 `worker_structure_checkpointed` event 作为 `durable_parallelism` evidence，
保留原 primary 作为 integration owner。这个判断是合理的，且任务本身确实具备并行性。

## 6. Child 的实际产出

三个 child 都不是空节点：

- CLI child 修改 4 个文件，focused tests `9 passed`，base regressions `128 passed`；
- DataFrame child 修改 5 个文件，focused tests `10 passed`，完整 IO 文件在 threads scheduler
  下 `75 passed, 4 skipped`；
- Array/docs child修改 2 个文件，相关 suite `49 passed, 1 expected xfail`。

它们没有被强制首轮 checkpoint。普通检查、修改和测试均留在 node 内，没有触发额外
Decision Provider。说明“只在真实结构事件时协调”的执行路径生效。

## 7. 协议故障

event-driven prompt 选择了专用分支，却漏掉普通 Runtime prompt 中的 contribution-child
receipt 约束。Worker 因此把以下机器字段当成自由文本：

- `accepted_contributions`：实际必须是 frozen artifact ID；
- `partial_goal_items`：实际必须是精确 goal-item key；
- `consumed_directive_ids`：实际只能是已交付 directive ID。

结果为：

| 指标 | 数值 |
|---|---:|
| `receipt_invalid_count` | 5 |
| `receipt_recovery_retry_count` | 3 |
| `invalid_resume_count` | 3 |
| `context_reacquisition_count` | 3 |

这不是模型未完成编码责任，而是 Runtime 给出的自然语言 contract 与 canonical validator
不一致。DataFrame retry 最终输出合法 receipt；另外两个 child 在 retry limit 内未恢复。

本阶段已修复：event-driven prompt 和 JSON schema 现在明确区分 goal key、contribution ID、
directive ID 与自由文本；child 明确使用 `succeeded`、不声明 integrated completion，并将三类
contribution attribution 数组留空。

## 8. Recovery 行为

Runtime 没有直接丢弃 job，而是根据全局 graph 发现原 integration owner 被 failed dependency
阻塞，尝试创建新的 coherent recovery owner。这证明了系统级状态和故障恢复有实际作用。

但恢复过程也暴露两个问题：

1. 第一个 patch 对新 `strategy_update` node 添加 dependency，validator 拒绝 unknown target；
2. 第二个 patch 新增 node 却缺少 conditional decomposition，validator 再次拒绝；
3. 第三个 provider 请求超时 300 秒；
4. 第四个请求补齐 `context_or_runtime_limit` decomposition 后应用。

结构 decision 共 6 次，其中 3 次产生有效结构变化，有效率 `0.5`。Provider 估算协调输入
`61,026` tokens、输出 `5,600` tokens。Validator recovery profile 已升级，明确要求在仍有
nonterminal execution node 时为单个新增 recovery node 提供 decomposition。

## 9. 最终 Candidate

Recovery worker 修改 15 个文件，在本地报告：

- focused suite：`40 passed, 1 expected xfail`；
- affected modules：`990 passed, 125 skipped, 13 deselected, 5 expected xfails`；
- `git diff --check`、`compileall`、pre-commit config 校验通过。

Official evaluator 只运行一次：

| 指标 | 结果 |
|---|---:|
| F2P | 3/5 |
| P2P | 707/707 |
| Resolved | false |
| Evaluator feedback consumed | 0 |

两项失败都属于隐藏的精确 contract：

- `Series.rename` 的 upstream patch 要求 `FutureWarning`，candidate 使用
  `PendingDeprecationWarning`；
- CLI upstream test 要求历史字符串 `exception ocurred`，candidate 使用正确拼写
  `exception occurred`。

这说明 candidate 对公开 SRS 的实现具有合理语义，但未复现 hidden upstream patch 的精确
warning 类型和拼写。由于本实验刻意不回流 evaluator 诊断，worker 没有第二次修复机会。

## 10. Artifact 与清理

稳定 archive：

```text
/root/hermes-validation-artifacts/phase4g13/
  dask__dask_2023.6.1_2023.7.0/
    phase4g13-runtime-medium-8f67fb1c7a/
```

Manifest 状态为 `verified`，335 个文件，共 22,313,902 bytes；模型源 key 文件被排除。归档
校验完成后已清理 run workspace、home、codex-home seed 和 child worktrees，共删除约 78 MiB
可重建数据。

原始证据入口：

- `reports/phase4g13-arm-report.json`；
- `reports/capability-trace.md`；
- `reports/candidate.patch`；
- `hermes-home/kanban.db`；
- `codex-homes/*/sessions/**/*.jsonl`；
- `runtime-contributions/**`；
- `manifest.json`。

`evaluated stop` 与 reducer 在同一秒发生过一次 race：reducer 已排出一个 remediation
materialization，随后 harness 终止 daemon，实际没有启动该 worker，也没有消费 feedback。
Archive DB 因此保留一个 `running` node projection；它是报告层残余状态，不代表第二轮执行。
