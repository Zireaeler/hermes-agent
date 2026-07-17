# Phase 4G9 Arm 1：Native Orchestra 架构结论

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
├── plots_diff              plots、diff 与 CLI 行为
├── tree_stream             tree streaming 与 pulling
├── stage_run               stage、run cache 与 dry-run
├── integration_audit       跨领域集成审查
├── unit_runner             广泛 unit-test 验证
├── compat_edges            兼容性与 target normalization
│   └── targets_scan        嵌套 target API 扫描
└── pyupgrade_audit         Python 3.6 migration 审查
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

## 运行中实际出现的协作闭环

本次运行的价值不只是“开了 8 个 subagent”。可观察事件显示 native orchestra
形成了一个动态执行闭环：

1. **开局快速形成第一批实现分工。** Parent 启动 27 秒后创建 `plots_diff`，随后在
   12 秒内依次创建 `tree_stream` 和 `stage_run`，填满包含 parent 在内的 4-thread
   上限。这三个 agent 分别覆盖相对独立但仍有交叉的实现区域，不是 harness 预设的
   planner/coder/tester 角色。
2. **完成一个 slot 后立即改变工作类型。** 第一批实现线程陆续释放 slot 后，parent
   没有继续机械拆实现任务，而是先后创建 `integration_audit`、`unit_runner` 和
   `compat_edges`，将并发资源从实现转向全局测试、集成审查和兼容性边界检查。
3. **存在 agent-to-agent 协作，不只是 parent 汇总。** 49 次 `send_message` 中，除
   parent 与各 agent 的通信外，`plots_diff` 向 `stage_run` 发送 3 次消息，
   `tree_stream` 向 `stage_run` 发送 1 次消息。Terminal summaries 也分别记录了 plot
   marker 与 stage serialization 的并行集成，以及 `collections.abc` 修改的协调。
   Collaboration message 正文被 Codex 加密，因此本报告不推断具体内容，但通信边、
   时间和双方最终说明足以证明跨责任协调确实发生。
4. **局部 agent 可以自行继续分解。** `compat_edges` 在检查 public target API 时创建
   depth-2 的 `targets_scan`，由后者扫描四个 plural-target fan-in，再把结果返回给
   `compat_edges`。Parent 不需要预先知道这个局部扫描边界。
5. **已有 thread 被继续使用，而不是一次性报告后丢弃。** Parent 共发出 6 次
   `followup_task`：对 `stage_run` 的一次调用因 thread limit 失败，其余 5 次由
   `unit_runner`、`integration_audit`、`compat_edges` 和 `pyupgrade_audit` 接收；
   `compat_edges` 在后段还再次接收 follow-up。这保留了 agent 已建立的局部代码上下文，
   避免重新扫描同一领域。
6. **Parent 始终承担集成责任。** Parent 没有退化成只等待报告的 scheduler。各 agent
   terminal summaries 显示，parent 继续整合 `remote modify --unset`、无 remote 的
   `RepoTree` fallback 和 update completion coverage，并在共享 workspace 上完成最终
   测试与收口。

这套闭环最值得借鉴的地方是：局部发现可以在秒级或分钟级改变任务分配，并直接回流给
仍保有上下文的 agent。它不需要把每次发现序列化成 durable receipt，再等待一次全局
Decision Provider round。

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

## 与当前 Runtime Kernel 的直接比较

| 维度 | Native ultra orchestra | 当前 Runtime Kernel |
| --- | --- | --- |
| 局部协调 | Parent、subagent 和嵌套 subagent 可直接通信，并立即看到共享 workspace 变化 | Durable workers 默认不通信，主要通过 terminal receipt、DB evidence 和下一轮 Decision Provider 交接 |
| 动态分工 | 运行中随时 spawn、follow-up、复用 slot；局部 agent 也能继续创建 ephemeral subagent | 只有 Decision Provider 能创建 durable node；primary-first 与 terminal-only `structure_request` 使扩图较晚 |
| 上下文复用 | Fork 可继承已有 turns，follow-up 继续使用同一局部 thread | 同一 node 内可以 resume，但新 node 使用独立 session，需要从显式 context 重新建立理解 |
| 集成速度 | 共享 workspace 省去 patch handoff，parent 可持续整合 | 隔离 candidate/worktree 需要显式 promotion、merge 和冲突处理 |
| 隔离与归因 | 并发写入同一 workspace，缺少稳定 ownership 和独立 candidate lineage | Node、attempt、receipt、revision 和 evidence 可以持久化、审计和恢复 |
| 独立验证 | Audit agent 继承同一模型、代码状态和部分假设，不能形成强独立证据 | 可以对固定 revision 使用独立 session、权限和 evaluator provenance |
| 故障恢复 | 主要依赖 Codex session 与共享文件；没有 DB reducer、lease 或 materialization history | 已验证 daemon/worker crash、lease takeover、checkpoint 和幂等 receipt ingest |
| 完成判定 | Parent 根据可见测试自判完成，本次明显高估质量 | Goal contract、ledger 和独立 evidence 可以阻止 worker 自报成功直接完成 job |

就“协作本身”而言，native ultra orchestra 明显强于当前 Kernel 的 durable worker
交接。Phase 4G8 Kernel Large 中，primary worker 基本持续到 terminal，随后才通过
`structure_request` 创建 fresh strategy worker；后者只能接收显式任务上下文和当前
workspace 事实，不能与 primary 围绕局部发现实时往返。最终 primary 峰值为 `58/68`，
expanded strategy 为 `55 -> 56 -> 55/68`。这至少说明当前 Kernel 的扩图没有复现
本次 native 运行中的高频协调和上下文复用优势。

但 native 的共享执行也暴露了本次运行中真实发生的问题：

- `unit_runner` 报告测试定义在 collection 期间变化，说明并发共享 workspace 会让测试
  观察到移动目标；
- `integration_audit` 最终声称没有发现遗漏，`pyupgrade_audit` 和 `compat_edges` 也认为
  没有 actionable finding，但 official evaluator 仍有 61 个 FAIL_TO_PASS case 失败；
- 多个 audit agent 与实现者共享同一模型族、实时 workspace 和局部假设，增加了覆盖面，
  却没有形成真正独立的反证；
- 共享 workspace 没有保存每个并行责任的候选 revision，无法判断某个 agent 的修改
  单独提高还是降低了质量；
- parent 仍是最终集成瓶颈。通信越频繁，parent 越需要判断哪些中间结论可信，而本次
  local green tests 没有提供足够 completion truth。

因此不能从 Arm 1 得出“可通信 subagent 系统应取代 Runtime Kernel”。更准确的结论是：

> Native orchestra 擅长一个 coherent responsibility 内的高频搜索、实现、测试和局部
> 协调；Runtime Kernel 擅长跨进程持久化、权限边界、候选隔离、独立证据、恢复和最终
> 完成判定。两者解决的不是同一层问题。

## 对生产架构和 Arm 2 的不同启示

生产系统不应让 Runtime Kernel 模仿 native orchestra 的 49 次局部消息，把每个交流都
升级成 durable event 和 Decision Provider round。更合理的两层结构是：

```text
Runtime Kernel
  └── durable node：一个可验收、可恢复、权限一致的完整责任
        └── native parent/subagents：node 内部的局部规划、并行搜索、实现、测试和通信
```

只有独立验证、capability/human boundary、隔离 worktree、跨长期恢复或真正低耦合的
durable parallelism 才升级成多个 Runtime nodes。这样 Runtime 不承担局部通信成本，
同时保留 native orchestra 缺少的 durable truth 和 policy boundary。

Arm 2 对照实验则有不同目的。为了单独测量“系统级 durable orchestra”是否有价值，
Arm 2 的 Runtime workers 应限制内部 subagent 使用，否则无法区分质量来自 Kernel
扩图还是 native inner orchestra。但这个限制只是实验控制变量，不应自动成为生产
worker policy。

Arm 2 若要公平代表 Runtime-level orchestra，至少需要在 primary 完成初步 repository
审查后提供一次受控 early structure assessment，允许对明确不重叠的责任建立隔离
worktrees，并指定 integration owner；同时必须保存每个 candidate revision 和 best-known
revision。否则 Arm 2 仍会重演 Phase 4G8 的“一个 primary 埋头执行到 terminal，随后 fresh
worker 接手残局”，实际测到的只是迟到 handoff，而不是系统级 orchestra。

未来 Arm 2 的核心问题应是：

> 在相同 one-shot、无 evaluator feedback 条件下，durable system-level workers 能否在
> 不具备 native 高频通信优势的情况下，依靠更好的责任边界、隔离 candidate、阶段证据
> 和集成控制，至少保持 Arm 1 的最终质量，同时提供可恢复和可审计的执行过程。

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
