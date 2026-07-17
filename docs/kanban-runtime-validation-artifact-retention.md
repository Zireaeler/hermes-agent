# Hermes Runtime 真实验证证据保留规范

## 1. 目的

长周期真实验证会产生两类不同输出：

1. 用于理解和比较各次 run 的公开精简报告；
2. 用于过程分析、重放和恢复的原始执行证据。

报告不能替代原始证据。Cleanup 可以删除可重建的执行材料，但不能删除 Codex session、
worker event stream、Runtime database 或 evaluator output 的唯一副本。

## 2. 存储模型

每次 run 使用一个稳定身份：

```text
phase / benchmark instance / run id
```

Git 中的可检索目录位于：

```text
docs/validation/<phase>/<instance>/<run-id>/artifact-catalog.md
```

原始证据可以保存在 Git 之外的稳定 artifact root：

```text
<artifact-root>/<phase>/<instance>/<run-id>/
  ARTIFACTS.md
  manifest.json
  codex-home/
    sessions/
  worker-events/
  optional-telemetry/
  runtime-state/
  evaluator/
  candidates/
  reports/
  environment/
```

默认运行位置应使用 `/root/hermes-validation-artifacts` 之类的持久文件系统，而不是
`/tmp`。只有在更新 catalog 并验证 manifest hashes 后，才允许移动 run。

## 3. Git Catalog

每个 `artifact-catalog.md` 必须记录：

- phase、instance、run id、结果分类和完成时间；
- protocol commit 与 hash；
- model、Codex version、effort 和 orchestration mode；
- parent 与 child thread IDs；
- candidate 与 evaluator result 链接；
- 原始证据的绝对路径或 artifact-store location；
- 每组 artifact 的 SHA-256、size、retention state 和 sensitivity class；
- artifact 当前是 present、moved、intentionally omitted 还是 lost；
- cleanup action 与精确 removed entries；
- 已知 observability gaps。

即使 raw artifact store 暂时离线，catalog 也必须有用。因此它需要包含最终结果和足以
识别 run 的 hashes，但不需要复制所有 raw events。

## 4. 必须保留的原始证据

以下内容不可替代，真实模型 run 必须保留。

### 4.1 Codex 执行证据

- `codex-home/sessions` 下每个 parent/subagent rollout JSONL；
- thread metadata、compaction events、collaboration calls、tool results 和 terminal messages；
- outer `codex exec --json` event stream；
- stdout 与 stderr；
- guardian 或 approval session events；
- native resume 所需的 session index/state files。

### 4.2 Runtime 执行证据

- Runtime/Kanban SQLite database 或 transactionally consistent snapshot；
- graph patches、reducer events、receipts、ledger entries、checkpoints 和 decision segment entries；
- daemon、worker、evaluator 和 fault-injector logs；
- leases、attempts、materializations、process IDs 和 terminal facts；
- policy resolution 与 capability footer evidence。

### 4.3 可选 Provider 遥测

可以保留 provider transport counters 和 error summaries，用于诊断模型服务或测量成本。
这些信息不是解释 worker 行为、Runtime orchestration、candidate quality 或 goal completion
所必需的证据；缺失时不得阻断 archive verification 或 cleanup。

完整 HTTP/WebSocket request/response capture 不属于 Runtime Kernel validation 要求。
如果保留可选 provider telemetry，必须删除模型源 API key，并替换真实模型源 base URL。

### 4.4 Candidate Lineage

- base commit 与 repository identity；
- 每个 evaluator-targeted candidate patch 及其 SHA-256；
- changed/untracked file manifests；
- best-known candidate 与 promotion/rollback decisions；
- 创建 commit 时对应的 candidate revision 或 Git bundle；
- cleanup 前最终 workspace status。

当 base revision 和完整 binary patch 已保留时，workspace 本身可重建。验证 candidate
reconstruction 后可以删除 workspace。

### 4.5 Evaluator 与环境

- 每次 evaluator invocation 对应一个 standardized result；
- raw evaluator output、bounded diagnostics、exit status 和 wall time；
- 被评估的精确 candidate hash；
- image tag 与 immutable image digest；
- benchmark dataset revision 与 protected-test hash；
- Python、Codex、dependency/toolchain、OS 和 environment fingerprints；
- qualification base/gold outcome 与 manifest hashes。

Gold patches 和 protected tests 属于 benchmark-integrity assets。它们不能发布到 Git，也
不能暴露给 workers。Private artifact store 可以保存它们，公开 catalog 只记录 hash 和
qualification facts。

## 5. Credential 边界

本项目 validation archive 中需要按 credential 处理的值只有模型源 API key 和真实模型源
base URL。

- `auth.json` 永不 commit；
- 从本地 export traces 和 reports 中删除 API keys；
- 用稳定 placeholder 替换真实 provider base URL；
- 发布前扫描 headers、query parameters、environment variables、commands 和 exception strings；
- redaction 同时记录 original artifact hash 与 archived artifact hash。

其他 Codex configuration、session messages、reasoning、tool calls 和 subagent communication
均可保留。包含 benchmark protected content 的文件因为实验完整性而保持 private，不是因为
credential secrecy。

## 6. Cleanup Allowlist

Automatic cleanup 只能删除明确可重建的内容：

- 记录 image digest 后的 Docker images；
- 记录 manifest 与 fingerprint 后的 dependency toolchains；
- 记录 protected hashes 和 outcomes 后的 qualification checkouts；
- 验证 candidate reconstruction 后的 workspace；
- pytest caches、bytecode、package caches 和 temporary build products；
- 保留 effective redacted config 与 hashes 后的 seed homes。

Automatic cleanup 不得删除：

- `codex-home/sessions` 或 node Codex homes；
- `worker-events`；
- 已经存在的 provider traces，但创建 provider trace 本身不是强制要求；
- Runtime database/service state；
- evaluator raw results；
- candidate lineage；
- manifests、catalogs 或 reports。

任何代码都不得删除整个 run root。Terminal status 和已生成 report 不是充分 cleanup
predicate。

## 7. 删除门禁

删除不可替代证据的 source copy 前，必须同时满足：

1. stable artifact store 中存在 verified copy；
2. `manifest.json` 包含 copied files 的 hashes 和 sizes；
3. Git catalog 指向对应 artifact-store identity；
4. credential 与 benchmark-integrity scans 已完成；
5. 用户明确授权删除 source copy。

任一条件不满足时，cleanup 必须停止并报告保留路径。

## 8. 当前证据损失

Phase 4G9 Arm 1 `phase4g9-arm1-native-20260717` 早于本规范。它的 derived report、
candidate、command list、session summary 和 collaboration event summary 已保存在 Git，
但原始 isolated Codex homes 与 raw worker event stream 在没有 stable raw archive 的情况下
被删除。Per-run catalog 必须将这些内容标记为 lost，不能暗示证据完整。

## 9. 已实现门禁

`hermes_cli.validation_artifacts` 已实现 archive 与 cleanup gate：

- 原始证据先复制到 staging directory，再 atomic promotion；
- 省略 `auth.json`；
- 替换 text traces 中精确的模型源 API key 与 base URL；
- 每个 archived payload file 记录 source/archive SHA-256、size 和 redaction count；
- 完成后重新读取 manifest，并验证每个 archived hash；
- cleanup 拒绝 missing、invalid、tampered 或属于其他 run 的 manifest；
- cleanup 拒绝 rebuildable allowlist 之外的所有 entries。

Phase 4G8 completed-run compaction 会先 archive `codex-homes`、`service`、`hermes-home`、
reports，以及存在的 worker event/optional telemetry directories，再删除 workspace、home
或 seed state。Phase 4G9 native run 在成功返回 real-run result 前，必须 archive
`codex-home`、`worker-events` 和 reports。

Artifact root 可以通过 `HERMES_VALIDATION_ARTIFACT_ROOT` 或 Phase 4G9
`--artifact-root` 配置。默认值：

```text
/root/hermes-validation-artifacts
```

Phase 4G9 只在 `run-report.json` 中记录 aggregate transport counters，作为可选 telemetry。
这些 counters 不属于 archive acceptance predicate，也不需要独立 provider-trace directory。
