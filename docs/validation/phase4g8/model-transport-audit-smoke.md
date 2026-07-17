# Phase 4G8 Model Transport Audit Smoke

## 1. 目的

验证隔离 Codex worker 实际通过 WebSocket 连接当前 `base_url + API key` 模型源，并让该事实成为
不泄露请求、响应或凭据的结构化审计结果。

## 2. 实现

Phase 4G8 model proxy 记录以下匿名计数：

```text
http_request_count
websocket_upgrade_attempt_count
websocket_101_count
websocket_failure_count
```

计数器不保存 URL、query、header、body、API key、响应正文或模型输出。完整 Phase 4G8 run 会把
`hermes_phase4g8_model_transport_audit_v1` 同时写入顶层 `run-report.json` 和 run metrics。

## 3. 真实验证

2026-07-15 使用全新临时目录完成一次真实 provider smoke：

- 从主 Codex 配置只读取 active provider/model/credential，生成隔离 `CODEX_HOME`；
- 隔离配置包含 `supports_websockets=true`、`stream_max_retries=20`、
  `websocket_connect_timeout_ms=8000`；
- worker 位于无默认出口的 network namespace，只能访问本地 model proxy；
- Codex 执行一个无工具短 turn；
- 主 `~/.codex` 配置在运行前后哈希一致；
- 临时目录和 network namespace 在验证后删除。

结构化结果：

```json
{
  "returncode": 0,
  "event_types": [
    "item.completed",
    "thread.started",
    "turn.completed",
    "turn.started"
  ],
  "source_config_unchanged": true,
  "transport_audit": {
    "schema": "hermes_phase4g8_model_transport_audit_v1",
    "http_request_count": 0,
    "websocket_upgrade_attempt_count": 3,
    "websocket_101_count": 3,
    "websocket_failure_count": 0
  }
}
```

该 smoke 证明当前 provider transport 实际完成 WebSocket upgrade，没有退回 HTTP。它不证明 Medium
benchmark resolved；任务能力结论仍以 independent official evaluator 为准。

## 4. 回归

确定性测试覆盖：

- `101 Switching Protocols` 后 frame 双向转发；
- 多个 idle poll 后 relay 仍保持可用；
- audit 对成功 upgrade 计数且不包含敏感字段；
- 隔离 provider 配置保留三个 WebSocket 参数。

Phase 4G8 三个目标测试文件合计 `192 passed`，相关 Python 文件通过 Ruff、`py_compile` 和
`git diff --check`。
