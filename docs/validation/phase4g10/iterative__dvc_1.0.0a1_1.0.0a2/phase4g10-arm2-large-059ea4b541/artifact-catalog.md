# Phase 4G10 Arm 2 Artifact 目录

## 远端可读证据

| 文件 | Bytes | SHA-256 | 用途 |
| --- | ---: | --- | --- |
| `execution-summary.md` | 2,470 | `828d6843281f01183ec1cd2485cf6683ef22a5dc925bad211b31c1e7b0837559` | 一页执行摘要 |
| `architecture-conclusion.md` | - | 提交时由 Git 固定 | 架构判断与三臂比较 |
| `capability-trace.md` | 48,774 | `dfcf487745aebe7aae4b1c7bf87f470d8f82572d13fb10a741b3d1b1bf5daf2a` | 按阶段的 DB/evaluator 过程 |
| `run-report.json` | 57,244 | `56a9162bdaa6f26ec2c18f281f250bb87d6851442c73471a0f803cc1ab3ebc57` | Runtime correctness 与最终 capability |
| `arm2-orchestration.json` | 202,579 | `5794df1c90b57bb4c8d5cf9638d1cefedf0a473a0b5ed7fe67f1434c3a589cb0` | 节点、时间线、token、attribution 和断言 |
| `candidate.patch` | 215,493 | `62374385dfb782bfab284e353170d1d0628f8121ece4e289456dc6fa4512a3d4` | 最后一次已评估 candidate |
| `candidate-evidence.json` | 3,896 | `a2aeddb9b5e53bb066ab7037f647966b9d8509ec926b23d9374c94f4237c8163` | base、changed files 与 patch identity |
| `operator-stop-request.json` | 415 | `4d543f3c246de57557e222d450d768de999fbcd8bc6c869d61b0b78f6c9de10e` | plateau 停止理由 |

## Frozen child contributions

| Child | Bytes | SHA-256 |
| --- | ---: | --- |
| `contributions/plots-diffs-output-model.patch` | 38,778 | `100b5a21eed0fa2d861249ad122c522bb6acd12fc487266ef4f4e71f0aca03f6` |
| `contributions/stage-runtime-and-run-cache.patch` | 25,151 | `0a422e5dd73e5f980130b2c3e83f2818a9e8da532cfb77c2378c5ed0be03b4da` |
| `contributions/tree-remote-and-streaming.patch` | 12,898 | `02bd555d3b8c4d3d32121f1acd04fd210527603b377e10acf6365a1b62e3c4c9` |

三个 contribution hash 与 `arm2-orchestration.json` 中的 DB artifact metadata 一致。Primary 最终将
三项都归类为 `modified_contributions`。

## Stable raw archive

完整原始证据未全部提交到 Git，保存在本机稳定 artifact root：

```text
/root/hermes-validation-artifacts/phase4g10/
  iterative__dvc_1.0.0a1_1.0.0a2/
  phase4g10-arm2-large-059ea4b541/
```

- archive status：`verified`；
- files：`355`；
- total bytes：`43,984,639`；
- `manifest.json` SHA-256：
  `82bc1bc752d28203ac6288329516179407fe5739ff7582a847a9fa753ab6e3b5`。

Raw archive 包含：

- Hermes DB 与 service state；
- primary、三个 durable child 和一次 superseded strategy 的 Codex session JSONL；
- worker events 与 receipt evidence；
- frozen contributions；
- evaluator result references；
- 首次归档时的 reports 与 manifest。

最终 derived reports 在 raw archive 创建后修正了 ledger 幂等统计、resume boundary 和 stop reason，
因此另存为 append-only verified supplement：

```text
/root/hermes-validation-artifacts/phase4g10-report-refresh/
  iterative__dvc_1.0.0a1_1.0.0a2/
  phase4g10-arm2-large-059ea4b541-report-v2/
```

- files：`6`；
- total bytes：`482,097`；
- `manifest.json` SHA-256：
  `ccf95f39ce86614c3bac13259d633507b3b7f31904999efd13d30b2181ed4872`。

远端 Git 中的报告与该 `report-v2` supplement 一致；原 raw archive 没有被覆盖。

API key 和 base URL 通过 archive redaction policy 处理。Gold patch、protected test source 和 evaluator
临时 raw artifacts 不在 worker-visible archive 中。
