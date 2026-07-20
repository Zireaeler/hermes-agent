# Phase 4G15 Evidence-Driven Live Orchestra 验证报告

## 1. 验证目标

验证以下最小闭环，而不是再次运行 hard benchmark：

```text
真实新证据出现
    -> Runtime 更新全局状态
    -> 当前 active worker turn 收到责任变更
    -> worker 避免完成过时工作
    -> canonical receipt ACK
    -> 最终质量不低于 coherent single worker
    -> run 过程被分析、吸收并进入受控改进生命周期
```

本实验没有使用 official evaluator，也没有证明多 worker 对所有复杂任务都有净正价值。固定质量脚本只
用于比较同一受控任务的最终结果。

## 2. 实现范围

- 新增 `codex_app_server` worker transport，复用原生 Codex thread/session。
- 新增 DB-authoritative `runtime_active_worker_turns`，记录 materialization、task/run、thread/turn 和生命周期。
- running target 的 live-safe directive 使用 `turn/steer`；accepted 只代表 transport 接受，receipt ACK
  才代表 worker 已消费。
- 新 directive 在 active turn 注册后出现时，自动绑定当前 thread/turn；turn 结束后的 late directive
  留在 durable path。
- 每个受管验证 run 必须声明 marker，并生成 JSON learning bundle、中文时间线和 registry receipt。
- archive/cleanup 会自动检查 marker 对应的 learning gate，调用方不能通过漏传参数绕过。
- candidate promotion 需要 baseline/treatment、质量不回归、目标指标改善和明确批准，不自动修改 policy。

## 3. 对照任务

两个 arm 使用相同模型 `gpt-5.6-sol`、相同指令、相同脚本和相同最终检查。

Consumer 先运行一次 `prepare_consumer.py`，捕获启动时的 `legacy-v1` 并等待 6 秒。Producer 在看到
consumer marker 后将共享契约更新为 `contract-v2`。

Baseline 不接收 Runtime live update。首 turn 按捕获值写入 `legacy-v1`，固定质量检查失败；同一
Codex thread 的第二 turn 修正为 `contract-v2`。

Treatment 在 producer checkpoint 后由 Runtime 向 consumer 当前 turn 发布 `stop_obsolete_work`，要求
停止 v1 路径并完成 v2。

## 4. 权威时间线

通过 run：`hermes-phase4g15-live-20260720-184812`

```text
1784544606  Runtime job 创建
1784544616  producer 发布 contract-v2 checkpoint
1784544616  live directive 入队并绑定当前 active turn
1784544616  turn/steer accepted
1784544646  worker receipt ACK directive
1784544646  consumer terminal，固定质量检查通过
```

Treatment receipt 明确包含：

```json
{
  "observed_contract": "contract-v2",
  "result_value": "contract-v2",
  "consumed_directive_ids": ["rdir_a4fc65e65b54"],
  "status": "completed"
}
```

## 5. 对照结果

| 指标 | Baseline | Treatment |
|---|---:|---:|
| 首 turn 最终质量通过 | 否 | 是 |
| 同 thread 恢复 turn | 1 | 0 |
| live directive accepted | 0 | 1 |
| terminal 前 ACK | 0 | 1 |
| 未避免的过时工作 | 1 | 0 |
| 最终质量 | 通过 | 通过 |

Treatment 没有通过新建 worker 或事后 evaluator loop 修正结果。责任变化发生在 consumer 原 active
turn 内，最终质量与 coherent baseline 相同，并少一次恢复 turn。

## 6. 失败过程如何转化为系统改进

第一次真正进入 treatment 的失败 run：`hermes-phase4g15-live-20260720-183854`。

它揭示原实现只支持：

```text
directive 先入队 -> turn 后注册
```

而不支持本阶段真正需要的时序：

```text
turn 先注册 -> 执行中出现新证据 -> directive 后入队
```

失败 run 中 delivery 一直为 `pending`，没有 thread/turn identity，consumer 最终仍写入 `legacy-v1`。
修复后新增 active-turn 表，并把回归测试改为后一种真实时序。

旧 artifact 经新 analyzer 重放后形成：

- finding：`live_delivery_unresolved`；
- candidate：`candidate_88540731826e3240c8e6`；
- baseline 指标：质量 0，未避免过时工作 1；
- treatment 指标：质量 1，未避免过时工作 0；
- promotion：`phase4g15-controlled-validation` 明确批准。

Registry promotion 只表示该修复已经过证据闭环，不会自动改写 provider prompt、validator 或代码。

## 7. Artifact

通过 run：

```text
/root/hermes-validation-artifacts/phase4g15/controlled-live-directive/
  hermes-phase4g15-live-20260720-184812/
```

失败 run：

```text
/root/hermes-validation-artifacts/phase4g15/controlled-live-directive/
  hermes-phase4g15-live-20260720-183854/
```

学习重放与 promotion：

```text
/root/hermes-validation-artifacts/phase4g15/controlled-live-directive-regression-replay/
  hermes-phase4g15-learning-replay-20260720-185505/
```

通过 run 的 manifest 保存 authoritative Kanban DB、两个 Codex session JSONL、中文报告、learning JSON
和 registry receipt；API key/base URL 未进入 archive。临时 workspace 和 Codex cache 在 manifest
验证后删除。

## 8. 结论与边界

本阶段已经证明：Runtime 可以在真实新证据出现后、target terminal 前改变仍在执行的责任，worker
能够在同一 Codex turn 内消费该变化，减少一次确定性的过时工作和恢复 turn，同时最终质量不回归。

它尚未证明：

- 任意自然 Medium/Large 任务都会产生值得 live steer 的结构事件；
- system-level orchestra 的最终质量普遍高于 native Codex internal subagents；
- active app-server 进程崩溃后可以恢复同一个未完成 turn；
- provider 能稳定从开放式工程事件中选择最佳 action。

下一阶段应使用自然任务验证 event precision 和净收益，而不是增加 dashboard 或继续堆 evaluator 门禁。
