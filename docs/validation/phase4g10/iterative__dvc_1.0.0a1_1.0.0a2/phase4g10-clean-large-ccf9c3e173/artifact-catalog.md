# Phase 4G10.1 Clean Replay Artifact 目录

## 远端可读证据

| 文件 | Bytes | SHA-256 | 用途 |
| --- | ---: | --- | --- |
| `execution-summary.md` | 提交时固定 | 提交时由 Git 固定 | 中文执行过程与架构判断 |
| `clean-replay-summary.md` | 1,657 | `f741cd24592f828089aec5d9be5804cfd0dca6feb419d0fbd56054dbc520e243` | Harness 生成的一页断言摘要 |
| `clean-replay.json` | 175,846 | `2960f90022ed664a1633e3b314682dd97aa6e1af1894109682c71d32947f4b39` | Clean invariants、事件计数和 archive 结果 |
| `capability-trace.md` | 34,355 | `cdffcee98645f7dd3e14174318062cb495526177e821847084b489b93acd0e83` | 按阶段的完整可读运行轨迹 |
| `arm2-orchestration.json` | 67,380 | `74129800e4b5108861dd9a21af6717775d3711ab462cb326188ccddd657bbc5a` | 节点、session、贡献、时间和 token 指标 |
| `run-report.json` | 220,870 | `47e1ac5627fea8fe1a4f792b7f30ca49a28bc2a05913751ac66f3dbca3f47941` | Runtime correctness、provider audit 和 evaluator 结果 |
| `candidate.patch` | 148,254 | `ff1a8775d6e817fc74443771337e07330b7d99159a955d1e04c3a3fba007422a` | 第三次 evaluator 固定评估的 candidate |
| `candidate-evidence.json` | 2,935 | `a6038549942adba16f2994d2785b5c818a7789a0219eb6c029dbb498ef2aa334` | base、workspace revision 与 patch identity |
| `ownership-canary.json` | 1,676 | `4a391fb3edc656f82e7372251fde5410b45e658738c99dc284c73a8c284d28ed` | owner、sibling 和 symlink 边界审计 |
| `clean-replay-source-state.json` | 342 | `e9facb81ea37d77c005980b08a2caf5d844f8fce2b8030fcd1e6a98568865144` | 初始与最终 Runtime source revision |

## Frozen Contributions

| Child | Patch bytes | Patch SHA-256 | Metadata |
| --- | ---: | --- | --- |
| `reporting-plots-templates-completions` | 28,516 | `72b5ee1e596294e4650813c2e6e9457c26c6aa627d2fe89430dd5f034ed2440d` | `contributions/reporting-plots-templates-completions.json` |
| `stage-pipeline-run-cache` | 36,160 | `1a252f08dda040cfa25e22a45a3fba8291bb6f6a4d5d17243180e9281b269339` | `contributions/stage-pipeline-run-cache.json` |
| `tree-remote-import-transfer` | 31,955 | `9be342fdde2e7d2eeec311fff3ad590cbb21d9806d55be8ab1a5b4641e3e1b4b` | `contributions/tree-remote-import-transfer.json` |

三个 patch hash 与 DB 中 `node_contribution_frozen` artifact metadata 一致；对应 metadata 文件还记录
base revision、changed files、file hashes、scope status、materialization 和 integration owner。

## Stable Raw Archive

完整原始证据保存在本机稳定 artifact root：

```text
/root/hermes-validation-artifacts/phase4g10-clean-replay/
  iterative__dvc_1.0.0a1_1.0.0a2/
  phase4g10-clean-large-ccf9c3e173/
```

- archive status：`verified`；
- files：`280`；
- total bytes：`40,212,212`；
- `manifest.json` SHA-256：
  `3d84eaeb0c92f25591868fab6b92b410f8d0c388b5b1a5fdcde70c3bc8d06fd7`；
- manifest redaction count：`0`；
- missing expected entries：`0`。

Raw archive 包含：

- `kanban.db` 和 supervisor state；
- primary 与三个 durable child 的 Codex session JSONL；
- worker events、structured receipts 和 task logs；
- frozen contribution patch/metadata；
- candidate、运行报告和 machine assertions；
- artifact-level SHA-256 manifest。

Archive manifest 校验通过后，runner 删除了 run root 下 `workspace`、`runtime-worktrees`、`home` 和
`codex-home-seed` 等可重建目录，共释放 `10,773,219` bytes。DB、sessions、events、reports、contributions
和最终 candidate 均保留。

API key 和 base URL 按 retention policy 排除或脱敏。Gold patch、protected test source 和 evaluator 临时
raw artifacts不进入 worker-visible archive，也不提交到 Git。

