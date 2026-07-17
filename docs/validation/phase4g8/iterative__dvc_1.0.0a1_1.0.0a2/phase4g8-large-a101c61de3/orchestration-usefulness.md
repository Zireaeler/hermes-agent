# Phase 4G8 DVC Large: Orchestration Usefulness

## 结论

这次 Large 运行证明 Hermes 的长周期执行、恢复和独立验证机制有实际价值，但没有证明 durable graph expansion 能提高最终任务得分。

- Runtime correctness 通过：一致性 `0` violation、`0` warning，无重复 terminal/ledger fact，3 个真实 checkpoint 全部 accepted，compaction fallback 为 `0`。
- End-to-end capability 未通过：最终 official evaluator 为 FAIL_TO_PASS `55/68`、PASS_TO_PASS `241/242`，未 resolved。
- primary 同一 Codex thread 从 `0/68` 推进到峰值 `58/68`；这说明持续上下文、真实 evaluator 反馈和恢复循环有明显作用。
- evidence-backed graph expansion 后，新 strategy worker 的三次结果是 `55 -> 56 -> 55`，没有超过 primary 峰值。
- 新 strategy worker 扩大了实现覆盖和本地验证面，但同时回退了已被 evaluator 确认的行为。系统缺少 best-revision preservation 和 regression-aware rollback。

因此更准确的判断是：

> Runtime orchestration materially improves continuity, recovery, implementation breadth, and completion honesty. In this run, durable graph expansion did not improve the official oracle because strategy control did not preserve the best known candidate revision.

## 任务规模

目标不是单个 issue，而是 DVC `1.0.0a1 -> 1.0.0a2` 的 33 项软件演进要求。最终 candidate：

- 修改 66 个路径；
- patch 大小 94,524 bytes；
- primary 和 expanded strategy 使用两个隔离的 Codex thread；
- 10 次 Decision Provider patch，其中 5 accepted、5 rejected；
- 13 个 evaluator run receipt，其中第一个因诊断提取不完整不计入有效能力序列；
- 3 个真实 compaction checkpoint；
- 总运行约 4 小时 19 分钟。

## Evaluator 进展

### Primary same-session

有效 F2P 序列：

```text
0 -> 43 -> 52 -> 56 -> 57 -> 57 -> 58 -> 58 -> 58
```

Primary thread 是 `019f6a91-16c0-70f3-bd74-19698a993b77`，跨 materialization 恢复 12 次。它没有被拆成 analysis、implementation、test 等多个 runtime node，而是在一个责任内持续执行检查、实现、测试和修复。

这个结果支持 primary-node-first：复杂任务首先交给一个完整责任 worker 是合理的。Runtime 的价值不是预先拆细，而是让同一执行上下文在真实进程故障、daemon 重启和 evaluator 反馈之间继续存在。

### Evidence-backed expansion

Primary 在连续三次相同 failure signature、仅有 `test_id_only` 诊断且本地对应测试通过后，返回 blocking `structure_request`。Runtime：

1. 规范化并持久化 structure request；
2. 清理此前 speculative strategy branches；
3. 通过合法 graph patch 创建新 durable strategy node；
4. 使用全新 Codex thread `019f6b33-1f94-7943-84fe-1db2107a132f`，不继承 primary 隐藏上下文；
5. 对 strategy candidate 再运行 fixed-revision independent evaluator。

扩图后的 F2P 序列：

```text
55 -> 56 -> 55
```

扩图行为本身是合理且 evidence-backed 的，但结果没有超过 primary 的 `58/68`。

## 新 Worker 做出了什么

Expanded strategy worker 没有只重复 evaluator test IDs。它重新审计完整 SRS，并补充或修正了多个遗漏领域，包括：

- merged YAML outputs；
- Markdown diff；
- plot templates 和 plural plots API；
- tree streaming；
- run-cache；
- shell completion scripts；
- path normalization；
- 多项 CLI 和 repository compatibility 行为。

它的本地验证峰值为：

- unit：`428 passed, 9 skipped`；
- functional：`735 passed, 47 skipped, 15 environment deselected`。

这证明 fresh strategy context 能发现 primary 没有覆盖的真实工作，不是无效空转。但本地覆盖增加并不等价于 hidden oracle 改善。

## 为什么最终反而下降

主要不是 runtime 恢复失败，而是 candidate strategy 缺少以下控制：

1. **Best revision preservation**

   Runtime 记录了每次 evaluator 的 target revision 和分数，但没有把 `58/68` revision 作为不可丢失的 best-known candidate。新 worker 直接在共享 workspace 上继续修改。

2. **Regression-aware acceptance**

   Strategy worker 的 patch 没有先证明“保留全部已通过 oracle 行为”，再接受新增覆盖。最终重新引入了两个 revision-label 失败，并保留一个 PASS_TO_PASS 回归。

3. **Rollback policy**

   当扩图结果 `55/68` 低于 `58/68` 时，runtime 继续从当前 workspace 前进，没有自动回到 best revision，或将新改动按增量重新筛选。

4. **Evaluator diagnostic quality**

   大部分剩余 case 只有 test ID，没有 assertion、expected value 或 bounded traceback。Fresh worker 因此能做完整 SRS 审计，但无法对残余 hidden mismatch 进行精确修复。

## 对 Hermes 架构的含义

### 已证明有用

- 一个 coherent primary worker 能跨 12 次恢复保持同一 native Codex context；
- SIGKILL、daemon crash、lease takeover 和 receipt-before-ingest recovery 没有制造重复事实；
- evaluator 失败不会被 worker 的本地成功声明覆盖；
- real compaction 能在长周期运行中保持 checkpoint chain 有效；
- structure request 可以从 terminal receipt 进入 durable graph expansion；
- fresh durable worker 确实能从不同上下文发现更多实现缺口；
- 系统没有把未通过任务误标成完成。

### 尚未证明有用

- 多一个 durable worker 能提高 official oracle 峰值；
- Decision Provider 当前能可靠保护已获得的最佳候选；
- 扩图后的广覆盖修改能稳定转化为 hidden evaluator 收益；
- `test_id_only` feedback 足够支撑残余问题收敛。

## 最重要的反思

这次运行不支持“复杂任务应预先拆给多个 worker”。恰好相反：primary 单 thread 取得了最佳 official 分数，扩图只应在真实 evidence 表明 durable boundary 后发生。

但它也不支持“Runtime 没用，直接开一个 Codex 对话即可”。Primary 的结果依赖：

- 固定 revision evaluator；
- 多轮失败反馈；
- 12 次同 session resume；
- worker/daemon 故障恢复；
- checkpoint compaction；
- capability 和 network isolation；
- completion invariant。

这些都是 Runtime 提供的长周期执行条件。更准确的系统边界是：

```text
Primary worker
  owns the coherent implementation loop

Runtime
  preserves context, evidence, recovery, policy, and completion truth

Additional durable worker
  is an evidence-triggered exception and must preserve the best known revision
```

## 下一步工程改进

在继续追求更复杂 agent team 之前，应先实现：

1. 每次 evaluator 后保存 immutable candidate revision、F2P/P2P 和 failure signature；
2. 维护 best-known candidate，任何新 strategy candidate 只有不回归时才能取代它；
3. 扩图 worker 基于独立 worktree 修改，完成后做 delta evaluation，再决定 merge 或 rollback；
4. PASS_TO_PASS 回归优先阻止 candidate promotion；
5. 将 evaluator progression 和 best-revision decision 作为一等 runtime event；
6. 对 `test_id_only` 连续稳定失败设为证据不足边界，避免无限 speculative edits。

这些改进比增加更多 worker 更直接，因为本次失败不是缺少并行执行者，而是缺少跨 candidate 的策略控制。

## 证据边界

本报告只使用 DB 中持久化的 runtime facts、worker 可观察事件、bounded evaluator diagnostics 和 candidate patch metadata。未读取 gold patch、protected evaluator source 或隐藏模型推理。
