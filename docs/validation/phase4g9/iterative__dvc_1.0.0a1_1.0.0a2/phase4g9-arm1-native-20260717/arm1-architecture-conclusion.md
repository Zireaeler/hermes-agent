# Phase 4G9 Arm 1: Native Orchestra Architecture Conclusion

## 结论

这次 Arm 1 是有效的 native Codex orchestra 基线，不是一个单 agent
伪装成多 agent 的运行。一个 standalone Codex parent 在没有 Hermes Runtime、
Decision Provider 或运行中 evaluator feedback 的情况下，主动创建并协调了 8 个
实现或审计 subagent，并在 77 分 47 秒后自然结束。

但它没有完成 DVC Large 任务。唯一一次 official evaluator 得分为：

- FAIL_TO_PASS：`7/68`；
- PASS_TO_PASS：`242/242`；
- resolved：`false`。

因此 Arm 1 同时给出两个不同结论：

1. native parent/subagent orchestration 的并行、通信、嵌套委派和持续集成机制确实
   被积极使用；
2. 在没有隐藏标准反馈的 one-shot 条件下，这次 native orchestra 的最终实现质量
   远低于任务要求。

这不是 Arm 2 的结果，也不能用来证明 Hermes Runtime 比 native orchestra 强。
此前 Phase 4G8 Kernel Large 的 `58/68` primary 峰值来自多轮 official evaluator
反馈，比较条件不同。未来 Arm 2 必须遵守同样的 one-shot evaluator 边界。

## 实际编排形态

```text
Codex parent / integrator
├── plots_diff              plots, diff, CLI behavior
├── tree_stream             tree streaming and pulling
├── stage_run               stage, run cache, dry-run
├── integration_audit       cross-area integration audit
├── unit_runner             broad unit-test validation
├── compat_edges            compatibility and target normalization
│   └── targets_scan        nested target API scan
└── pyupgrade_audit         Python 3.6 migration audit
```

旁路还有 2 个 guardian approval session。它们只审查危险操作，不属于实现
orchestra，也不计入 8 个 worker subagent。

可观察到的编排行为：

- 最大实现并发为 4，包含 parent；
- 时间加权平均实现并发为 `3.270567`；
- parent 和 subagents 共记录 21 个执行 turn；
- `spawn_agent=9`，实际形成 8 个 subagent session；
- `send_message=49`；
- `followup_task=6`；
- `wait_agent=20`；
- `list_agents=25`；
- 2 次调用因 thread limit 被拒绝，parent 在 slot 可用后继续复用或重试；
- parent 发生 2 次 context compaction，全部实现线程合计 6 次；
- 一个 depth-1 subagent 创建了 depth-2 `targets_scan`。

所有 subagent 共享 parent workspace。它们不是隔离 worktree，也没有 durable
runtime state。它们可以高频通信、立即看到彼此修改，由 parent 持续集成。这正是
本实验要测量的 native in-process orchestra 形态。

## 执行与质量之间的落差

parent 在 terminal message 中报告：

- unit：`432 passed, 9 skipped`；
- broad functional：`774 passed, 56 skipped`；
- affected functional：`344 passed`；
- Flake8：0 findings；
- 认为 34 项 SRS 已完整实现。

这些是 worker 在看不到 official oracle 时的自我验证事实，不是最终 benchmark
真相。official evaluator 后来显示 61 个 FAIL_TO_PASS case 仍失败，其中 53 个是
unit case，8 个是 functional case，主要分布在：

- plots/diff 数据与 CLI 契约；
- run/repro/update 参数兼容；
- stage checksum、wdir 和 dry-run 行为；
- filesystem/path utility 语义。

这说明失败不能简单归因于“没有测试”或“没有并行”。native orchestra 运行了大量
可见测试，也投入了多个独立 audit agent。更直接的问题是：34 项 release SRS 很宽，
现有可见测试不能完整表达目标 release 的精确兼容语义；parent 将本地绿色测试和
广覆盖修改误判成了完整完成。

candidate 修改 129 个文件、134,809 bytes。它保留了全部 `242/242` PASS_TO_PASS，
说明没有普遍破坏旧行为；但只命中 7 个新要求测试，说明广泛修改没有准确收敛到
目标版本行为。多 agent 覆盖面和最终需求命中率不是同一个指标。

## 对系统级 Orchestra 的基线含义

Arm 1 证明了 native orchestra 的明显优势：

- parent 可以在同一个共享上下文中动态分工；
- subagents 能高频交换局部发现；
- 完成的 slot 可以立即复用；
- parent 能直接集成共享 workspace 中的结果；
- 内部 compaction 没有终止长时间执行。

它也暴露了 native orchestra 本身没有解决的问题：

- 没有外部 completion truth 时，parent 会高估完成度；
- 多个 agent 可以共同扩大错误实现，而不只是扩大正确覆盖；
- shared workspace 提高通信效率，但不保存可比较的独立 candidate revision；
- 没有 durable evidence reducer 来区分局部测试通过与 goal 真正满足。

未来 Arm 2 的合理目标不是在时间或通信效率上胜过 native orchestra，而是验证：

> 在同样的 one-shot、无 evaluator feedback 条件下，durable system-level workers
> 是否能通过更好的责任边界、隔离、阶段证据和集成控制，至少保持 Arm 1 的最终
> 质量，同时提供 native orchestra 不具备的恢复与审计能力。

## 冻结质量门禁

Arm 1 已将未来 Arm 2 的最低质量线冻结为：

1. 使用相同 base、SRS、模型族、official image 和一次 evaluator；
2. 不读取 gold、protected tests、历史 candidate 或 Arm 1 实现；
3. PASS_TO_PASS 回归不得多于 Arm 1，即必须保持 `242/242`；
4. 若 Arm 2 未 resolved，FAIL_TO_PASS 必须至少达到 `7/68`；
5. 若 Arm 1 的一次样本后来被重跑，也不能替换这次 baseline 或做 best-of-N。

`7/68` 是很低的非劣门槛。它只保证实验公平，不代表 Arm 2 达到工程可用质量。
Arm 2 仍应单独报告 absolute capability，而不能只以“没有输给 Arm 1”作为成功。

## 观测边界

- official evaluator 恰好运行一次，且在 candidate freeze 后运行；
- evaluator 结果没有回流给 Codex；
- candidate SHA-256 为
  `494c5e7bb04a8a33e85de387e7d541f7197eacfc2b57a73b4565641278636931`；
- post-terminal collector 曾因非 UTF-8 pytest artifact 失败；修复仅删除 workspace
  顶层 `.pytest-*` 生成目录，未恢复 Codex，未重跑 evaluator；
- collector 失败使精确 model-proxy request count 未能持久化，这是观测缺口；
- token 数据来自各 rollout 最终累计计数之和，不应解释为去重后的独立上下文大小；
- native collaboration message 正文由 Codex 加密保存。归档保留 task name、事件、
  时间、状态和 ciphertext hash，不发布或推断隐藏模型推理。

本报告不使用 gold patch、protected evaluator source 或 private reasoning。
