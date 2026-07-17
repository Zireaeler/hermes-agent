# Hermes Kanban Runtime Kernel Phase 4G9 Iterative

# Native Ultra Orchestra 完整 Arm 1

## 1. 目的

历史 Phase 4G9 Arm 1 只允许一次 terminal candidate 和一次 official evaluator。该 run
可以证明 native Codex ultra orchestra 确实进行了 subagent 委派、通信和共享 workspace
集成，但不能测量它在获得与 Runtime Kernel 相同的 evaluator feedback 后能否持续修复并
收敛。

本协议定义新的完整 Arm 1：一个 Codex ultra parent 在同一 root thread 中持续消费每轮
official evaluator 失败诊断，自主协调 ephemeral subagents，直到任务 resolved 或到达真实
资源/基础设施边界。

旧 one-shot run 必须保留为 preliminary evidence，不得覆盖、重命名或替换。

## 2. 比较对象

Arm 1 测量：

```text
一个 native Codex ultra root
+ proactive internal subagents
+ parent/subagent 与 subagent/subagent 通信
+ shared workspace
+ same-parent evaluator feedback loop
```

未来 Arm 2 测量：

```text
Hermes Runtime Kernel
+ 多个普通 Codex max workers
+ durable execution nodes
+ isolated candidate/worktree boundaries
+ Runtime-level integration and evidence
```

`ultra` 不是普通 worker reasoning 档位。它使用 `max` wire reasoning，并附带主动创建和
协调 native subagents 的 client policy。Arm 1 因而是单 root 的 native orchestration
system，不是普通单 agent baseline。

## 3. 冻结任务与环境

沿用已 qualification 的 DVC Large 实例：

```text
instance: iterative__dvc_1.0.0a1_1.0.0a2
base commit: fc42ca721c25bdd24875c999e37fb4f589ecd63c
dataset revision: 9b83d5af943ba7a17567336f5b18239f73960219
official image: xingyaoww/sweb.eval.x86_64.iterative_s_dvc-3760
FAIL_TO_PASS: 68
PASS_TO_PASS: 242
```

必须使用 clean detached base、全新隔离 `CODEX_HOME`、`gpt-5.6-sol`、`ultra`、
MultiAgentV2、最多 4 个同时 active threads，以及与 official image 对齐的 worker toolchain。

## 4. Iterative 执行协议

执行路径：

```text
fresh ultra parent
  -> terminal candidate 1
  -> freeze complete patch and revision
  -> official evaluator
  -> complete source-safe diagnostics
  -> codex exec resume <same-parent-thread>
  -> terminal candidate 2
  -> ...
  -> resolved or real boundary
```

强制规则：

- 第一轮必须启动 history-free parent thread；
- 后续轮次必须使用同一 parent thread ID，不得创建 fresh root 替代失败的 resume；
- parent 可以继续使用已有 subagents，也可以创建新的 ephemeral subagents；
- 每次 evaluator 前必须冻结完整 binary candidate patch 和 SHA-256；
- evaluator 只能读取冻结 candidate，不能修改 worker workspace；
- evaluator feedback 只能在对应 candidate 冻结后进入 parent；
- 每个当前 failed test 都必须有 test ID 和 bounded diagnostic；
- feedback extraction 不完整时不得把残缺诊断发送给 Codex；
- 不暴露 protected test source、gold patch、target implementation 或历史 candidate；
- evaluator feedback 不经过 Hermes Decision Provider，也不创建 Runtime nodes。

## 5. 无固定任务轮数

Arm 1 不设置 evaluator task-round 上限。以下情况本身不得终止：

- 分数一轮未提高；
- failure signature 重复；
- parent 已经运行过多个 turn；
- subagent 数量达到过上限；
- candidate 改动规模较大。

允许的 terminal boundary 只有：

- `official_resolved`；
- 显式 total wall-time resource boundary；
- 单个 worker turn 的 wall-time boundary；
- Codex process/session resume 失败；
- evaluator infrastructure 或完整 feedback extraction 持续失败；
- operator 明确终止。

未 resolved 的 resource/infrastructure 终止必须标记为 task-failed 或 infrastructure-invalid，
不能声明完整 Arm 1 capability 通过。

## 6. Candidate Lineage 与 Best Revision

每轮必须保存：

- candidate round；
- complete patch 与 SHA-256 revision；
- changed files；
- parent terminal message；
- worker mode（fresh/resume）与同一 thread 证明；
- evaluator invocation identity；
- F2P/P2P result；
- feedback coverage；
- 是否成为 best-known candidate。

Best candidate 排序顺序：

1. resolved 优先；
2. PASS_TO_PASS regressions 更少优先；
3. FAIL_TO_PASS passed 更多优先。

后续 candidate 回退时不得删除或覆盖 best-known patch。Run 结束时，Git 报告和 artifact
store 必须同时保留最终 workspace candidate 与 selected best candidate 的身份。

## 7. 持续监控与过程报告

运行期间必须持续记录并按轮检查：

- parent 与 subagent thread lifecycle；
- spawn、send_message、followup_task、wait 和嵌套 delegation；
- subagent responsibility 与 terminal summary；
- parent 如何分配、重新分配和集成工作；
- project-visible tests 与命令；
- evaluator feedback 如何被下一轮同一 parent 消费；
- candidate patch/score progression；
- compaction、cache、token、wall time 和并发；
- shared workspace 的冲突、移动目标和集成问题。

最终中文报告不能只列 worker 数量和分数。它必须还原 ultra orchestra 的执行过程，说明
哪些通信或委派改变了实现、哪些策略有效、哪里形成共同盲点，并为 Arm 2 的 Runtime node
粒度和 integration policy 提供依据。

## 8. 原始证据保留

新 run 在 cleanup 前必须 archive 并校验：

- 完整 isolated `codex-home/sessions`；
- 每轮 outer Codex JSONL 与 stderr；
- 每轮 candidate patch/metadata；
- 每次 standardized evaluator result；
- runner state 与 final report；
- credential-redacted config、transport audit 和 environment identity。

只有模型源 API key 和真实 base URL 需要 credential redaction。不得再次删除唯一 session
或 worker event 副本。

## 9. 验收标准

Infra 完成需要证明：

- initial turn 使用 fresh parent；
- evaluator failure 生成完整 feedback；
- 下一轮 argv 使用 `codex exec resume --json <same-thread-id>`；
- 不同 thread ID 被拒绝；
- 多轮 candidate 和 evaluator lineage 不覆盖；
- best revision 在后续回退时仍保留；
- 没有固定 evaluator task-round limit；
- report 区分 resolved、task-failed 和 infrastructure-invalid；
- raw evidence archive gate 通过。

真实 Arm 1 完成需要从 clean base 启动新 run，并运行到 `official_resolved` 或记录明确的真实
boundary。无论最终是否 resolved，都必须生成可读过程报告；只有 resolved 才能作为完整
native ultra capability baseline。
