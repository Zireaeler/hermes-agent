# Phase 4G9 Arm 1 完整迭代执行总结

## 一句话结论

Native Codex Ultra 的 parent/subagent orchestra 对这个 Large 任务产生了真实且显著的工程收益：
同一 parent 在 12 轮中将 official FAIL_TO_PASS 从 `14/68` 提升到 `63/68`，同时最终保持
PASS_TO_PASS `242/242`。但它没有完成任务，最后三轮稳定停在相同的 5 条 command diff
契约失败，因此本 run 的正式分类是 `task-failed`，不是 resolved。

## 实验对象

```text
一个 gpt-5.6-sol ultra parent
+ native MultiAgentV2 subagents
+ 最多 4 个同时 active implementation threads
+ shared workspace
+ 每轮冻结 candidate 后运行 official evaluator
+ evaluator 失败反馈由同一 parent thread 继续消费
```

Hermes Runtime Kernel 和 Decision Provider 均未参与执行。本实验测量的是 Codex 正在探索的
native parent/subagent orchestration，而不是普通单 worker，也不是 Runtime Arm 2。

冻结任务为 SWE-EVO DVC Large：

- instance：`iterative__dvc_1.0.0a1_1.0.0a2`；
- base commit：`fc42ca721c25bdd24875c999e37fb4f589ecd63c`；
- FAIL_TO_PASS：68；
- PASS_TO_PASS：242；
- parent thread：`019f6f58-25b4-76d3-a491-ffb9b5d3e69c`。

## 最终结果

| 项目 | 结果 |
| --- | --- |
| Official resolved | `false` |
| Best candidate | Round 10 |
| Best FAIL_TO_PASS | `63/68` |
| Best PASS_TO_PASS | `242/242` |
| Candidate rounds | 12 |
| Official evaluator invocations | 12 |
| Evaluator feedback turns | 11 |
| Native implementation/audit subagents | 54 |
| Implementation turns | 67 |
| Context compactions | 7 |
| Peak implementation concurrency | 4 |
| Average implementation concurrency | `2.139541` |
| Wall time | `21994.483s`，约 6 小时 6 分钟 |
| Termination | `operator_requested_stop_after_evaluated_plateau` |

Round 13 只启动了 resume，随后发生 proxy connection refused。它没有模型响应、candidate 或
evaluator，因此被明确记录为 `discarded_without_candidate_or_evaluator`，不计入 12 轮结果。

## 分数进展

| Round | Worker | F2P | P2P | 主要含义 |
| ---: | --- | ---: | ---: | --- |
| 1 | fresh | 14/68 | 242/242 | 大范围本地测试通过，但目标 release 契约理解偏差很大 |
| 2 | resume | 14/68 | 242/242 | evaluator 后 ownership 错误导致 worker 无法写入，属于 infra 空转 |
| 3 | resume | 54/68 | 241/242 | feedback 驱动三个 cluster 并行修复，单轮净解决 40 条 |
| 4 | resume | 57/68 | 241/242 | 继续收敛，同时发现历史 artifact 污染 |
| 5 | resume | 60/68 | 241/242 | plots、stage 和 diff 契约继续修正 |
| 6 | resume | 62/68 | 241/242 | 剩余问题集中到 command diff 与 stage dry-run |
| 7 | resume | 63/68 | 241/242 | stage dry-run 解决，只剩 5 条 command diff |
| 8 | resume | 63/68 | 241/242 | 尝试 mock seam 和 revision call shape，无 official 改善 |
| 9 | resume | 63/68 | 241/242 | 重建 canonical stage 与 baseline diff，仍未改善 |
| 10 | resume | 63/68 | 242/242 | 修复唯一 P2P regression，成为 best candidate |
| 11 | resume | 63/68 | 242/242 | 调整 dispatch seam，5 条失败不变 |
| 12 | resume | 63/68 | 242/242 | 改写表头、hash 和 JSON 控制，5 条失败仍不变 |

最后 5 条失败均属于 `tests/unit/command/test_diff.py`：

```text
test_default
test_no_changes
test_show_hash
test_show_json
test_show_json_and_hash
```

Round 10、11、12 的官方失败集合完全相同。继续运行只会让 parent 在无法读取 protected test
source 的条件下反复猜测精确输出契约，因此停止是基于已评估 plateau，而不是固定轮数上限。

## Orchestra 实际做了什么

这不是“parent 一个人干活，subagent 只写报告”。可观察 evidence 显示：

1. Round 1 由 parent 动态形成 plots、diff、tree/remote 三个实现 cluster，parent 自己负责
   stage/run/repro、共享入口和最终集成；没有预设 planner/coder/tester 拆分。
2. 第一批实现结束后，parent 将空闲 slot 转为 schema、requirements、stage/run-cache、tree
   等独立审计责任；审计发现了 historical tree、missing checksum、warning semantics 等真实缺陷。
3. Round 3 收到完整 evaluator feedback 后，parent 重新建立 plots、stage/CLI、SCM/filesystem
   三个 cluster，并在同一轮把 F2P 从 14 提升到 54。这是 native orchestra 最强的价值证据。
4. 后续轮次持续按剩余失败重组 subagents。整个 run 可观察到 `spawn_agent=57`、
   `send_message=250`、`followup_task=13`、`wait_agent=127`，说明存在高频动态协调。
5. Parent 始终承担 merged diff、全局测试和最终 candidate 冻结责任，没有退化为纯调度器。

完整逐阶段过程见 [capability-trace.md](capability-trace.md)。

## 成本与边界

- implementation input tokens：`245410202`；
- cached input tokens：`236906496`；
- cache ratio：`0.965349`；
- output tokens：`970741`；
- reasoning output tokens：`505437`。

Native orchestration 的高频通信和 shared workspace 让局部发现能快速合并，但也出现了明显成本：

- 多个 writer 共享 workspace，发生 patch preimage 冲突和 ownership overlap；
- 每轮重新创建多个 ephemeral child，后期大量时间花在重新建立局部上下文；
- 7 次 compaction 带来重复审查，并出现 `/tmp` 使用约束漂移；
- official evaluator 只给 test ID 和 bounded diagnostics 时，最后 5 条精确契约无法继续定位；
- Round 4 读取了两个全局 `/tmp` 历史 artifact，因此能力结果必须标记为
  `historical-artifact-contaminated`。

没有证据表明 worker 读取了 gold patch 或 protected test source。污染不使运行无效，但使它不能
作为严格隔离的排行榜结果。

## 对 Hermes 的直接意义

本 run 证明 native orchestra 擅长高频、共享上下文、动态局部委派。Hermes Runtime Arm 2 不应
试图靠创建更多 durable nodes 复制这种通信方式。它需要证明的是：在相同 evaluator feedback 和
资源边界下，durable isolation、独立责任、恢复和稳定 contract re-injection 能否减少共享写冲突、
compaction 漂移和重复上下文获取，并至少达到同等最终质量。

详细判断见 [architecture-conclusion.md](architecture-conclusion.md)。

