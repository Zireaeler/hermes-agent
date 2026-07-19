# Phase 4G12 Small Artifact Catalog

## 1. Run Identity

| 字段 | 值 |
| --- | --- |
| Phase | `phase4g12` |
| Instance | `dynamic-small` |
| Run ID | `phase4g12-small-20260719-161352` |
| 结果 | Runtime passed / task capability passed |
| 完成时间 | 2026-07-19 16:20 Asia/Shanghai |
| Protocol commit | `5ca7138` |
| Branch | `feature-kanban-runtime-kernel` |
| Model | `gpt-5.6-sol` |
| Reasoning effort | `xhigh` |
| Codex | `codex-cli 0.144.4` |
| Orchestration mode | `closed_loop_coordination` + evidence-driven expansion |

## 2. Thread 与 Runtime Identity

| Node | Codex thread | Resume count | Terminal state |
| --- | --- | ---: | --- |
| parser-contract | `019f7970-5753-7921-a81f-6c12c5d4d443` | 1 | completed |
| renderer-contract | `019f7970-5753-7780-be1a-1b55e19765b4` | 1 | completed |
| legacy-token-adapter | `019f7972-7817-7273-a829-fbddf9e5780e` | 1 | completed |
| pipeline-integration | `019f7974-d047-7ff1-bfc0-06f31b7275e2` | 0 | completed |

Runtime job：`rjob_8ef1528aa3e1`。

## 3. Stable Raw Archive

Artifact path：

```text
/root/hermes-validation-artifacts/phase4g12/dynamic-small/phase4g12-small-20260719-161352
```

Manifest：

```text
manifest.json
sha256 = 33e2c05c4e586a1c9106b27dfa78e2059151e55218201f4e509ff2a3cb5a588f
status = verified
files = 110
bytes = 3791198
```

| Artifact group | Files | Bytes | Group digest | Retention | Sensitivity |
| --- | ---: | ---: | --- | --- | --- |
| `codex-home` | 94 | 1,456,155 | `9805ff4dd5df4e5ac3a022cb333fbc4a5ef9b44be2d7c92ea807afdd196a9809` | present | sessions public；credential omitted |
| `hermes-home` | 9 | 2,216,149 | `a984f9478372cd121b48f0afbc56512a840cfe9dde758b8817c0e0b0d07d2289` | present | DB/logs，已 redaction scan |
| `reports` | 1 | 115,220 | `4b9bc9f0dee98b24eecc9b2d1c992eaee11abe29e14f5b80f8a76212479c7ec1` | present | public report |
| `runtime-contributions` | 6 | 3,674 | `ed063398db29e8c01755d85ce85eb1ebfbd973d269bccfb65e874aed818ce8c2` | present | public candidate lineage |

Group digest 是按 manifest 中该组每个 `path sha256 bytes` 排序串联后计算的 SHA-256；单文件 hash
以 stable archive 的 `manifest.json` 为准。

`codex-home/auth.json` 已按 credential policy 省略。模型源 API key 和真实 base URL 不进入 Git，
archive redaction policy 为 `model_source_key_and_base_url_only`。

## 4. Git-Published Evidence

| 文件 | SHA-256 | Bytes |
| --- | --- | ---: |
| `run-report.json` | `d66a0e30fd270aa65e9631fd6fbd8a52d9ddbbec5dc4684a5d949a98627e1db6` | 115,220 |
| `parser-contract.patch.gz` | `780bebed2a056cac1a6981db13e81fcda97275acd9e698a029116f94bea2336b` | 314 |
| `renderer-contract.patch.gz` | `2df0fc4f4faf6748a74f27759b90a668d85913f4e9fd70f5095d2cf5dc735d9d` | 226 |
| `legacy-token-adapter.patch.gz` | `d095358c221980ed6c7522d091ec3d279f8da5aaf6a5c82854ea4fce5721ea46` | 233 |

Git 中的 patch 是方便审阅的派生副本。权威 raw patch 与 metadata 保存在 stable archive 的
`runtime-contributions`。

## 5. Cleanup State

- Stable archive：present and verified；
- Source run：仍保留于
  `/tmp/hermes-phase4g12-real/phase4g12-small-20260719-161352`；
- Source cleanup：not performed；
- Failed attempt roots：在本阶段报告与提交完成前暂未删除；
- Workspace：可由 base revision 与三份 patch 重建，但本轮没有请求删除不可替代 evidence。

## 6. Observability Gaps

- 没有完整 HTTP request/response body trace；该项不是 Runtime validation acceptance predicate；
- Decision Provider token 未计入 Codex CLI usage；
- Small fixture 不包含 independent evaluator、daemon crash 或 lease takeover；
- WebSocket 基础设施无效 attempt 只记录错误事实，最终有效 run 使用同源 HTTP transport。
