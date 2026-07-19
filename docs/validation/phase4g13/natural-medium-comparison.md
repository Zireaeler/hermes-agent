# Phase 4G13 自然 Medium 双臂对照与架构结论

## 1. 对照结果

| 指标 | Coherent single worker | Runtime orchestra |
|---|---:|---:|
| Worker node 数 | 1 | 5 |
| Official evaluator 次数 | 1 | 1 |
| F2P | 3/5 | 3/5 |
| P2P | 707/707 | 707/707 |
| Resolved | false | false |
| Wall time | 1171.437s | 3468.978s |
| Worker input tokens | 7,863,864 | 7,299,364 |
| Worker cached input tokens | 7,355,392 | 6,565,888 |
| Worker output tokens | 31,966 | 120,661 |
| Reasoning output tokens | 17,825 | 65,366 |
| Cache ratio | 93.53% | 89.95% |

Runtime wall time 是 single worker 的 `2.96x`，多 2297.541 秒；worker input tokens 少
`7.2%`，但 output tokens 为 `3.78x`，reasoning output 为 `3.67x`。协调 worker 与普通执行
worker 的 token 集合有重叠，因此不能把 `coordination_token_overhead` 再直接加到 aggregate
worker tokens 上。

机器可读对照见 `comparison-report.json`。

## 2. 本次证明了什么

### 2.1 Runtime 已经具备真实动态 orchestra

本 run 不是预先写死三个 child。Primary 先收到完整目标，自行审查 repository，产生结构
checkpoint；Provider 再根据全局 graph 和 evidence 决定扩图。三个 child 随后在不同 worktree
并行执行。

因此，以下能力已经真实实现：

```text
repository evidence
  -> node semantic state change
  -> global Runtime decision
  -> dynamic graph mutation
  -> durable isolated execution
```

这比此前“单 primary 埋头执行到底”的 Runtime 前进了一步。

### 2.2 任务确实适合有限拆分

CLI/traceback、DataFrame 和 Array/docs 的写范围与测试面低耦合；DataFrame 内部五项修改共享
同一 core/test feedback loop，因此保持一个 child 又是正确的。无通信 Runtime child 在这种
subsystem boundary 上并不天然劣于可通信 subagent。

### 2.3 全局恢复有实际作用

两个 child 失败、原 primary 被 dependency 阻塞后，Runtime 根据完整 graph 创建 recovery
responsibility，最终仍产生可评估 candidate。普通一次性脚本在同样 branch failure 下不会自动
形成这一恢复结构。

## 3. 本次没有证明什么

### 3.1 没有证明 orchestra 改善最终质量

两臂得到完全相同的 official 结果和相同两个失败 contract。Runtime 没有提高 F2P，也没有
减少 P2P 回归。

更关键的是，最终 candidate 由 full-workspace recovery worker 重建，而不是 primary 集成三个
child contributions。原 child 工作对最终 patch 没有形成可证明的因果贡献。因此不能把本次
结果解释成“多节点合作与 single worker 打平”。准确说法是：

```text
并行 child 完成工作
  -> 交接协议失败
  -> child 工作被浪费
  -> fallback single worker 重做
  -> 最终与 Arm A 打平
```

### 3.2 没有证明 Runtime 成本可接受

墙钟时间增加 `196.1%`。虽然三个 child 的实现阶段真实并行，但 receipt recovery、三次上下文
重获、两次 validator rejection、一次 300 秒 provider timeout 和最后的完整重做吞掉了并行
收益。

### 3.3 没有证明隐藏 oracle 可以由分工自然推断

两个失败点分别是 warning class 和历史 typo。增加独立 worker 数不会自动发现这类公开 SRS
未表达的精确 contract。若不允许 evaluator feedback，single worker 与 Runtime 都缺少该信息。
这不是继续增加 Runtime node 能解决的问题。

## 4. 对无通信 Runtime 的判断

当前设计不是没有意义，但其价值条件比“任务足够大”更严格。

无通信 durable child 适合：

- write scope 可隔离；
- 输出能冻结为完整 patch/evidence；
- integration owner 能一次消费 contribution；
- 中间发现不需要高频往返；
- crash recovery、权限或审计价值足以覆盖交接成本。

Native parent/subagent 更适合：

- 需要频繁共享局部发现；
- 多个方向会快速互相修正；
- 责任无法形成独立 artifact；
- 任务能在一个 session 内可靠完成。

本 Medium 的拆分边界本身适合 Runtime，但实现失败在 contribution handoff。它说明无通信架构
的关键不是更多 node，而是让每个 node 的终态成为低损耗、机器可验证、可直接集成的事实。

## 5. 当前系统的净结论

| 维度 | 判断 |
|---|---|
| 自然发现 durable gap | 通过 |
| Provider 选择扩图 | 合理 |
| Child scope 隔离 | 通过 |
| Child 实际并行工作 | 通过 |
| 普通进展不触发 Provider | 通过 |
| Terminal contribution 交接 | 失败，已修 prompt/schema |
| Recovery graph 自修复 | 最终通过，但成本过高 |
| 最终任务质量提升 | 未证明 |
| 相比 single worker 的净正价值 | 本 run 未证明 |

Runtime 的正面价值目前主要是可恢复性、状态权威性和可审计结构，不是质量提升。若下一次 clean
run 能让三个 child 直接 terminal、primary 真正消费全部 contribution，并在不重做的情况下维持
或改善质量，系统级 orchestra 才能被认为对这类任务产生正价值。

## 6. 已完成修正

本次 run 后已完成：

1. event-driven worker prompt 恢复 canonical receipt 规则；
2. schema 明确 goal key、contribution artifact ID、directive ID 不得写自由文本；
3. child 明确使用 `succeeded`，不得声明 integrated goal complete；
4. validator recovery profile 升级，单个 recovery node 在存在 nonterminal node 时也必须提供
   evidence-backed decomposition；
5. Runtime arm report 和 comparison 增加 wall time；
6. focused regressions 固定组合 prompt 与 schema 行为。

## 7. 下一验证门槛

不应立即再消耗一次完整 Medium 来重复证明模型能力。下一步先用轻量真实 contribution case
验证：

- child 第一次 terminal receipt 直接被接受；
- `receipt_invalid_count=0`；
- `invalid_resume_count=0`；
- contribution 包含正确 changed files 和 patch hash；
- primary 消费 contribution 后不重新实现 child scope；
- evaluator-stop 不留下假 `running` projection。

这些条件通过后，再做一次自然 Medium clean replay。Clean replay 的成功标准不是必须 resolved，
而是 child 贡献真实进入最终 candidate、无协议性重做，并能量化并行收益与协调开销。
