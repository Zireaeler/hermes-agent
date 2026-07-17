# Phase 4G9 Iterative Arm 1 运行监控记录

## Round 1：初始分工

时间：2026-07-17 17:11-17:13 CST

- Parent thread：`019f6f58-25b4-76d3-a491-ffb9b5d3e69c`。
- Parent 先检查 clean base、repository tree、`setup.py` 和 worker toolchain，再决定分工；
  harness 没有预设 agent topology。
- Parent 将 SRS 归为三个低重叠 cluster，并创建 3 个 depth-1 subagent：
  - `plots`（Bohr）：plot CLI、plot metadata、rendering defaults；
  - `diff_misc`（Curie）：diff/Markdown、template newline、logging、Python compatibility、
    S3 config、stage-name validation；
  - `tree_remote`（Nietzsche）：tree/remote/import/update/run-cache。
- Parent 自己声明负责 stage/run/repro/run-cache、跨领域集成和最终验证。
- 四个实现线程填满 Codex ultra 默认并发上限。

### 初始架构观察

- 分工按代码与反馈耦合度形成，不是 planner/coder/tester 角色拆分。
- Parent 同时承担实现和 integration owner，不是纯 scheduler。
- `run-cache` 同时出现在 parent 与 `tree_remote` 的责任声明中，存在共享 workspace 下的
  ownership overlap。后续需要观察它是否通过通信协调、自然避让，或导致重复/冲突修改。
- 当前只记录可观察 agent message、tool event 和 workspace fact，不推断隐藏 reasoning。

## Round 1：共享 workspace 协调与责任调整

时间：2026-07-17 17:17-17:21 CST

- 四个 session 同时写入同一个 workspace。外层 stderr 已出现多次 `apply_patch`
  preimage 不匹配，涉及 `istextfile.py`、`output/__init__.py`、`schema.py` 和 shell
  completion；这些失败发生时目标文件已被其他并发工作修改。
- 可观察到的处理方式不是覆盖并发修改，而是重新读取现状后继续。`diff_misc` 明确报告：
  plotting change 同时触及 template emitter，形成重复 newline；它检查共享 diff 后将结果
  收敛为单一 terminator，同时保留另一个 agent 的 plot-scale 改动。
- Parent 在初始分工后向 `diff_misc` 发送了新的运行中消息；后者随后说明正在接手并从本地
  tests/code 追踪 `#3588`。这表明责任可在执行中动态调整，不局限于启动时的一次性任务包。
- Parent 自己持续推进 stage/run/repro 语义，先从现有测试确认 commit prompt、dry-run side
  effect 和 uncached output，再修改实现；当前仍是实现与集成 owner，而不是只等待子 agent。
- 当前 workspace 已同时出现 plotting、diff/config/compatibility、tree/update 和 stage/run 等
  多域修改。尚未看到完整集成测试或 evaluator，因此暂不能判断并发收益是否超过冲突成本。

## Round 1：子任务收敛与初次集成

时间：2026-07-17 17:24-17:30 CST

- Parent 报告第一轮跨域集成已形成，并明确提到 delegated review 发现并修正了 uncached
  output 恢复 guard。这是 subagent 输出影响 parent 实现决策的直接证据，不只是并行产出。
- `diff_misc` 完成其 cluster 并给出可核对的验证摘要：聚焦 integration suite `86 passed`，
  额外 targeted checks `13 passed`，同时运行 flake8、compileall、completion syntax、CLI
  parsing 和 `git diff --check`。它识别到一个由共享 merged-output schema 引起的旧
  `outs_persist` expectation failure，并将其明确交给 parent 处理，没有自行覆盖跨域设计。
- `plots` 在实现 canonical `.dvc/plots` 时主动保留 legacy `.dvc/plot` 读取路径，说明 agent
  不只机械改名，也在处理升级兼容性；随后继续运行 command/functional tests。
- `tree_remote` 已让 historical file/directory run-cache transfer 等 focused behaviors 通过，
  正在扩大到 API read、tree、update/import CLI、remote config 和 data-sync wiring 的集成验证。
- Parent 自己的 stage/run/repro/run-cache 第一轮修改已完成，开始运行 focused regression；
  当前协作结构仍是三个 domain owner 加一个 parent implementer/integrator。

### Session 计数说明

- 17:31 出现的第 5 个 Codex session 不是 worker/subagent，而是只读 `auto_review`
  approval reviewer。它只审查 parent 删除 workspace 内 pytest 临时目录的危险操作请求，并
  返回 `allow`。
- 因此本轮可计入 orchestra 的执行 session 仍为 `1 parent + 3 subagents`；approval reviewer
  只属于 capability/audit lane，不应被计入任务分解或 worker 并发指标。
- 此次审查证明危险操作采用旁路自动审查：parent 没有获得宽泛权限，也没有等待人工输入；
  reviewer 根据 cwd 和精确删除目标批准了清理测试垃圾。

## Round 1：从并行实现切换到独立审计

时间：2026-07-17 17:31-17:33 CST

- 三个初始 implementation subagent 均已 terminal，并把 changed areas、测试结果、环境噪声和
  剩余跨域风险交回 parent。Parent 没有直接结束，而是逐项检查 shared diff、兼容路径和
  release version，并清理会污染 repository discovery 的 pytest 临时目录。
- `plots` 最终报告的 focused coverage 包括 `19 + 22 + 14 + 13 + 1` 个通过用例；
  `tree_remote` 报告 `66 + 4 + 2 + 2` 个聚焦通过，以及 `91 passed / 30 skipped` 的
  broader integration。这里记录的是 worker-visible tests，不等价于 official evaluator 结果。
- Parent 在三个 implementation agent 结束、并发槽释放后，最终启动三个独立审计责任：
  1. stage/run-cache semantics；
  2. consolidated output/plot schema；
  3. requirement-by-requirement audit。
  Parent 自己继续做 broad test environment 和最终集成。公开状态消息最初只说“两个独立
  audit，并由 parent 做 checklist”，但随后可观察 agent topology 显示 checklist 也被委派给
  `requirements_audit`；最终事实应以实际 session/task 为准。
- 这形成了动态两阶段 topology：`并行 domain implementation -> parent integration -> independent
  audit -> broad verification`。审计不是预先固定的 runtime node，而是 parent 根据集成状态
  在运行中创建；后续需观察审计是否发现实际缺陷，以及修复由谁承担。
- 截至此时，生命周期累计 task subagent 为 6 个（3 implementation + 3 audit），但两批不
  同时运行；峰值并发始终是 `1 parent + 3 child`。另有 1 个 approval reviewer，不计入 task
  subagent。

## Round 1：Native compaction 后继续集成

时间：2026-07-17 17:34-17:39 CST

- Parent 在同一 thread `019f6f58-25b4-76d3-a491-ffb9b5d3e69c` 内触发 native Codex
  compaction。可观察行为是先生成完整 handoff summary，随后声明从 preserved implementation
  state 继续；workspace、已有修改、plan 和三个 active audit agent 都没有丢失或重建。
- 这次 compaction 属于 native worker 内部上下文维护，不是 Phase 4G9 harness 的
  candidate/evaluator round，也没有产生新的 runtime worker。后续统计必须与 official
  evaluator 反馈后的 `codex exec resume` 分开。
- Compaction 后 parent 立即启动 pinned environment unit suite：结果为 `427 passed`，另有
  2 个 missing-`pyarrow` environment failures 和 3 个 renamed-internal expectations。Parent
  没有把这些统一视为产品缺陷，而是把 stage-cache/public-call mismatch 交给 `stage_audit`
  根据 locking/refactor semantics 判定。
- `schema_audit` 已证明并修正两个问题：stage name 校验需要 true full match；非字符串 API
  name 应转为 `InvalidStageName`。它还把 visible multistage expectation 更新为 canonical
  nested `persist` form，并继续扩大 schema tests。
- `requirements_audit` 发现 directory run-cache transfer 的潜在缺陷：two-pass pull 可能在
  manifest 下载前缓存空目录信息，导致第二次 collection 漏掉 child objects；正在直接复现。
- `stage_audit` 则识别两个高风险点：run-cache restoration 绕过 public checkout，以及
  no-cache cache path 会暂时修改 output state；正在通过完整 commit/repro/stage-cache suite
  判断是否为真实回归。

## Round 1：独立审计发现并修复真实缺陷

时间：2026-07-17 17:40-17:55 CST

- `schema_audit` 完成并修复：
  - stage name 必须 true full match，并拒绝非字符串 name；
  - trailing-newline stage name 不再通过 schema；
  - canonical nested output schema 增加 exact round-trip、removed legacy groups、malformed
    mapping、`cache: false + persist: true` 等边界测试。
  它的 broader audit 为 `53 passed`，并把 `_checkout()` spy mismatch 明确转交
  `stage_audit`，没有擅自恢复旧 public-call 结构。
- `stage_audit` 证明并修复三个产品语义问题：
  - `--no-commit` 下 uncached outputs 仍被写入 private cache object；
  - 多输出 run-cache restore 在发现后续缺失对象前可能已经部分恢复，破坏 fallback 原子性；
  - hardlink/symlink 模式恢复 uncached outputs 后未正确 unprotect，可能反向污染 private cache。
  它同时吸收 `requirements_audit` 的 independent partial-restore finding。最终聚焦结果为
  `131 passed / 5 skipped`，run-cache push/pull `2 passed`。
- `requirements_audit` 另发现 Bash completion 仍公开不存在的 `dvc diff -t`，而 Zsh 已正确；
  已移除 stale flag。
- Parent 的完整 unit suite 结果为 `446 passed / 9 skipped`。仅有 2 个 HDFS tests 因 pinned
  toolchain 明确缺少 optional `pyarrow` 而失败，暂按环境问题隔离，不冒充 product-clean full
  collection。Parent 随后启动完整 functional suite。
- Schema audit 结束后，parent 利用空出的并发槽创建新的 independent tree/remote review，重点
  检查 stream ownership、file-object hashing 和 run-cache object transfer。因此生命周期累计
  task subagent 至少增至 7 个，但峰值仍为 3 child。

## Round 1：Broad functional run 与 transport 阻塞

时间：2026-07-17 17:48-17:58 CST

- 完整 `tests/func` 运行结果为 `728 passed / 56 skipped / 24 failed`，耗时 204.51 秒。
  失败集中在 analytics/API/root discovery/import/get/install/status/update/version 等依赖“测试临时
  repo 不应向上发现其他 DVC repo”的场景，以及 2 个 HDFS optional dependency case。因为
  basetemp 位于 benchmark workspace 内，而 workspace 自身带 tracked `.dvc/config`，其中大部
  分具有 ancestor-repository pollution 特征；但必须逐项复跑/分类，不能仅凭名称把 24 个失败
  全部标为环境噪声。
- Functional command 完成后，parent provider stream 连续报告 WebSocket reconnect，当前达到
  `11/20`，外层 event stream 暂停更新；runner、Codex wrapper、native Codex 均仍存活，harness
  没有重启或替换 parent。
- Transport 阻塞期间，已启动的 subagent session 仍继续运行并写入独立 session JSONL。
  `requirements_audit` 已完成 changed unit `136 passed`、changed functional `120 passed`、
  run-cache transfer `2 passed`、stage/dry subset `8 passed`，并确认没有已知遗漏 SRS behavior。
- `tree_final_audit` 发现历史树判型缺陷：当前 workspace 路径为目录、但目标 Git revision 中同一
  DVC output 为文件时，remote open 错用 workspace filesystem 判断并抛 `IsADirectoryError`；
  应以 output checksum metadata 为权威。它还发现 missing checksum guard 需要在
  `DvcTree.open()` 内更早执行，避免泄漏 `AttributeError`，正在补回归测试。
- 这一段展示了 native orchestra 的一项韧性和一项边界：child execution 不依赖 parent 的当前
  provider stream，可在 parent 重连时继续；但 findings 的最终验收、broad failure triage 和
  terminal candidate 仍必须等 parent 恢复后完成。

### Transport 恢复与失败分类

- Parent 在保持同一 process/thread/workspace 的情况下从连续重连恢复，没有重新开始 turn，
  随后成功接收 `stage_audit`、`requirements_audit`、`tree_final_audit` 的消息和 final answer。
- Parent 对 24 个 functional failures 的分类为：22 个来自 workspace 下 basetemp 向上发现
  tracked `.git/.dvc` 以及 active audit artifacts 的环境污染；剩余 2 个 multistage repro
  failures 被单独复跑。
- 两个单独复跑的 case 使用 `.` 和 `?` 作为 stage name。新 SRS 明确禁止 punctuation，因此它们
  是旧 fixture 与新 contract 冲突，不是产品回归。Parent 将 fixture 改为合法等价名称，并扫描
  其他 punctuation-bearing stage fixtures；没有为了旧测试放宽新 validator。
- `tree_final_audit` 最终交付并通过 `72` 个 focused unit、`36` 个 focused functional 及额外
  tree/run-cache subsets；missing-checksum 和 historical file/directory type 两项修复均已进入
  shared candidate。

## Round 1：最终验证准备

时间：2026-07-17 18:01-18:05 CST

- Parent 宣布四个 independent audits（schema、stage/run-cache、requirements、tree/remote）全部
  complete，当前 SRS checklist 无已知 behavioral gap。
- Parent 精确列出并删除 audit/pytest/broad-run 生成目录；删除目标均位于当前 workspace，经过
  auto-review lane，未删除 tracked root `.dvc`、source、session 或 harness evidence。
- 为验证 22 个 ancestor pollution failures，parent 计划在恢复保护下临时隐藏 checkout 根
  `.git/.dvc` markers，运行 broad suite 后立即恢复。此操作的有效性不仅取决于测试结果，还
  必须验证 marker 在成功、失败或中断路径上都恢复；监控将检查 workspace Git/DVC root。

### 隔离验证结果与 marker 恢复

- 在 checkout root markers 隐藏且排除 HDFS optional cases 后，functional main sweep 为
  `749 passed / 47 skipped / 11 deselected / 2 failed`。先前 ancestor-discovery failures 全部
  消失，证明其环境分类成立。
- 剩余两个 install-hook failures 来自 pinned toolchain 的 `dvc` launcher 使用不存在的
  `/opt/miniconda3/envs/testbed/bin/python` shebang。Workspace-local wrapper 首次绕过 shebang
  后仍因新 hook process 未带 `fractions.gcd` shim 失败；parent 根据完整 stderr 修正 wrapper，
  第二次得到 `2 passed`。没有修改产品代码来适配测试机 launcher。
- Parent 在 markers 隐藏时误跑了一次 unit suite，得到 `434 passed / 18 failed`；这与正常根
  环境中 `446 passed` 冲突，说明隐藏 root markers 不适用于 unit suite。Parent 没有据此修改
  产品代码，随后恢复 `.dvc`、`.git`。
- 18:11 CST 已核对：`.git` 与 `.dvc` 均恢复到原路径，`.git-verification-hold`、
  `.dvc-verification-hold`、`.verify-bin`、`.verify-isolated` 均不存在。恢复成功，但分步 move
  缺少单 shell trap 仍是 test-infra 可改进点。
- Functional 最终证据应准确表述为：main sweep `749 passed`，两个 hook case 经 corrected test
  launcher 单独 `2 passed`；不是同一次命令的 `751 passed`。

## Round 1：第二次 native compaction 与重审成本

时间：2026-07-17 18:12-18:16 CST

- 最终正常 marker 环境 unit suite 为 `452 passed / 9 skipped / 4 HDFS deselected`；
  completion Bash syntax、`git diff --check`、`compileall` 和 version check 全部通过。
- Parent 在准备 terminal response 时再次遇到 WebSocket reconnect，随后在同一 thread 内触发第
  二次 native compaction。Handoff 保留了 72-file scope、全部 audit fixes、测试总数、环境限制
  与剩余步骤，未丢失责任边界。
- 新 context 没有直接信任 handoff 后终止，而是重新读取 schema/serialization、stage/output、
  tree/data-cloud、run-cache/repro、plots 等大块 diff，并重建 high-level requirement mapping。
- 这提供了一项重要对照事实：native compaction 增强了长任务可继续性，但会产生明显的
  context reacquisition、重复审查和额外 provider round 成本。重复审查可能提高最终质量，
  但不能在报告中把它误记为新的 durable responsibility/node；它仍是同一 parent session 的
  内部恢复循环。

### 第二次 compaction 后的约束漂移

- 最终 integrator pass 修复 4 个 plural CLI rename 导致的 overlong-line style regressions；随后
  changed-file lint 与 targeted `139` tests 通过，说明 parent-level merged-diff lint 捕获了局部
  agent lint 未覆盖的问题。
- Parent 为最终 broad verification 使用 `/tmp/dvc-final-unit` 与 `/tmp/dvc-final-func` 作为
  basetemp。这样技术上正确解决了 ancestor repository pollution，unit 得到 `452 passed / 9
  skipped / 4 deselected`。
- 但 Arm 1 初始 execution contract 明确写有“不修改 workspace 外文件”。第二次 compaction
  handoff 没有重申这条约束，新 context 因而在 `/tmp` 写入 test artifacts。该行为没有读取
  gold、protected evaluator data 或外部网络，也没有污染 candidate product tree，但仍是可证明
  的 contract deviation。
- 这比单纯 context reacquisition 成本更重要：native compaction 不仅可能让 parent 重做审查，
  还可能在压缩摘要遗漏约束时产生行为漂移。最终架构结论需把它与 Runtime Kernel 的 stable
  contract re-injection 能力对照。测试结束后必须核对并清理这两个 `/tmp` basetemp。

### 重复审查继续产生质量收益

- 第二次 compaction 后的 parent 审查并非纯重复：除了 4 个 lint regression 和 plural root
  exports 完整性外，它又发现一个此前 implementation/audit tests 都未覆盖的 tree 组合缺陷。
- 已有实现分别支持 historical read streaming 和 working-tree pull fallback，但 historical
  revision 在 remote 不支持 stream、必须 fallback pull 时，代码仍会对 active Git tree 做错误
  assertion。Parent 已修改该路径并添加 focused regression。
- 因此 native re-review 的结论不能简单写成浪费：它同时带来显著 context reacquisition 成本、
  一次 contract drift，以及至少一个真实深层缺陷修复。Arm 2 对照应比较净收益，而不是只比较
  worker/node 数。
- 随后的 requirement-by-requirement recheck 又推翻了“全部 SRS 已覆盖”的先前 audit 结论：
  `stage: hide unwanted warnings` 实际未实现，普通 dependency/output change 仍由
  `Stage.changed()` 发 warning。Parent 只将 generic message 降到 debug，同时保留 edited
  stage file/command 的 specific warning，并补双向测试。
- 这表明 native parent 的最终全局审查具有不可替代性：domain audits 可以提供高覆盖和缺陷发现，
  但仍可能共同漏掉跨实现/需求语义的 item。最终报告不能把 subagent “no gap” 当作权威事实，
  应以 parent integrated evidence 和 official evaluator 为准。

## Round 1：Official evaluator 结果

时间：2026-07-17 18:31-18:35 CST

- Candidate 1 冻结信息：
  - revision：`patch-sha256:1884181bbf6f3880bf4afc373efe8cd087b5031785372a889c264049a66ac673`；
  - patch bytes：`119726`；
  - changed files：`76`；
  - worker wall time：`4814.212s`；
  - parent worker return code：`0`。
- Official evaluator：
  - FAIL_TO_PASS：`14/68`；
  - PASS_TO_PASS：`242/242`；
  - 未解决 case：`54`；
  - feedback coverage：`54/54`，`current_failure_complete`，无 missing/truncation。
- 这与 worker-visible `455 unit passed`、`749 functional passed + 2 launcher-adjusted hook
  passed` 形成强烈反差。P2P 全绿证明 candidate 没有破坏旧 protected behavior，但 F2P 只完成
  约 20.6%，说明 parent/subagents 对目标 release 的 API/模块/重构语义仍有大量偏差。
- 有具体诊断的代表性偏差包括：
  - command run contract 仍期望 `run_cache=True`，candidate 改为 `ignore_run_cache=False`；
  - target Stage API 仍要求 `compute_md5`；
  - recursive update 的调用/聚合形态不符；
  - plots/diff namespace 与异常语义不符。
  另有大批 utils/fs、plots、diff cases 只有 exact test id，要求在 parity environment 精确复跑。
- 结论：本地 broad green 只能证明 candidate 内部自洽和 P2P 稳定，不能证明目标演进实现正确。
  本次 iterative protocol 的 evaluator feedback round 是必要的，而非形式门禁。

## Round 2：同一 Parent Resume

时间：2026-07-17 18:35 CST 起

- Harness 通过 `codex exec resume --json 019f6f58-25b4-76d3-a491-ffb9b5d3e69c -`
  继续同一 parent thread；`active-turn.json` 为 `candidate_round=2`、`mode=resume`。
- Round 2 启动后 provider stream 再次连续断开，当前已重连到 `10/20`，尚无新的 parent
  implementation event。Evaluator feedback 已完整写入 runner state，不依赖当前 transport 内存。

### Round 2 权限阻断与空转

- WebSocket 恢复后 parent 正确识别 failure 不是 54 个孤立 bug，而是 target release surface
  形态整体偏差，并计划分为 plots、stage/run CLI、SCM/filesystem 三个并行 cluster。
- 但 round 1 evaluator 后 workspace 被重建为 `root:root`，普通文件 `0600`、目录 `0700`；
  Codex worker（UID 65534）无法进入仓库。Parent 尝试 `chmod 777`、`chmod 755` 均被
  auto-review 拒绝，最终 terminal response 请求人工授权，未产生 source change。
- 外层监控根据用户已授予的完整权限恢复：workspace 全树 owner 为 `nobody:nogroup`，目录
  `0755`，普通 tracked/untracked source `0644`，Git-index `100755` 文件恢复 executable mode；
  并以 `nobody` 身份验证源码可读写、Git status 可运行。
- Runner loop 没有 duplicate-candidate stop，因此 round 2 会被 evaluator 评为同分后继续
  round 3；这是一轮 infra-induced 空转，不能计为模型 no-progress 或真实能力边界。
- 代码级根因：`_reclaim_workspace()` 只在 worker turn 后、freeze candidate 前执行；evaluator
  之后没有再次 reclaim，所以下一轮 resume 先看到 evaluator 遗留的 root ownership/mode。
  当前进程需由监控补救；实测完成后 infra 必须在 evaluator 后增加 reclaim 和回归测试。

## Round 3：Official Feedback 修复分工

时间：2026-07-17 18:47 CST 起

- Round 2 evaluator 确认 duplicate candidate 仍为 `14/68 F2P`、`242/242 P2P`，runner 按协议
  自动继续 round 3，没有把 duplicate score 当作终止边界。
- 同一 parent thread 重新创建三个实际存在的 child session：
  - `/root/round3_plots`；
  - `/root/round3_stage_cli`；
  - `/root/round3_scm_fs`。
  Round 2 中只“计划”但未创建的 agent path 不可复用；parent 最初向
  `/root/round2_scm_fs` 发消息得到 `live agent path not found` 后才纠正。
- Parent 自己读取 `dvc/utils/fs.py` 与 official failures，继续作为 integration owner；
  `stage_cli` 已识别目标 release 要求 positive `run_cache` flag 与独立
  `force_downstream` control，计划在 public boundary 对齐并保留内部兼容；`scm_fs` 正处理
  no-commit 与 filesystem utility/compatibility failures。
- Resume 初始 shell cwd 一度不是 workspace，导致 `.git` 存在但 `git status` 报 not a repo；
  后续 tool call 已回到正确 workspace。说明 native thread continuity 不等于每次 turn 的 shell
  execution cwd 一定正确，worker仍需运行时自检。
- 三个新 child 的 session 前部包含大量旧 handoff、旧 final response 和权限阻断消息，再接收
  cluster NEW_TASK；不是仅注入精简责任。这形成显著 context duplication/noise，可能削弱
  cache/token 效率。最终报告需与 runtime node 的显式最小 context contract 对照。

### Round 3 集成、验证与 Candidate 3

- 三个 cluster 最终全部回到同一 parent 集成。可观察的 focused evidence 为：plots 51 passed；
  run/repro/update 124 passed、5 skipped；SCM/filesystem/diff 37 passed、1 platform skip。
- Parent 随后运行 whole-unit，最终为 449 passed、9 skipped；whole-functional 为 749 passed、
  47 skipped、2 failed。两个失败均为隔离 toolchain 中 stale console launcher 导致的 Git hook
  executable failure，而非 candidate source behavior；这一判断仍不替代 official evaluator。
- Parent 在 round 3 再次把 pytest basetemp 写入 workspace 外的 `/tmp/dvc-phase4g9-*`。这证明
  round 1 第二次 compaction 后发生的 execution-constraint drift 没有在后续 resume 中自动恢复；
  测试结束后必须清理这些目录，并在架构比较中计入 native long-thread 的约束重注入缺口。
- Candidate 3 于 2026-07-17 19:15 CST 冻结：
  `patch-sha256:08f89b7f06bc871f4100b4bf684359c00c0c4acbbe7c36111e9abf1911c650ac`，
  patch bytes 为 140840。

## Round 3：Official evaluator 结果

时间：2026-07-17 19:15-19:20 CST

- FAIL_TO_PASS：`54/68`，较 round 1/2 的 `14/68` 新增解决 40 条；
- PASS_TO_PASS：`241/242`，出现 1 条 path-conversion regression；
- 剩余 15 条失败全部进入 feedback，coverage 为 `15/15`，无 truncation 或 missing test id；
- 主要剩余 cluster 已收敛为 plots exact signatures/parser、command diff rendering、Stage dry/path
  conversion，而不再是 round 1 的大范围 release-surface 缺失。

这一轮是 iterative native-ultra 的首个强正向能力证据：同一 parent 在接收完整 official feedback
后，借助三个临时 subagent cluster 与 parent integration review，将 F2P 从 20.6% 提升到 79.4%。
同时 P2P 从全绿降为 241/242，说明快速大范围兼容改造仍需要下一轮独立回归校正。

## Round 4：剩余失败收敛

时间：2026-07-17 19:20 CST 起

- 同一 parent thread 自动 resume，将剩余问题重新划分为 plots、command diff、Stage 三个审计责任；
- evaluator 后 workspace 再次变为 `root:root`、source file `0644`，worker 可读但不可写，并首先
  遇到 Git dubious ownership。外层监控立即恢复为 `nobody:nogroup`、目录 `0755`、普通文件
  `0644`、Git-index executable `0755`，并以 worker UID 验证 Git 与源码写权限；
- 该问题再次证明 evaluator 后 reclaim 必须进入 runner loop，而不能依赖监控人工补救。

## Round 4：结果与历史 artifact 污染

- Candidate 4 official evaluator 为 `57/68 F2P`、`241/242 P2P`：相对 Candidate 3 只新增
  解决 3 条，path-conversion regression 未恢复。Round 4 大量本地 parity probe 虽然 green，
  但对 target output/argument contract 的推断仍不准确。
- Parent 在 Round 4 执行 `rg ... . /tmp`，读到本次冻结实验外的
  `/tmp/phase4g9-arm1-finalize.json`（2026-07-17 13:23 CST，由旧 one-shot Arm 运行留下）和
  `/tmp/py36-review.diff`。未发现读取 protected test source、gold patch 或 credential；旧 JSON
  主要包含此前 official evaluator diagnostics，其中大部分与本轮正常回流反馈重叠。
- 尽管没有直接泄露 oracle source，这仍是 frozen-arm isolation violation：worker 不应通过全局
  `/tmp` 获得旧实验诊断。当前 run 保留用于观察 native 行为，但最终能力结论必须标记为
  historical-artifact-contaminated；下次运行前需清理或 mount-isolate 外部 artifact。
- Round 4 之后 evaluator 再次把 workspace 变为 `root:root`；监控按相同规则恢复并验证 worker
  写权限。这是 evaluator-after-reclaim bug 的第二次复现（不计 Round 2 的首次阻断）。

## Round 5：收敛但未完成

- Round 5 根据更精确 feedback 修正：plots targets list shape、`working tree` label、Stage dry-run
  不进入 `_run`、Windows backslash path normalization，以及 target-style diff table。
- 本地 focused 为 `85/85`，unit 为 453 passed、9 skipped，functional 在排除已确认的 HDFS/
  stale launcher 环境项后通过；official evaluator 仍仅达到 `60/68 F2P`、`241/242 P2P`。
- 从 Round 3 到 Round 5 的 official progression 为：`54 -> 57 -> 60` F2P，说明 feedback loop
  持续有正进展，但后期每轮只净解决 3 条，边际收益明显下降；P2P regression 连续三轮未恢复。
- Round 6 继续同一 parent thread 时再次出现 long-thread/multiple-compactions accuracy warning。
  Parent 因三种 materially different diff/stage 实现仍得到相同失败，决定重新分配独立根因审计。

## Round 6 至 7：从 8 条收敛到 5 条

- Round 6 将 plots 的额外 kwargs、diff 表格/hash/change contract、Stage dry-run 和 working-directory
  conversion 分给独立责任，并由 parent 重新集成。Official 结果为 `62/68 F2P`、`241/242 P2P`。
- Round 7 进一步增加 lazy `Repo.diff` forwarding、split stage compatibility module 和 mutation-free
  dry-run path。Official 结果为 `63/68 F2P`、`241/242 P2P`。
- 从 Round 7 开始，全部剩余 F2P 已固定为 5 条 `tests/unit/command/test_diff.py` case。此前跨 plots、
  stage、filesystem、SCM、run/repro 的大范围 gap 已基本消失。

## Round 8 至 9：重构调用边界但分数不变

- Round 8 尝试保持 named revisions、增加 target-era mock seams，并把 `resolve_wdir` 放回 canonical
  module。Focused diff 和 stage suites 全绿，但 official 仍为 `63/68`、`241/242`。
- Round 9 反向恢复 baseline command diff 行为，并重建 canonical stage implementation。Worker-visible
  unit 为 459 passed，focused functional 为 96 passed，official 失败集合仍完全相同。
- 这两轮说明剩余失败不是普通代码遗漏。Parent 已经开始在缺少 protected assertion 的条件下反复
  猜测精确 call shape 与 presentation contract。

## Round 10：恢复全部 PASS_TO_PASS

- Parent 将 diff 改为 alpha2-style change table、raw JSON hashes、full hash transitions、named revisions
  和 lazy module dispatch，同时修正 shallow checkout 的 `../..` serialization。
- Official 结果仍为 `63/68 F2P`，但 P2P 从 `241/242` 恢复到 `242/242`。
- 根据 resolved、P2P regression、F2P 的排序规则，Round 10 成为最终 best candidate。后续 candidate
  不覆盖该冻结 revision。

## Round 11 至 12：确定 plateau

- Round 11 调整 `dvc.repo.diff.diff` 等 dispatch seam，移除诊断 warning。Worker-visible compatible
  unit 454 passed，official 5 条失败不变。
- Round 12 再次实质修改 table header、`--show-hash`、full-hash transition、positional facade call、
  empty human/JSON output。Worker-visible unit 和 focused diff suites 通过，official 5 条失败仍不变。
- Round 10、11、12 的 F2P/P2P 和失败 test ID 完全一致，构成有 evaluator evidence 的 plateau，
  而不是 harness 固定轮数终止。

最终 5 条失败：

```text
tests/unit/command/test_diff.py::test_default
tests/unit/command/test_diff.py::test_no_changes
tests/unit/command/test_diff.py::test_show_hash
tests/unit/command/test_diff.py::test_show_json
tests/unit/command/test_diff.py::test_show_json_and_hash
```

## Round 13 与停止

- Runner 在 Round 12 evaluator 后启动同一 parent thread 的 Round 13 resume；provider connection
  refused，未产生模型 response、workspace candidate 或 evaluator invocation。
- Operator 在确认三轮 official plateau 后请求停止。Finalizer 将 Round 13 标记为
  `discarded_without_candidate_or_evaluator`，保留 live JSONL/stderr，但不把它伪装成第 13 个失败候选。
- 正式结果为 12 candidates、12 evaluators、best Round 10、`task-failed`，termination reason 为
  `operator_requested_stop_after_evaluated_plateau`。

## 最终编排观测

- parent thread 始终为 `019f6f58-25b4-76d3-a491-ffb9b5d3e69c`；
- native implementation/audit subagents：54；
- guardian sidecars：7，不计入 implementation workers；
- implementation turns：67；context compactions：7；
- `spawn_agent=57`、`send_message=250`、`followup_task=13`、`wait_agent=127`；
- peak implementation concurrency：4；time-weighted average：`2.139541`；
- input/cached input：`245410202 / 236906496`，cache ratio `0.965349`。

Native orchestra 的主要正向证据是 Round 3 的 `14 -> 54` 跳升和后续持续收敛；主要负向证据是
shared workspace 冲突、每轮 ephemeral child 重建、compaction 约束漂移，以及最终隐藏契约盲区。
