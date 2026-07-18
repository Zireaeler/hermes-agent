# Phase 4G10 Arm 2 架构结论

## 结论

本次运行证明了 Hermes Runtime Kernel 已能执行真实的系统级 durable orchestra，而不再是单个
primary worker 外包一层状态记录：primary 在只读仓库审查后提出三个责任边界，Decision Provider
基于 event `788` 创建三个隔离 child nodes；三个 child 并行执行并产生非空 frozen patch；原
primary thread 随后恢复，应用、修改并验证三份贡献。

最终结果为 F2P `63/68`、P2P `242/242`，与 Phase 4G9 Native Ultra 的 best 相同，但任务仍未
resolved。准确结论是：

```text
Runtime correctness: passed
Effective orchestration: passed
Task capability: task-failed
```

这三个结论互不替代。节点真实拆分不等于任务完成；任务未完成也不否定 durable orchestra 已经
发生。

## 拆分为什么有效

### 1. 拆分来自仓库证据

Primary 第一轮用 `313s` 检查代码、测试和共享入口，未修改 source，然后识别出三个相对独立的
责任：

1. plots、diffs 和 output model；
2. stage runtime 与 run-cache；
3. tree、remote 与 streaming。

Checkpoint 同时保留 `dvc/repo/__init__.py`、`dvc/cache.py`、全局兼容改造和 shared fixtures 给
primary 集成。它不是按 analysis/implementation/test 阶段拆分，也不是因为“任务复杂”而泛化拆分。

### 2. Child 不是只做审查或报告

三个 child 分别修改 `25`、`21`、`13` 个路径，并产生：

| Child | Patch bytes | Focused verification |
| --- | ---: | --- |
| plots-diffs-output-model | `38,778` | `182 passed`，另有 lint 与 completion 检查 |
| stage-runtime-and-run-cache | `25,151` | `48` focused、`122/137` broader suites |
| tree-remote-and-streaming | `12,898` | `264 passed, 22 skipped` |

三个 patch 均有 base revision、SHA-256、changed files、scope verification、materialization 和
session provenance。最终 attribution 将三项都标为 `modified`，没有 rejected contribution。

### 3. 隔离并行产生了实际时间收益

三个 child 同时开始。各自 wall time 为 `1501s`、`1275s`、`1286s`：

```text
serial sum:          4062s
parallel window:     1501s
savings upper bound: 2561s
```

这不是完整系统 speedup，因为 assessment、Decision Provider、集成和 evaluator 仍有成本；但它
证明低耦合责任被并行执行，而不是依次排队或由 primary 重做全部实现。

## Primary 仍然承担主要质量收敛

Durable children 完成 breadth implementation 后，首个 integrated candidate 只有 `13/68`。之后同一
primary thread 消费 evaluator 反馈，进展为：

```text
13 -> 52 -> 54 -> 56 -> 58 -> 63 -> 63
```

因此不能把最终 `63/68` 全部归因于 child split。更准确的分工是：

- child nodes 快速建立三个大责任域的实现基础；
- primary 解决共享入口、patch adaptation 和跨域兼容；
- primary 的连续 remediation loop 提供了大部分 oracle 分数增益。

这也说明 Runtime 不应在第一次拆分后持续追求更多节点。剩余 failure 最终集中在同一个 command
diff contract，继续创建 durable workers不会增加独立责任或新信息。

## 与 Native Ultra 的区别

Native Ultra 使用 shared workspace、54 个 ephemeral subagents 和高频 parent/child 消息。它适合
局部发现快速传播，但会产生移动目标、overlapping ownership 和反复重建 child context。

Arm 2 使用三个长期责任、隔离 worktree 和 frozen patch：

- worker 之间不自由聊天；
- child 完成后通过 artifact/receipt 交接；
- primary 是唯一 integration owner；
- attribution 和恢复状态进入 DB；
- 每个 worker 都是普通 `max` profile，没有依赖 ultra 内部 orchestra。

观测对比：

| 指标 | Native Ultra | Runtime Arm 2 |
| --- | ---: | ---: |
| Best F2P / P2P | `63/68`, `242/242` | `63/68`, `242/242` |
| Wall time | `21,994s` | `14,830s` |
| Implementation input tokens | `245,410,202` | `101,444,575` |
| Cached input ratio | `0.965349` | `0.962185` |
| Child agents/nodes | `54` ephemeral | `3` durable |
| Evaluator rounds | `12` | `7` |
| Context compactions | `7` | `6`，均发生于 primary |

Arm 2 在本次运行中更省时、累计输入更少，且最终质量没有下降。但不能据此宣称 Runtime 普遍优于
Native Ultra：两次运行不是同时冻结同一代码版本的统计重复实验，且 Native run 有历史 artifact
污染，Arm 2 运行中也修复了多个 Runtime bug。

## 与 Phase 4G8 普通 Runtime 的区别

Phase 4G8 Large 的 primary peak 为 `58/68`，迟到的 strategy expansion 最终回退到 `55/68`。根因
是 primary 直到 terminal plateau 才请求结构升级，新 strategy 直接在当前 candidate 上广泛改动，
没有隔离贡献和 best-revision protection。

Phase 4G10 的改进不是“节点更多”，而是：

- 在广泛实现前增加 evidence-backed structure assessment；
- child 从固定 base revision 建立隔离 worktree；
- contribution 先冻结，再由 primary 显式接受、修改或拒绝；
- primary session 不被 strategy worker 替换；
- evaluator 反馈继续回到同一 integration owner。

这避免了 Phase 4G8 的“fresh strategy 覆盖 primary 最佳状态”路径。

## Runtime 暴露的问题

本 run 不是一次无故障 green run。执行中发现并修复：

1. child worktree ownership 未被 worker lane 正确采用；
2. primary resume 丢失最新 contribution context；
3. contribution attribution 被错误要求在每轮重新触碰所有 child files；
4. attribution lineage 在 resume 后未恢复；
5. 大型 receipt 被上下文预算截断；
6. receipt recovery 曾创建无效 strategy branch；
7. 同一 materialization 在 adapter recovery 后写入重复 progress ledger fact；
8. operator-stop resume 报告丢失历史 daemon-start boundary。

最终 DB consistency 为 `0 violation / 0 warning`，重复 terminal 和 ledger fact 都为 `0`。这证明修复后
可以恢复并收敛，但不能把结果描述为 Runtime 在初始版本上一次无故障通过。

## 最后 5 项为什么没有解决

第 6、7 次 evaluator 都是同一组 command diff tests。反馈只有 test ID 和“未提取到 bounded
failure detail”，没有 expected/actual、traceback 或 assertion。Primary 已尝试 revision defaults、
delegation seam、non-mutating projection 和 xdist 隔离，本地 focused tests 均通过，official 结果
仍完全不变。

这不是 durable decomposition 的失败边界，而是可观测 oracle 信息不足后的 contract plateau。此时
继续拆节点只会让更多 worker 共享同一个信息盲区。Operator stop 因此要求 workspace patch SHA-256
必须与最后 evaluator candidate 完全一致，再将 run 归类为 task-failed。

## 对生产 Runtime 的含义

可保留的策略是：

```text
一个 primary 先完成有限结构评估
  -> 有真实低耦合责任时创建 2-3 个 durable children
  -> child 用隔离 workspace 交付 frozen contribution
  -> 原 primary 负责集成与后续局部修复
```

不应推广的策略是：

- 大任务自动多节点；
- 为角色或阶段拆节点；
- residual bug 每轮创建新 worker；
- 没有外部 oracle 时默认创建 evaluator；
- 用节点数量证明 orchestra 价值。

本次最强结论不是“Runtime 分数高于某基线”，而是：Hermes 已能让少量 durable workers 在真实任务
中承担独立责任，并在不牺牲最终观测质量的情况下完成隔离并行、可恢复集成和贡献归因。
