# Phase 4G16 Natural Orchestra Calibration 总结

> 本文记录 deferred decomposition 实现前的三次校准。后续扩大后的 Deferred Medium 已自然产生
> `defer_until_milestone`，但暴露 checkpoint repair 缺陷；权威补充见
> [Deferred Medium 首轮复盘](deferred-medium-checkpoint-repair-analysis.md)。不得继续引用本文旧 Case C
> 结论来声称最新 fixture 没有自然拆分价值。

## 结论

Phase 4G16 已完成自然生产路径、确定性回归、三类 paired run、learning absorption、归档与清理 gate。
但本阶段不能宣称已经证明系统级 orchestra 对自然任务具有正价值。

三个 treatment 的最终质量均不低于 coherent single worker，Runtime consistency 均通过。真实 worker
在三个任务中都选择了 `continue_single_node`，因此没有出现 natural candidate、dynamic child、live
directive 或 contribution handoff。Case A/B 证明 Runtime 能克制无依据扩图；Case C 只证明当前正向
fixture 仍低于 durable decomposition 阈值。

## 冻结配置

- 模型：`gpt-5.6-sol`，reasoning effort `max`，worker 内 multi-agent 关闭。
- Baseline：一个 coherent Codex worker，禁止 subagent。
- Treatment：真实 Kanban task/run/receipt、真实 Decision Provider、真实 Codex worker session。
- Prompt 不包含 candidate key、node key、文件路径、graph op 或预期拆分答案。
- 没有独立 evaluator，也没有多轮 evaluator 修复循环；冻结 unittest 仅作最终质量 oracle。
- 每个 run 先归档并验证 manifest，再吸收 learning，最后清理 source 中可重建和已归档的重复数据。

## 汇总

| Case | Baseline | Treatment | Runtime node | Assessment | Candidate/child | 结论 |
|---|---:|---:|---:|---:|---:|---|
| Coherent Negative | 7 tests，257.542s | 5 tests，333.535s | 1 | 1 | 0/0 | 正确保持单节点 |
| Shared Contract Medium | 5 tests，242.593s | 4 tests，372.130s | 1 | 1 | 0/0 | 正确保持共享合同闭环 |
| Durable Boundary Medium | 17 tests，510.129s | 15 tests，541.473s | 1 | 1 | 0/0 | 质量通过，但正向校准失败 |

测试数包含 worker 自己补充的测试，因此不能用数量直接比较两臂质量。这里的强断言是：两臂的冻结
oracle 都通过，treatment 没有低于 baseline。Wall time 也受模型随机性影响，只能作为本次观察值，不能
直接归因为 Runtime 开销。

## Case A：Coherent Negative

目标是在一个两行同步执行器中实现重试策略。Worker 只读检查后确认：实现与唯一测试模块共享同一 API，
不存在 2 至 3 个不重叠写域。Runtime 接受 `continue_single_node`，同一 session resume 后完成实现。

- 节点：`implement-retry-policy`，共 1 个。
- Materialization：2 次，分别是结构评估和同 session 实现。
- Decision Provider：2 次，初始建图和评估后的 `continue_node`。
- Coordination checkpoint/action/directive：0/0/0。
- Learning：`no_regression_signal`，已吸收。

这说明 primary-first 没有把普通小任务机械拆成 analysis、implementation 和 testing 节点。

## Case B：Shared Contract Medium

目标同时修改 normalize、formatting 和 store。Worker 发现三个模块都消费同一个尚待确定的 record
contract，唯一测试文件也会成为并行写冲突点，因此拆分会增加合同同步和集成成本。

- 节点：`implement-shared-record-contract`，共 1 个。
- Materialization：2 次，同一 Codex thread resume 1 次。
- Decision Provider：2 次；dynamic node 为 0。
- Coordination checkpoint/action/directive：0/0/0。
- Learning：`no_regression_signal`，已吸收。

这说明“涉及多个模块”本身不是 durable node 边界。

## Case C：Durable Boundary Medium

第二版任务包含 core schema v2、legacy v1 transform 和 audit JSONL consumer。Worker 明确识别了三个
行为面，也确认 legacy/audit 各有独立测试，但仍给出以下 repository evidence：

- 已检查文件合计约 150 行，生产实现很小；
- legacy 与 audit 都依赖尚未冻结的 v2 事件形状；
- 最终还需要统一补充跨三条路径的端到端测试；
- 立即创建持久 child 的合同漂移风险和交接成本高于并行收益。

因此它自然选择 `continue_single_node`。Runtime 没有伪造 candidate，随后用同一 Codex thread resume，
Primary 完成 core、两个扩展和 E2E 测试。Treatment 15/15 通过，job `done`，consistency 0 violations；
baseline 17/17 通过。失败项仅为：

- `natural_candidate_observed=false`；
- `candidate_consumed_by_provider=false`。

该 run 的 learning category 是 `calibration_fixture_gap`，scope 为 `validation_campaign`。它不是
`missed_coordination`，因为没有 sibling、旧 revision artifact 或重做 lineage。

## 能证明什么

1. 自然结构评估、checkpoint 持久化、Provider 决策和同 session resume 的生产链路可运行。
2. 无需 orchestra 时，Runtime 没有制造额外 worker、handoff 或 speculative graph。
3. 三个 treatment 都完成目标且质量不低于 coherent baseline。
4. 每个 run 都形成中文报告、manifest-verified archive 和 absorbed learning receipt。
5. Case C 的失败可以被正确分类为校准任务问题，而不是自动生成错误的 Runtime policy 修复候选。

## 不能证明什么

1. 没有自然 run 经过 candidate -> Provider -> dynamic child -> contribution -> Primary integration。
2. 没有证据表明 Runtime 在新事实出现后改变了另一个仍在执行的责任。
3. 没有证据表明无通信 durable workers 相比 coherent single worker 改善了质量、时间或重做量。
4. 当前 2 次 Provider 调用包含初始建图和 `continue_node`；对 no-op assessment 的第二次调用仍有优化空间。

## 从过程得到的设计问题

当前 early assessment 只有两个选择：

```text
continue_single_node
expand_now
```

Case C 暴露了第三种自然情况：边界已经可见，但共享合同尚未稳定。合理执行可能是：

```text
Primary 只负责建立并验证共享合同
        ↓ milestone artifact / fixed revision
Runtime 重新评估 deferred candidates
        ↓
独立 child 分别实现低耦合 adapter
        ↓
Primary 消费 contribution 并完成集成
```

这不是要求每个任务都拆分，而是允许拆分时机由执行证据决定。现有 binary assessment 无法表达
`defer_until_milestone`，也没有把 milestone artifact 作为 child 的冻结输入。

## 下一步

在继续增加随机 Medium 前，先设计并实现 deferred decomposition：

1. Structure checkpoint 可声明 `defer_until_milestone`、共享合同 artifact 和候选责任，不立即创建 child。
2. Primary 在合同测试通过后提交 milestone checkpoint；Runtime 固定 revision/artifact。
3. 本地 reducer 检查候选前置条件，只有 owner/scope/capability 变化才调用 Provider。
4. Child 从固定合同 revision 派生隔离 worktree，不依赖口头摘要猜接口。
5. Primary 保持 integration owner，贡献沿 Phase 4G14 durable handoff 路径晋升和集成。
6. 先用一个受控小 smoke 验证状态机，再选一个工作量足够的自然 Medium 做 paired validation。

自然 Medium 必须满足：每个候选责任有实质工作量、写域可分、共享接口能在中途冻结、最终仍有明确
integration owner。继续用约 150 行且一个 worker 数分钟即可完成的 fixture，只会重复得到合理的 no-op。

## Evidence

- Case A archive：`phase4g16/coherent-negative-hermes-phase4g16-natural-20260720-214022/coherent-negative`
- Case B archive：`phase4g16/shared-contract-medium-hermes-phase4g16-natural-20260720-215035/shared-contract-medium`
- Case C archive：`phase4g16/durable-boundary-medium-hermes-phase4g16-natural-20260720-222052/durable-boundary-medium`
- Artifact root：`/root/hermes-validation-artifacts`
- Source cleanup：三个 manifest 与 learning gate 通过后分别删除 26,116,777、25,176,080、
  30,335,877 bytes，共 81,628,734 bytes；临时 campaign 仅保留 reports。
- 回归：`562 passed`；focused `23 passed`；Ruff 和 `git diff --check` 通过。

本报告中的运行事实来自各 archive 的 `case-report.json`、`capability-trace.md`、
`orchestration-learning.md`、`orchestration-learning-receipt.json`、manifest 和权威 Kanban DB。
