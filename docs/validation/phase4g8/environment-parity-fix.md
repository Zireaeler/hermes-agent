# Phase 4G8 Worker / Evaluator 环境等价性修复

## 1. 结论

2026-07-14 的事后审计确认，Medium 真实运行使用的旧 runner 没有把 official evaluator 的测试前
dependency hot-fix 应用到 worker toolchain。该问题已经修复，并通过实际 Dask official image 和完整
official evaluator 验证。

本次验证不调用 Decision Provider 或 Codex worker，不构成新的任务能力分数。它只证明环境准备、
提取、缓存、指纹和 evaluator parity 边界已经闭环。

## 2. 原始问题

Dask official image：

```text
xingyaoww/sweb.eval.x86_64.dask_s_dask-9531
```

Pristine image 中的关键依赖：

```text
Python 3.10.14
pandas 2.2.2
pytest 8.3.2
```

旧 runner 直接复制 `/opt/miniconda3/envs/testbed` 给 worker，因此 worker 使用 pandas 2.2.2。

Official evaluator 在运行测试前还执行：

```bash
pip install 'pandas<2.0'
```

实际 evaluator 环境使用 pandas 1.5.3。Worker 因此在错误的最终依赖环境中运行公开测试，并遇到
大量不属于 official evaluator 的 pandas 2.2 兼容噪声。

## 3. 修复规则

Runner 现在执行：

```text
official harness eval script
    -> 只提取 Start Test Output 标记之前的 setup prefix
    -> 在临时 official image container 中执行 setup
    -> 提取 post-setup conda toolchain
    -> 删除 /testbed 和 /workspace 的 .pth 绑定
    -> 比较 container 与 extracted toolchain 的环境指纹
    -> 转为 root-owned read-only cache
```

Cache identity 包含：

- official image content digest；
- evaluator setup script SHA-256；
- setup environment SHA-256；
- setup 后的 resolved environment fingerprint。

每次真实 run 都重新执行 setup preflight。解析结果相同时复用只读 toolchain；解析结果变化时生成新的
内容寻址缓存，避免未锁定的 package constraint 静默复用旧环境。

Worker 不可见：

- setup script 正文；
- hidden test patch；
- evaluator test command；
- gold patch；
- `/testbed` checkout。

每次 official evaluator 运行后也会回报环境指纹，并与 worker toolchain 的预期指纹比较。不匹配时
运行必须作为 infrastructure-invalid 失败，不能计为 task-quality failure。

## 4. 实际 Dask 验证

环境准备：

```text
image content digest:
sha256:b067cb26fc09fd8cb8371a6271f19e1357de2d303ddd220a53894cab77f39cce

setup SHA-256:
f11ec9f1b317aba59f68717bb7d8df236643e75110e95d436ac9e10c3fd3b75e

environment SHA-256:
8601ded067e25620404a459f6c1ed63bb4ab2fc47fdb474d3262e8fc05415dd2
```

准备后的 worker toolchain 与 evaluator 均为：

```text
Python 3.10.14
pandas 1.5.3
pytest 8.3.2
numpy 1.26.4
distributed 2024.8.1
pyarrow 17.0.0
SQLAlchemy 2.0.32
```

完整 official evaluator 使用 base candidate 运行结果：

```text
FAIL_TO_PASS: 0 / 44
PASS_TO_PASS: 2861 / 2861
resolved: false
environment fingerprint: matched
```

业务结果与 qualification oracle 一致；环境指纹与 post-setup worker toolchain 完全一致。

## 5. 验证范围

自动化回归：

```text
tests/hermes_cli/test_kanban_runtime_phase4g8.py
tests/hermes_cli/test_worker_lanes.py

97 passed
```

新增覆盖：

- evaluator setup prefix 保留 dependency hot-fix；
- setup prefix 不包含 official test command；
- cache identity 对 image/setup/environment 变化敏感；
- base/gold qualification 环境漂移会被拒绝；
- 实际 extracted toolchain 与 setup 后 container 指纹一致。

同一组回归还验证了 worker 的旁路危险操作审查：隔离 `CODEX_HOME` 固定使用
`approval_policy=on-request`、`approvals_reviewer=auto_review`、有版本/hash 的 Phase 4G8 review policy
和 `rules/default.rules` exec-policy；真实 lane 不再使用 `approval=never`；`worker_started` event 记录
实际 approval/reviewer/exec-policy 摘要。主 `.codex/config.toml` 不会被修改。

隔离配置还必须从 active model provider 白名单复制连接参数，而不是只复制 `base_url` 和 `wire_api`。
当前保留 `supports_websockets`、`stream_max_retries` 和 `websocket_connect_timeout_ms`；本次模型源对应值为
`true`、`20` 和 `8000`。由于 worker 的 `base_url` 指向 netns 内唯一可达的本地 model proxy，该 proxy
同时实现 HTTP Upgrade 和 WebSocket 双向字节流透传，再连接真实 upstream 的 `ws/wss` transport。
因此 `supports_websockets=true` 是实际可用能力，不是与 proxy 实现矛盾的表面配置。路径白名单、非特权
UID、netns 默认拒绝出口和 credential redaction 规则保持不变。

该 transport 扩展及后续 audit 的最终目标回归为 `192 passed`。其中单独覆盖：隔离 provider 精确保留
`supports_websockets=true`、`stream_max_retries=20`、`websocket_connect_timeout_ms=8000`，以及 proxy
完成 `101 Switching Protocols` 后将 client/upstream WebSocket frame 双向转发。相关 Python 文件同时
通过 Ruff、`py_compile` 和 `git diff --check`。

首次真实 Medium WS run 进一步发现：proxy 原先在 `select()` 连续 300 秒无数据时退出 relay，等价于
错误设置了 WebSocket idle TTL；长 reasoning turn 会因此被主动断流。修正后该时长只作为有限轮询周期，
idle poll 不关闭连接，只有 EOF、socket exception 或 owning process 结束才终止 relay。确定性 socketpair
测试覆盖“超过一个 idle poll 后仍可双向传输”。

真实 smoke 先确认了一个必要边界：只设置 `on-request + auto_review` 时，`danger-full-access` 下的
`sudo -n true` 没有形成 approval request，因而直接执行。Auto-review 不是全量命令拦截器。修复后：

```text
sudo -n true
  -> exec-policy: forbidden

rm -rf /root/phase4g8-nonexistent-target
  -> exec-policy: prompt
  -> codex-auto-review: declined / unacceptable risk
  -> command 未执行

pytest -q
  -> exec-policy: unmatched，按普通 worker 路径执行
```

当前 sub2api `/models` 已确认同时提供 `gpt-5.6-sol` 与 `codex-auto-review`。真实 reviewer smoke 的
`rm` 目标不存在，没有业务或 host 副作用。外层 bwrap 进一步将 node `CODEX_HOME` 中的
`config.toml`、`auth.json` 和 `rules/` 叠加只读，防止 worker 关闭审查；netns + bwrap + 非特权 UID +
实际 Dask toolchain 的组合 preflight 继续得到环境 SHA-256
`8601ded067e25620404a459f6c1ed63bb4ab2fc47fdb474d3262e8fc05415dd2`。

该审查只增加一层风险分类和软判断，无法证明所有可能的脚本语义都被识别。外层 bwrap、netns、
非特权 UID、只读 toolchain 和 protected artifact 不挂载仍是最终安全边界。

## 6. 后续验证事实

Medium 的环境等价性阻断项已解除。旧 run `phase4g8-medium-26741ac7ab` 继续作为受环境漂移影响的
历史事实保留，不能回写成成功，也不再作为当前能力基线。

2026-07-15 完成的新 run `phase4g8-medium-6b2be98f01` 已同时启用：

- post-setup worker/evaluator environment parity；
- evaluator failure 回流原 implementation node；
- 同一 Codex backend session resume；
- bounded remediation bundle；
- 3 次有效 evaluator failure budget。

新 run 的 worker 与三次 evaluator 均记录 environment SHA-256
`8601ded067e25620404a459f6c1ed63bb4ab2fc47fdb474d3262e8fc05415dd2`，package parity preflight
通过，没有再次出现 pandas 2.2.2 / 1.5.3 漂移。最终 Runtime Validation 通过，End-to-End Capability
Validation 未通过：FAIL_TO_PASS `36/44`，PASS_TO_PASS `2861/2861`。因此新失败可以归入任务质量和
failure diagnostics 收敛问题，而不再由 worker/evaluator environment mismatch 解释。

完整过程见 [Small / Medium 真实任务执行流程](small-medium-execution-flow.md) 和
[v2 前 Medium trace](dask__dask_2022.9.2_2022.10.0/phase4g8-medium-6b2be98f01/capability-trace.md)。

环境与 remediation 修复后又执行了 clean run `phase4g8-medium-223cfadfef`。该 run 的 worker 与 evaluator
继续使用相同 environment SHA-256，三轮结果为 F2P `33/44 -> 37/44 -> 39/44`、P2P 始终
`2860/2861`。因此当前任务失败仍不是 dependency drift，但出现了一个真实 P2P regression，能力结论保持
未通过。

WebSocket relay 在 clean run 中承载了超过旧 300 秒边界的长 turn。为把 transport 事实变成可审计证据，
proxy 后续增加只记录计数的 `hermes_phase4g8_model_transport_audit_v1`，不保存 URL、header、body 或凭据。
真实 provider smoke 记录：upgrade attempt `3`、101 success `3`、failure `0`、HTTP request `0`；主
`~/.codex` 配置哈希未变化。后续完整 run 会把相同 audit 写入 `run-report.json`。
