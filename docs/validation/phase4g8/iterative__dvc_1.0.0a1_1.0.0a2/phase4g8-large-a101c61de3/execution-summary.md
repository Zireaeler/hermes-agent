# Phase 4G8 DVC Large 执行总结

## 一页结论

- Run：`phase4g8-large-a101c61de3`
- Runtime job：`rjob_0534a88c6028`
- 任务：完成 DVC `1.0.0a1 -> 1.0.0a2` 的 33 项跨模块软件演进要求
- Runtime Validation：通过
- End-to-End Capability Validation：未通过
- 分类：`runtime-correct/task-failed`
- Primary worker 峰值：FAIL_TO_PASS `58/68`，PASS_TO_PASS `241/242`
- Expanded strategy worker 最终：FAIL_TO_PASS `55/68`，PASS_TO_PASS `241/242`
- 最终状态：operator 在最后一次 evaluator 后停止，不继续尝试未解决任务

这次运行证明了两件不同的事：

1. Hermes 的长周期执行、同 session 恢复、checkpoint、故障恢复和结构升级确实有用；本 benchmark 还验证了预设 external oracle 的接入。
2. 新增 durable worker 并不天然提高任务质量。本次扩图增加了实现覆盖，但没有保护 primary 已获得的最佳 revision，最终 official 分数下降。

因此，本次结果不能写成“Large 成功完成”，也不能写成“Runtime 没有价值”。准确结论是：

> Runtime correctness 通过，任务未解决；primary 长周期 worker 取得了主要能力增益，evidence-backed graph expansion 发现了更多真实缺口，但当前 candidate 策略缺少 best-revision preservation 和 regression-aware rollback。

## 适用边界：Evaluator 不是默认生产路径

Small、Medium、Large 都是 benchmark task，任务开始前已经存在完整、固定、独立的 official test oracle。因此 evaluator 在这些 run 中有明确语义：衡量 candidate 是否满足已知但对 worker 隔离的标准。

这不代表 Hermes 之后处理普通开发任务时也应默认创建 evaluator。一般任务通常只有目标描述、现有项目测试、worker 自己补充的测试、运行产物和必要的人工验收，并没有一套预先准备好的 hidden test set。此时再创建一个只会重跑 worker 测试或泛化审查的 evaluator，不能提供与 SWE-EVO oracle 等价的独立证据，反而会增加流程和上下文成本。

生产默认路径应是：

```text
Goal / current gap
  -> one coherent primary worker
  -> worker-owned inspection, implementation, testing and debugging
  -> receipt with changed artifacts, commands, evidence and remaining risks
  -> local reducer checks explicit goal contract and required policy gates
  -> complete, blocked, human_required, or evidence-backed structure expansion
```

只有 goal contract 本身提供了独立验收机制，或任务跨越高风险/权限/责任边界时，才启用独立 verification。例如：已有 acceptance suite、协议 conformance checker、部署 smoke、security review、合规审批或 human acceptance。Benchmark evaluator 是其中一个特殊实例，不是默认模板。

## 为什么要单独跑 Large

Small 和 Medium 主要验证单一或较集中的变更。DVC Large 的目标覆盖 CLI、plots、diff、stage、run-cache、tree、remote、serialization、completion scripts 等多个区域，不能通过一次局部补丁合理代表完成。这里使用 evaluator 是因为 SWE-EVO 已提供固定 oracle，不是因为复杂任务天然需要 evaluator。

本次测试刻意不预拆成 analysis、implementation、testing 等多个 node。初始 graph 只创建一个 coherent primary responsibility，由同一 Codex session 负责：

```text
理解 33 项要求
  -> 检查历史代码和当前行为
  -> 设计与实现
  -> 运行本地测试
  -> 消费 official evaluator 反馈
  -> 持续修复
```

只有 primary worker 在真实 evidence 下声明 durable boundary 后，Runtime 才创建新的 strategy node。

## 执行结构

| 项目 | 结果 |
| --- | ---: |
| Decision Provider rounds | 10 |
| Applied graph patches | 5 |
| Rejected graph patches | 5 |
| 有效 durable worker threads | 2 |
| Superseded speculative strategy nodes | 3 |
| Primary session resume | 12 |
| Expanded strategy session resume | 3 |
| Official evaluator receipts | 13 |
| 有效 evaluator attempts | 12 |
| Accepted real checkpoints | 3 |
| 总运行时间 | 15,526 秒，约 4 小时 19 分钟 |

Primary thread：

```text
019f6a91-16c0-70f3-bd74-19698a993b77
```

Expanded strategy thread：

```text
019f6b33-1f94-7943-84fe-1db2107a132f
```

两个 durable node 使用独立 Codex context。新 strategy worker 没有读取 primary 的隐藏推理，只得到 Runtime 提供的 goal、gap、checkpoint、bounded evaluator evidence 和 workspace。

## 完整执行过程

### 1. 环境与 oracle qualification

Harness 使用已 qualification 的 SWE-EVO DVC external oracle。Worker 与 evaluator 使用同一固定依赖环境：

- Python `3.9.19`
- pytest `7.4.4`
- numpy `1.20.0`
- environment fingerprint `666a583a...c26ce0f6`

Worker 无法读取 gold patch 或 protected evaluator source。Evaluator 只返回允许暴露的 test IDs 和 bounded diagnostics。

初始 evaluator 结果：

```text
FAIL_TO_PASS  0/68
PASS_TO_PASS 242/242
```

第一次 evaluator receipt 的 diagnostic extraction 不完整，因此重新执行；它不计入后续有效能力序列。

### 2. Primary worker 建立基础实现

Decision Provider 创建一个 primary implementation node，没有按传统开发阶段拆成多个 worker。Primary 最初只识别到版本变化，official evaluator 随即证明目标远大于单一 version bump。

Worker 消费完整失败集合后，开始覆盖：

- plots API、parser 和输出行为；
- command/repository diff；
- remote default 修改；
- stage name validation；
- update batching；
- dry-run 与 logging；
- revision normalization；
- compatibility、serialization 和 path conversion；
- run/repro、params/metrics 等 release contract。

前四个有效 evaluator 结果为：

```text
0 -> 43 -> 52 -> 56 / 68
```

这说明 evaluator feedback 不是形式化门禁：它实际暴露了本地测试未覆盖的需求，并驱动同一 worker 修改 candidate。

### 3. Primary 同 session 持续修复

Runtime 没有在每次 evaluator 失败后创建新 worker，而是 reopen 同一 implementation responsibility，并恢复同一 Codex thread。Primary 后续进展为：

```text
56 -> 57 -> 57 -> 58 / 68
```

峰值 candidate 达到：

```text
FAIL_TO_PASS 58/68
PASS_TO_PASS 241/242
```

此时仍有 10 个 F2P failure，以及 1 个 P2P regression。Primary 继续针对可见诊断修复，但连续三次 evaluator 返回同一 failure signature：

```text
efsig_6e79adf9db6e5b1836b5e7e6
```

这些剩余 case 主要只提供 `test_id_only`，没有 assertion、expected value 或 traceback；与此同时，可访问的本地对应测试已经通过。

### 4. Primary 发出 structure request

Primary 没有继续无证据猜测，也没有自行创建 runtime node。它返回 terminal blocked receipt，并附带 blocking `structure_request`：

- reason：`independent_verification`
- evidence：连续三次相同 evaluator signature
- requested evidence：代表性 plots、command-diff 和 path-conversion bounded diagnostics
- protected source access：明确为 false

这是真实执行中发现的 durable boundary，不是 Decision Provider 在任务开始前猜测出的拆分理由。

### 5. Runtime 修复并扩展 graph

Structure request 路径暴露了若干 Runtime 实现问题。运行中完成了以下修复：

1. 将 legacy `status: structure_request` / `blocked_independent_verification` receipt 规范化为 canonical blocked receipt；
2. blocking structure request 不再被错误投影为 `candidate_ready`；
3. workspace revision 未变化时，resume 可以重新 ingest 先前 invalid 的 structure receipt；
4. structure request 被接受后，清理此前由 invalid receipt 诱发的 speculative strategy branch；
5. superseded node、task、materialization 和 backend session 被一致清理，避免 orphan ready worker；
6. receipt recovery budget 与 infrastructure recovery budget 分离。

Decision Provider 随后创建新的 durable strategy node。Graph expansion 有 worker receipt、failure signature 和 open gap 作为 evidence，符合 delegation policy。

### 6. Fresh strategy worker 重审完整 SRS

新的 strategy worker 没有只盯住 10 个剩余 test IDs。它重新审计完整 33 项要求，发现 primary candidate 仍缺少或不完整的领域，包括：

- merged YAML outputs；
- Markdown diff；
- plural plots API 和 plot templates；
- RepoTree/DvcTree streaming；
- run-cache 与 uncached files；
- shell completion scripts；
- path normalization；
- 多项 CLI、stage 和 repository compatibility 行为。

本地验证峰值：

```text
unit       428 passed, 9 skipped
functional 735 passed, 47 skipped, 15 environment deselected
```

这说明 fresh context 确实发现了 primary 没有完成的真实工作，并非无效重复执行。

### 7. Expanded strategy 的 evaluator 结果

Expanded strategy 的三轮 official evaluator 为：

| 轮次 | FAIL_TO_PASS | PASS_TO_PASS | 说明 |
| --- | ---: | ---: | --- |
| 1 | 55/68 | 241/242 | 比 primary 峰值下降 3 项 |
| 2 | 56/68 | 241/242 | 修复 1 项，但仍低于峰值 |
| 3 | 55/68 | 241/242 | 再次回退，未 resolved |

最终 failure 包括 plots、command diff、revisions、dry-run 和 path conversion。Strategy worker 扩大了总体代码与测试覆盖，却重新引入了 primary 已经通过的 revision-label 行为。

### 8. Operator 停止运行

最后一次 evaluator 后 Runtime 已 materialize 下一 remediation，但 operator 决定停止，不继续消耗资源。终止不是固定 evaluator attempt budget，也不是磁盘、token 或 wall-time resource exhaustion。

最终 candidate：

- changed paths：66
- patch bytes：94,524
- patch SHA-256：`fa4f39ec8fc86cea6d6fec42737dc6662398b9f755f74c096e76c3ef86d94fdc`
- protected oracle included：false

## Evaluator 总时间线

| Attempt | 执行阶段 | F2P | P2P | Feedback consumed |
| ---: | --- | ---: | ---: | --- |
| 1 | qualification retry | 0/68 | 242/242 | 否，诊断提取不完整 |
| 2 | primary | 0/68 | 242/242 | 否 |
| 3 | primary | 43/68 | 241/242 | 否 |
| 4 | primary | 52/68 | 241/242 | 否 |
| 5 | primary | 56/68 | 241/242 | 是 |
| 6 | primary | 57/68 | 241/242 | 是 |
| 7 | primary | 57/68 | 241/242 | 是 |
| 8 | primary | 58/68 | 241/242 | 是 |
| 9 | primary | 58/68 | 241/242 | 是 |
| 10 | primary | 58/68 | 241/242 | 否，转入 structure request |
| 11 | expanded strategy | 55/68 | 241/242 | 是 |
| 12 | expanded strategy | 56/68 | 241/242 | 是 |
| 13 | expanded strategy | 55/68 | 241/242 | 否，operator 停止 |

## 故障与恢复覆盖

本 run 不是只让一个模型连续运行到结束。它真实覆盖：

- worker SIGKILL；
- 同一 Codex backend session resume；
- daemon hard crash；
- expired lease takeover；
- receipt 已持久化但尚未 ingest 时重启；
- WebSocket transient error/retry 后继续 turn；
- 3 个 real compaction checkpoint；
- fixed-revision independent evaluator；
- structure request lifecycle；
- evidence-backed graph expansion；
- speculative branch suppression；
- fresh durable worker context isolation。

最终 Runtime 不变量：

- consistency violation/warning：`0/0`
- duplicate terminal fact：`0`
- duplicate ledger fact：`0`
- premature done：`false`
- compaction fallback：`0`
- checkpoint chain：valid
- run-owned orphan process：`0`

Runtime 没有因为 worker 自报测试成功而完成 goal，也没有因为 operator 停止而伪造 resolved。

## 为什么任务最终没有通过

### 不是单纯的 worker 能力不足

Primary 将 F2P 从 `0` 提升到 `58`，strategy worker 又补齐多个实际 SRS 区域，说明 worker 能处理相当规模的 brownfield 变更。

### 也不是 Runtime consistency 失败

进程恢复、DB facts、checkpoint、evaluator provenance 和 completion invariant 全部通过。

### 主要缺口是跨 worker 的 workspace/candidate 管理

Runtime 当时没有维护 immutable best-known candidate：

```text
best primary revision: 58/68
        |
        | shared workspace continues changing
        v
expanded strategy final: 55/68
```

新 worker 的广覆盖修改没有保护 primary 已建立的稳定 milestone，Runtime 也没有在新证据变差后回到该 revision。于是本地工程覆盖增加，却没有转化为 official oracle 增益。

“保留稳定 milestone、隔离探索分支、允许比较后选择或回滚”是通用 orchestration 问题，不依赖 evaluator。普通任务可以根据现有测试、构建结果、artifact check、worker evidence 或 human decision 判断是否接受新分支；不需要把 hidden score 引入生产 Runtime。

第二个限制是 evaluator 剩余诊断多为 `test_id_only`。这足以证明 candidate 未完成，但不足以精确指导隐藏 contract 修复，导致后期推断成本很高。

## 对 Orchestration 初心的回答

本 run 支持以下设计：

- 默认一个 coherent primary worker，而不是预先拆成多个角色；
- Runtime 负责长周期 persistence、recovery、policy、evidence 和 completion truth；
- 当任务确实存在外部验证反馈时，优先回流同一 worker session；
- 只有真实 durable boundary 才扩展 graph；
- fresh worker 可以用于独立策略探索，但不能直接覆盖 primary 的稳定 milestone。

本 run 不支持以下做法：

- 因为任务复杂就预先创建很多 worker；
- 将 benchmark 的 hidden evaluator loop 复制成普通任务的默认工作流；
- 没有独立验收标准时，创建一个 evaluator 重跑 worker 自己写的测试；
- 让新 strategy worker 在唯一共享 workspace 上无保护地继续修改；
- 只记录最新 workspace，不保留已验证的稳定 milestone。

面向一般任务，下一步最有价值的改进是：

1. 为长周期 primary worker 提供可恢复的 workspace milestone，而不是增加执行角色；
2. structure expansion 使用独立 worktree/branch，避免新策略直接破坏 primary 工作区；
3. worker receipt 明确记录完成范围、验证命令、artifact evidence、风险和未完成项；
4. reducer 只按 goal contract 中真实存在的 completion/evidence 要求判断，不凭空要求 external evaluator；
5. 无预设 oracle 的普通任务优先由 worker 自测，必要时根据风险进入 human review 或已有专项 verifier；
6. 保留多个策略产物及 provenance，让 Decision Provider 或 human 可以选择 merge、继续或 rollback；
7. 继续优化 crash recovery、session continuity、capability boundary、liveness 和 observability。

Benchmark harness 自身仍可单独改进：保存每轮固定 revision 和 official score，防止后续探索覆盖 benchmark best result；改善 bounded diagnostic extraction。这些只服务于测试能力测量，不应升级成所有 Runtime job 的默认协议。

## 如何阅读其余证据

- `execution-summary.md`：当前文件，优先阅读；
- `orchestration-usefulness.md`：架构价值与下一步判断；
- `capability-trace.md`：完整自动生成时间线，适合审计具体 node、命令和 evidence；
- `capability-trace.json`：机器可读 trace；
- `run-report.json`：Runtime invariants、evaluator progression 和 metrics；
- `candidate.patch`：最终 candidate，不含 protected oracle；
- `candidate-evidence.json`：revision、hash、大小和 changed paths。

## 证据边界

本总结只使用 Runtime DB facts、可观察 worker events、bounded evaluator diagnostics 和归档 candidate metadata。未读取或泄露 gold patch、protected evaluator source、隐藏 expected values 或模型私有推理。
