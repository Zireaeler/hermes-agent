# Hermes Kanban Runtime Kernel Phase 4G9

# Native Orchestra 对照实验

> 本文记录历史 one-shot Arm 1 协议和 preliminary run。它不再作为 native ultra
> orchestra 的完整 capability baseline。正式 iterative Arm 1 及未来 Arm 2 的公平
> 对照规则见 [Phase 4G9 Iterative Native Ultra Arm 1](kanban-runtime-kernel-phase4g9-iterative.md)。

## 1. 目的

Phase 4G8 已证明 Runtime Kernel 能够在真实长周期 SWE-EVO 任务中保持执行连续性、
从进程故障中恢复，并维持可信的完成证据。但它尚未证明 durable 系统级编排能够提高
最终实现质量。DVC Large 的 primary worker 最高达到 `58/68` FAIL_TO_PASS，后续
durable strategy worker 仅达到 `55 -> 56 -> 55/68`。

Phase 4G9 定义以下受控对照：

```text
Arm 1：一个 native Codex parent，使用 ephemeral internal subagents

Arm 2：Hermes Runtime Kernel，使用 durable 系统级 workers
```

实验要回答的问题是：Runtime 级编排能否达到 native parent/subagent 编排的最终质量。
Runtime 可以更慢，但不能仅仅因为工作跨越 durable worker 边界就得到更差的 candidate。

本文冻结对照实验协议和 Arm 1 执行契约，不设计或实现 Arm 2。

---

## 2. Benchmark

冻结任务是已经 qualification 的 SWE-EVO DVC 演进实例：

```text
instance: iterative__dvc_1.0.0a1_1.0.0a2
base commit: fc42ca721c25bdd24875c999e37fb4f589ecd63c
dataset revision: 9b83d5af943ba7a17567336f5b18239f73960219
official image: xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760
worker-visible SRS items: 34
FAIL_TO_PASS: 68
PASS_TO_PASS: 242
```

冻结的 oracle qualification 结果：

| Revision | FAIL_TO_PASS | PASS_TO_PASS |
|---|---:|---:|
| base | 0/68 | 242/242 |
| gold | 68/68 | 242/242 |

Gold 只用于 qualification evaluator，不向任一对照 Arm 暴露。

---

## 3. 冻结的 Arm 1 契约

Arm 1 是一次 standalone native Codex 执行：

```text
clean base workspace
        |
        v
一个 native Codex parent
        |
        +-- 可选 ephemeral internal subagents
        |
        v
terminal candidate revision
        |
        v
一次固定 official evaluator
```

Arm 1 必须使用：

- Codex CLI `0.144.4`；
- 模型 `gpt-5.6-sol`；
- `model_reasoning_effort = "ultra"`；
- 启用 native MultiAgentV2；
- 同时 active 的 Codex threads 最多 4 个，包括 parent；
- 全新的隔离 `CODEX_HOME`、thread history 和 workspace；
- 已配置模型源的 WebSocket transport 参数；
- unrestricted workspace execution 与非交互式 approval handling；
- 一个对集成和 terminal result 负责的 parent。

在当前 Codex build 中，`ultra` 有两项相关语义：

1. 模型请求使用 `max` reasoning effort；
2. client 选择主动 native multi-agent delegation 指令。

因此，`ultra` 是 Arm 1 的 native orchestra profile，不是高于 `max` 的模型推理档位。
Runner 不得用手写近似 prompt 替代该 profile。

Subagents 是 ephemeral Codex execution threads。它们可以检查、实现、测试，并通过
native collaboration tools 与 parent 通信；它们不会成为 Hermes execution nodes，
也不使用 Hermes Runtime state。

---

## 4. 执行 Prompt 边界

Parent 接收：

- worker 可见的完整 34 项 SRS；
- 精确的 base workspace；
- 运行项目测试所需的可信 worker environment setup；
- 对理解、规划、实现、集成、测试、调试和最终验证的完整责任；
- 在有助于速度或质量时主动使用 native subagents 的明确权限；
- 持续执行到 terminal candidate 或真实 blocker 的要求。

Prompt 不得预设 planner/coder/tester topology。Subagent 数量、角色、任务分配、通信和
并发是 native orchestra 的输出，不是 harness 的预设决策。

Parent 与所有 subagents 禁止读取或接收：

- gold patch 或 upstream target implementation；
- protected test patch 内容或 protected evaluator files；
- Phase 4G8 历史 candidate patches 或 worker transcripts；
- Phase 4G8 evaluator 分数或 diagnostics；
- Hermes memory、Decision Session、checkpoint、graph、ledger 或 provider guidance；
- 执行期间的任何 official evaluator result。

允许使用项目可见测试和 agents 自己编写的测试，但不允许接触 hidden oracle。

---

## 5. 隔离与完整性

Arm 1 使用相互独立的目录：

```text
phase4g9/<run-id>/
  codex-home/
  workspace/
  protected/
  worker-events/
  evaluator-runs/
  reports/
```

执行前必须检查：

- workspace `HEAD` 等于冻结 base commit，且工作树 clean；
- materialization 后 workspace 不含 remote；
- `CODEX_HOME` 不含复制的 sessions、memories、skills、plugins 或无关项目 trust entries；
- 只复制选定 provider configuration 和 credential；
- Codex execution identity 对 protected paths 既不可读也不可写；
- 不挂载 Phase 4G8 历史 run，也不把它加入 workspace；
- source `~/.codex/config.toml` 与 `auth.json` 的 hash 保持不变。

Evaluator 只能在 parent 终止且 candidate revision/patch hash 冻结后运行。Evaluator 针对
固定 candidate 执行，不能修改 worker workspace。

---

## 6. Evaluator 规则

Official evaluator 只用于 benchmark 测量，不是 Arm 1 worker、verifier node 或 feedback
source。

Harness 必须强制：

```text
evaluator invocations before terminal candidate = 0
evaluator invocations after terminal candidate  = 1
evaluator feedback turns sent to Codex           = 0
```

唯一一次 evaluator result 记录：

- resolved status；
- FAIL_TO_PASS passed/total；
- PASS_TO_PASS passed/total 与 regression count；
- candidate patch hash 与 bytes；
- fixed target revision；
- evaluator image 与 dataset revision；
- evaluator wall time 与 infrastructure status。

不允许 rerun、修改模型后重试、best-of-N selection 或 evaluator 后继续修复。基础设施失败
可以使 run invalid，但不能静默改为评估另一个 candidate。

---

## 7. 资源上限

Arm 1 必须获得足够长的时间来代表一次认真执行，同时保留有限的安全边界：

- parent wall-time ceiling：6 小时；
- active threads 最多 4 个，包括 parent；
- 最多一个 root Codex execution；
- official evaluator 最多调用一次；
- 不人工触发 daemon restart、worker kill、lease expiry 或 compaction；
- 到达上限时，只终止本 run 拥有的 process group。

触发 wall-time ceiling 时产生 terminal resource-limit candidate，不允许隐藏 continuation
或第二次尝试。

---

## 8. 必需证据

Arm 1 archive 必须包含：

- 冻结 protocol version 和 runner configuration；
- credential 已脱敏的 source/config integrity hashes；
- parent thread ID；
- child thread IDs、native task names、prompts、statuses 和 timing；
- spawn、message、follow-up、wait 和 close events；
- 可观察范围内的 peak 与 time-weighted concurrency；
- parent command/test activity 和 changed-file summary；
- terminal parent message 与 exit status；
- 精确 candidate patch 和 candidate revision/hash；
- 一次 official evaluator result；
- wall time；
- input、cached input、output 和 reasoning output tokens；
- 可观察范围内的 model call/turn count；
- cache-hit ratio；
- process cleanup result；
- 说明 parent 如何分配工作、集成结果、测试实现以及 unresolved 原因的可读执行记录。

Archive 可以保留审计 orchestra 所需的脱敏 native Codex JSONL events。不得发布 credential、
hidden tests 或 gold content。原始 worker/model 输出作为执行证据可以保留。

---

## 9. 冻结的对照门禁

Arm 1 是单一 baseline candidate。未来 Arm 2 必须使用相同 task、base commit、dataset
revision、official image、SRS、model family 和 one-shot evaluator rule。

质量按以下顺序比较：

1. 如果 Arm 1 resolved，Arm 2 也必须 resolved；
2. Arm 2 不得产生比 Arm 1 更多的 PASS_TO_PASS regressions；
3. 如果两者都 unresolved，Arm 2 的 FAIL_TO_PASS passed count 必须大于或等于 Arm 1；
4. 任一 Arm 均不得使用 best-of-N、evaluator-guided retries、gold knowledge 或历史 candidate。

Wall time、token cost、cached input、model calls、concurrency、handoffs 和 changed files
作为次要指标报告。Runtime 可以更慢，主要门禁是最终任务质量不劣于 baseline。

单一样本不能证明模型统计优势。该实验只回答一个更窄的架构问题：在这个已 qualification
的 large task 上，durable Runtime orchestration 是否保持或提高了 native
parent/subagent orchestration 的质量。

---

## 10. Arm 1 验收标准

Arm 1 在满足以下条件时完成：

- 冻结 config 与 isolation preflight 通过；
- 一个 standalone native Codex parent 在 clean base workspace 启动；
- 在 harness 未预设 topology 的情况下捕获 native subagent behavior；
- 执行以 terminal candidate 或有记录的 resource/blocker boundary 结束；
- evaluator access 前 candidate 已冻结；
- official evaluator 恰好运行一次；
- evaluator feedback 未到达 parent 或任一 subagent；
- 必需的 process、orchestration、token、cache、candidate 和质量证据已归档；
- report 生成后清理可重建 image、toolchain、workspace 和 transient caches；
- protocol、runner、tests 和 Arm 1 report 已 commit 并 push。

Arm 1 完成不要求 `68/68`。只要执行与测量协议保持完整，unresolved 结果仍是有效 baseline。

---

## 11. 延后的 Arm 2

Arm 2 需要另行设计 early structure assessment、durable worker write isolation、
integration ownership、best-revision preservation，以及抑制 Runtime workers 内部的
nested subagent orchestration。

这些改动明确不属于 Arm 1 Goal，不得从本文推断或直接实现。

---

## 12. 冻结的 Arm 1 Baseline

Arm 1 已于 2026-07-17 完成。不可变对照 baseline 为：

```text
resolved: false
FAIL_TO_PASS: 7/68
PASS_TO_PASS: 242/242
parent wall time: 4667.077 seconds
implementation subagents: 8
peak implementation concurrency: 4 including parent
time-weighted average implementation concurrency: 3.270567
official evaluator invocations: 1
evaluator feedback turns: 0
```

运行前冻结的 protocol 是 commit `0059774` 中的 Section 1-11，SHA-256 为：

```text
05578a73404caa1550bceb5a97ba89d3dfc7b3036e5de6939288a2269f792b38
```

本结果章节只在 terminal candidate 和 one-shot evaluation 完成后追加。

精确 candidate patch SHA-256：

```text
494c5e7bb04a8a33e85de387e7d541f7197eacfc2b57a73b4565641278636931
```

Native Codex rollout 会加密保存 collaboration message bodies。因此 archive 记录 task
names、sender/target、timing、tool result 和 ciphertext hashes，不伪造不可观察的 plaintext
prompts。脱敏后的 outer event stream 没有暴露 spawn calls；这不影响 child-session identity
或 concurrency evidence。

Terminal 后 patch collector 最初被生成的非 UTF-8 pytest artifacts 阻断。恢复过程只删除
workspace 顶层 `.pytest-*` 目录，没有恢复 Codex，并且 evaluator 恰好只调用一次。
Collector failure 前没有持久化精确 model-proxy request counters；该事实只属于可选运维
遥测缺口，不影响 worker 行为或 Runtime Kernel 分析。

归档报告和架构结论位于：

```text
docs/validation/phase4g9/
  iterative__dvc_1.0.0a1_1.0.0a2/
    phase4g9-arm1-native-20260717/
```
