# Phase 4G14 Natural Medium 真实执行报告

## 1. 结论

2026-07-20，Phase 4G14 在冻结的 SWE-EVO
`dask__dask_2023.6.1_2023.7.0` 上完成了一次全新的 Natural Medium 运行。

本 run 的结论是：

- Runtime handoff 验证通过；
- 3 个 isolated child 的 attempt patch 全部捕获、晋升并被原 Primary 集成；
- 没有 receipt repair、实现重做、replacement worker 或 context reacquisition；
- official evaluator 只运行 1 次，结果不回流 worker；
- Runtime consistency 为 `0 violations / 0 warnings`；
- benchmark 最终为 F2P `3/5`、P2P `707/707`，因此 task capability 未 resolved；
- 与 Phase 4G13 失败 run 相比，wall time 从约 57 分 49 秒降为 31 分 17 秒；
- 与 coherent single worker 相比，最终质量没有提升，且 wall time 仍多约 11 分 46 秒。

因此，Phase 4G14 证明了系统级 orchestra 可以可靠保存并集成隔离 worker 的工程成果，修复了
Phase 4G13 的实际浪费；它没有证明在本任务上多 worker 能提高最终代码质量。

---

## 2. 测试约束

本 run 只向 Runtime 提供：

- 冻结 repository base revision；
- 真实 2023.7.0 SRS；
- 正常 workspace 和 worker capability；
- 最多 3 个 durable child 的通用 orchestra policy。

未向 Decision Provider 或 worker 提供：

- candidate key；
- gold patch；
- protected test source；
- 预设文件拆分；
- 预设 child 名称或责任；
- evaluator 的 expected value；
- 多轮 evaluator 追分机会。

所有 Codex worker 禁用内部 subagent。每个 durable child 使用独立 Codex home、session 和 git
worktree。Official evaluator 使用冻结 image 和固定 candidate revision，只记录 benchmark 结果。

---

## 3. 实际 Graph

系统最初只创建一个 coherent Primary：

```text
implement-release-2023-7-0
    |
    | read-only repository assessment
    v
early structure checkpoint
    |
    | repository evidence + disjoint write scopes
    v
Decision Provider accepts expansion
    |
    +-- release-cli-traceback
    +-- release-dataframe
    +-- release-array-docs
    |
    v
same Primary session resumes as integration owner
    |
    v
fixed-revision official evaluator, exactly once
```

非评估 worker 共 `4` 个：1 个 Primary 和 3 个 child。Evaluator 是第 5 个 execution node，
不计入编码 worker 数。

### 为什么拆成三个 child

Primary 先检查 repository，随后报告三组相对独立的责任：

| Child | 责任 | 声明写入面 |
| --- | --- | --- |
| `release-cli-traceback` | CLI entry point 与 IPython traceback | `dask/cli.py`、`dask/base.py` 及对应测试 |
| `release-dataframe` | pandas/dataframe 兼容性与 quantile tree | dataframe source 与对应测试 |
| `release-array-docs` | `chisquare` maintenance 与 rechunk 文档 | array stats、文档及对应测试 |

Primary 保留共享 release metadata、Distributed version pin、跨域验证和最终集成。拆分依据来自真实
文件、测试和依赖边界，不是 SRS 中预先给出的阶段或角色。

---

## 4. 执行过程

### 4.1 Primary 结构评估

Primary 在第一次 materialization 中只读检查 16 组 source、test、release 和 dependency 文件，
形成带 repository evidence 的 early structure checkpoint。Decision Provider 执行两次有效结构
决策：第一次创建 Primary，第二次接受 3-child expansion；有效决策比例为 `2/2`。

### 4.2 三个 child 并行实现

三个 child 都在自己的 worktree 中完成真实修改和本地验证：

| Child | Patch | 文件数 | 结果 |
| --- | ---: | ---: | --- |
| `release-array-docs` | 996 bytes | 2 | succeeded |
| `release-cli-traceback` | 3,161 bytes | 4 | succeeded |
| `release-dataframe` | 7,306 bytes | 6 | succeeded |

本 run 没有自然产生 malformed receipt。这不是覆盖缺失：controlled two-child case 已确定性验证
malformed receipt 的 quarantine、metadata-only repair 和无实现重做；Natural Medium 负责验证
正常真实模型路径不会因 handoff 再退化。

### 4.3 Phase 4G14 handoff

每个 child terminal 后都先由 Runtime 从 git worktree 捕获 attempt patch，再验收 receipt：

```text
3 terminal contribution attempts
    -> 3 immutable attempt patches
    -> 3 valid contribution receipts
    -> 3 promoted contributions
    -> Primary accepts 3/3
    -> final candidate contains all 3 lineages
```

| 指标 | 结果 |
| --- | ---: |
| Attempt capture | `3/3` |
| Capture failure | `0` |
| Promotion | `3/3` |
| Receipt invalid / repair | `0 / 0` |
| Receipt 导致的实现重做 | `0` |
| Primary accepted / modified / rejected | `3 / 0 / 0` |
| Integrated contributions | `3/3` |
| Preservation ratio | `1.0` |

Primary 复用原 Codex thread 完成集成，没有创建 full-workspace recovery worker。它验证 patch hash、
应用三个 frozen patch、补共享 release metadata，并运行跨责任测试。

### 4.4 Primary 验证

Primary 报告的主要本地结果：

- release focused suite：`65 passed, 1 xfailed`；
- rename / astype / first-last selection：`14 passed`；
- base/docs selection：`129 passed, 3 skipped`；
- dataframe IO selection：`72 passed, 4 skipped`；
- `git diff --check` 通过。

多进程 semaphore 和已安装 Distributed 版本不兼容被记录为环境边界，没有被伪装成通过结果。

### 4.5 Official evaluator

Evaluator 对固定 candidate 只运行一次：

- F2P：`3/5`；
- P2P：`707/707`；
- feedback coverage：完整；
- feedback consumed：`0`；
- resolved：`false`。

剩余两个错误是：

1. `Series.rename(..., inplace=True)` 产生了 `PendingDeprecationWarning`，official contract 要求
   `FutureWarning`；
2. CLI warning 文案写成 `exception occurred`，official contract 期望历史兼容字符串
   `exception ocurred`。

这两个错误说明 worker 的本地测试没有精确覆盖隐藏 release contract，不是 handoff 丢失或
integration 漏 patch。

---

## 5. 与两个对照的比较

| 运行 | 编码 worker | Wall time | Input / cached / output tokens | F2P | P2P |
| --- | ---: | ---: | ---: | ---: | ---: |
| Coherent single worker | 1 | 19m31s | 7.86M / 7.36M / 31,966 | 3/5 | 707/707 |
| Phase 4G13 Runtime | 5 | 57m49s | 7.30M / 6.57M / 120,661 | 3/5 | 707/707 |
| Phase 4G14 Runtime | 4 | 31m17s | 6.43M / 5.84M / 79,877 | 3/5 | 707/707 |

Phase 4G13 的 3 个 child 也完成了真实实现，但两个 child 因 receipt 字段错误进入 failed，Primary
无法获得其 artifact，最终新增 recovery worker 从完整 workspace 重建结果。Phase 4G14 保留同样的
自然三路拆分，却把 handoff 从“两个成果不可交付”变成 `3/3` 可靠集成，省去 recovery worker，
wall time 下降约 `45.9%`。

相对 coherent single worker，Phase 4G14 没有获得质量优势，而且更慢约 `60.2%`。这说明该任务的
三个代码面可以隔离并行，但 hidden contract 猜测仍由各 worker 独立承担；Runtime 的 artifact
handoff 不能自动弥补需求理解错误。

协调开销中，Decision Provider 估算 token 为 `16,759`；结构评估 worker 报告 input/output 为
`507,632 / 10,820`，其中 cached input 为 `430,336`。这是真实成本，不应只报告 child 并行节省。

---

## 6. 实测发现并修复的基础设施问题

正式有效 run 前有两个 infrastructure-invalid preflight：

1. Runtime 把 terminal receipt 的动态约束错误应用到 early structure checkpoint schema；
2. live provider 不接受 `uniqueItems`、`minItems`、`maxItems` 等 Structured Outputs 关键字。

修复后，provider schema 只提供其支持的 enum 提示；空集合、完整集合和唯一性仍由本地 canonical
validator 强制，不降低 correctness boundary。两个 invalid preflight 没有进入能力比较。

正式 run 还暴露了一个 stop-ordering 竞态：evaluator failure ingest 的同一 supervisor tick 曾创建
一个未 dispatch 的 Primary remediation task，runner 随后才记录“一次 evaluator 后停止”。该 task
没有进程、没有 feedback consumption、没有 workspace 修改，因此不改变本次质量和 handoff 结论；
但归档投影停在 `waiting_worker`，而不是稳定的 evaluated candidate。

Phase 4G14 收尾实现已把 evaluated-stop 阈值写入 job verification policy。现在 reducer 在创建
failure bundle、interrupt session 或 materialize remediation 之前先检查 DB coverage；达到阈值后
本地禁止 remediation。确定性回归同时证明原本需要 evaluator 反馈的 same-session remediation
路径不受影响。本问题按要求没有通过重跑 Medium 来掩盖。

---

## 7. 能证明和不能证明的事项

本 run 能证明：

- Runtime 可以从自然 repository assessment 形成 evidence-backed durable expansion；
- isolated child 的 patch、receipt、promotion 和 Primary attribution 已形成可靠 lineage；
- 原 Primary session 可以在 child 完成后恢复并消费全部 frozen artifacts；
- handoff 修复消除了 Phase 4G13 的 recovery 重做；
- evaluator 一次记录与 Runtime correctness 可以和 task quality 分开报告。

本 run 不能证明：

- 多 worker 在此任务上比单 worker 更准确；
- 所有 Medium 都应该拆成 3 个 child；
- hidden evaluator 应成为普通开发任务的默认 completion gate；
- peer-to-peer 无通信设计在高耦合任务上优于 native parent/subagent；
- replacement lineage 的自然分支已触发。该分支由 controlled regression 覆盖，本 run 没有发生
  replacement。

---

## 8. 证据

- Run：`phase4g14-runtime-medium-1c43cd09ba`
- Runtime job：`rjob_8fa98d7fae85`
- Stable archive：
  `/root/hermes-validation-artifacts/phase4g14/dask__dask_2023.6.1_2023.7.0/phase4g14-runtime-medium-1c43cd09ba`
- Archive status：`verified`
- Archive files / bytes：`273 / 22,659,058`
- Manifest SHA-256：`be6b08a62db4b366a393f9fa2b91148366b9bca37aba9553eade7f06e3e73bac`
- Candidate patch SHA-256：`b0526b166210cb38f3d4ed4aea53af03979177ed29de1d7c37ba7a2fa0729f0e`
- Qualification spec SHA-256：`c39bdb2f5a8caacdde22b2d4d81c820c7be7a02a9eca54efe3c70323a0338845`
- Official image digest：
  `sha256:e0ee1e98546c7599146b341c40503c109b69bddc740802ef8d287b388f8cd29f`

Archive 保留 Runtime DB、4 个 Codex session、worker events、三份 attempt patch、promotion metadata、
candidate、evaluator result 和生成报告；API key/base URL 已省略或脱敏。
