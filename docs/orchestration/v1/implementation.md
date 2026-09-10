# orchestra 首版实施与验收记录

> 本文前半部分保留首版实现选择和行为边界，末尾记录 2026-09-01 实际落地的代码、模型接入、测试与真实三轮验收。股票模拟系统仍是首个真实长期目标，但尚未初始化。

## 0. 当前实现状态

**Orchestra v1 已实现并通过真实一对一闭环验收。**

实际代码：

```text
hermes_cli/orchestra_v1.py
hermes_cli/orchestra_v1_control.py
hermes_cli/orchestra_v1_decision.py
hermes_cli/orchestra_v1_worker.py
hermes_cli/orchestra_v1_codex.py
scripts/orchestra_v1.py
tests/test_orchestra_v1.py
tests/test_orchestra_v1_control.py
tests/test_orchestra_v1_decision.py
tests/test_orchestra_v1_worker.py
tests/test_orchestra_v1_codex.py
tests/test_orchestra_v1_codex_integration.py
```

当前已经可以：

```text
初始化一个目标项目并独立保存 intent.md
→ 每次 decide 创建全新的 Hermes AIAgent
→ 从人类意图、当前判断、任务、最近结果与 Git 事实重建上下文
→ 只允许 Orchestra 读取和搜索目标仓库
→ 保存完整的新 state.md、task.md 和 decision.txt
→ 根据决定新建或恢复真实 Codex thread
→ 将 worker 最终结果写入 result.md
→ 在下一次 fresh decide 中自动使用该结果
```

首版命令已经可运行：

```text
python scripts/orchestra_v1.py init --project <path> --goal-file <path>
python scripts/orchestra_v1.py decide --project <path>
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path>
python scripts/orchestra_v1.py status --project <path>
```

首版仍由人类显式触发，不运行后台循环。实际实现与验收细节见本文第 10 节。

## 1. 首版固定选择

### 1.1 代码组织

首轮实现实际落在以下文件：

```text
hermes_cli/orchestra_v1.py
hermes_cli/codex_worker.py
scripts/orchestra_v1.py
tests/test_orchestra_v1.py
tests/test_codex_worker.py
```

这是首轮实施记录，不是继续向少数大文件堆叠功能的结构要求。后续修改必须遵守 [`project-rules.md`](project-rules.md)：按控制材料、决策、Agent 单轮执行、仓库读取、worker 协调和 Codex transport 等真实职责逐步拆分；CLI 保持薄层，测试按职责镜像组织。

不为了形式一次性建立空目录、插件系统或通用运行时，但下一项实质功能触及现有混合职责时，必须在同一改动中拆出所触及的职责，而不是继续扩大 `orchestra_v1.py` 或 `codex_worker.py`。

### 1.2 orchestra 使用 Hermes 现有代理循环

orchestra 直接使用 `run_agent.AIAgent`，不再实现第二套模型调用和工具循环。

每次 `decide` 创建一个新的 `AIAgent`：

```python
AIAgent(
    session_id=<每轮新标识>,
    ephemeral_system_prompt=<orchestra 固定职责>,
    skip_context_files=True,
    skip_memory=True,
    load_soul_identity=False,
    max_iterations=<单轮检查上限>,
)
```

必须满足：

- 不传入上一轮 orchestra 对话；
- 不设置父会话；
- 不加载普通记忆、人格和项目上下文文件；
- 单轮内保留完整工具循环；
- 本轮结束后不再恢复该会话。

首版给 orchestra 的工具只包含代码读取与搜索。先不提供写文件、补丁、子代理和旧 Kanban 工具；Git 分支、版本、工作区状态和差异摘要由外层程序机械收集后直接放入上下文。需要运行测试或产品时，先由 orchestra 把它作为 worker 任务或人类检查要求，不为首版继续建设一套任意命令权限系统。

### 1.3 worker 首版只接 Codex

首版不设计通用 worker 接口，也不同时支持 Claude Code。

第一个 worker 后端固定使用 Codex app-server。它已经提供结构化的：

- 新建会话；
- 恢复已有会话；
- 开始一次执行；
- 接收流式事件；
- 中断执行；
- 获得最终状态。

首版启动器通过本地标准输入输出运行 `codex app-server`，使用 JSONL 消息完成：

```text
initialize
→ initialized
→ thread/start 或 thread/resume
→ turn/start
→ 等待 turn/completed
```

新任务使用 `thread/start`，同一任务继续使用 `thread/resume`。`Ctrl+C` 时发送 `turn/interrupt`，随后清理子进程。

如果旧运行时内核中的真实 Codex worker 代码可以直接抽取，应优先复用；若其依赖旧任务图、数据库或节点协议，则只保留 app-server 通信部分，不连带迁移旧运行时结构。

Codex 接入在首版中可以写死。只有一对一闭环稳定后，才讨论第二种 worker 后端。

### 1.4 人类显式触发

首版不运行守护进程，不轮询仓库，不自动判断什么时候进入下一轮。

每次由人类执行命令：

```text
init
决定
运行 worker
查看状态
```

后续可以提供一次性的 `step` 组合命令，但它仍然在终端前台运行，并在真正启动 worker 前显示 orchestra 生成的任务。

## 2. 本地控制材料

每个目标项目使用一个独立控制目录：

```text
$HERMES_HOME/orchestra/<project-key>/
├── intent.md
├── state.md
├── task.md
├── result.md
├── decision.txt
├── worker-thread.txt
└── last-orchestra-output.md
```

`project-key` 由仓库绝对路径的稳定摘要生成。控制目录解析符号链接后的真实位置必须位于目标项目之外，避免 worker 的项目写权限覆盖控制材料。首版不建设项目注册中心。

### 2.1 `intent.md`

人类意图唯一来源。`init` 把 `--goal-file` 内容写入该文件，之后只有人类直接编辑它。每轮 `decide` 都重新读取；Orchestra 和 worker 无权覆盖、追加或生成替代版本。

首版不增加意图更新命令、固定格式、schema、迁移或旧状态自动提取。旧控制目录缺少 `intent.md` 时，`decide` 明确失败，由人类根据真实意图手工创建。

### 2.2 `state.md`

只保存 Orchestra 当前有效的项目判断，例如：

- 已确认项目事实；
- 承重假设及失效信号；
- 当前收敛缺口；
- 重要停止路线；
- 当前决定残留；
- 待人类决定事项。

初始化时该文件为空，之后由每轮 Orchestra 整体重写；它不保存或复制人类意图。

实现禁止：

- 固定章节校验；
- 版本号；
- 迁移；
- 新旧格式兼容；
- 追加式运行日志；
- 自动修复历史状态；
- 要求 worker 维护该文件。

### 2.3 `task.md`

当前 worker 的推进说明。新任务或任务边界修正时整体覆盖。

它不包含完整项目状态和 orchestra 推理，只包含：

- 当前期望的项目变化；
- 为什么现在做；
- 相关事实；
- 工作假设；
- 自主范围；
- 必须保留的边界；
- 需要返回的证据；
- 何时提前上报。

### 2.4 `result.md`

最近一次 worker 最终回答，整体覆盖。

worker 应尽量说明：

- 实际改变了什么；
- 证据在哪里；
- 哪些内容没有完成；
- 是否发现改变项目方向的新事实；
- 是否有承重假设失效。

该文件只是下一轮线索，不自动成为项目事实。

### 2.5 `decision.txt`

只保存当前机械动作之一：

```text
继续当前任务
开始新任务
等待
询问人类
停止
```

它不是项目状态机，只用于决定是否启动、恢复或跳过 worker。“开始新任务”在新 thread ID 就绪后由 `run-worker` 更新为“继续当前任务”，使中断后的再次运行恢复该 thread，而不是重复新建。

### 2.6 `worker-thread.txt`

保存当前任务对应的 Codex `thread.id`。

- “开始新任务”的决定产生时不修改旧 ID；
- `run-worker` 请求新任务且 `thread/start` 返回新 ID 后原子覆盖；
- 人类取消启动或新 thread 建立前失败时保留旧 ID；
- 新 thread 已建立后，即使当前 turn 中断或失败，也保留新 ID 供同一任务恢复；
- 继续当前任务时恢复当前 ID；
- 文件缺失或会话无法恢复时明确失败，不自动创建替代路线；
- 旧任务 thread 不进入下一轮 Orchestra 默认上下文。

### 2.7 `last-orchestra-output.md`

保存本轮 orchestra 原始最终回答，便于调试。

它不自动进入下一轮上下文。只有人类排查输出解析问题时读取。

## 3. 首版命令

首版脚本提供五个入口：

```text
python scripts/orchestra_v1.py init --project <path> --goal-file <path>
python scripts/orchestra_v1.py decide --project <path>
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path>
python scripts/orchestra_v1.py status --project <path>
```

### 3.1 `init`

只做机械初始化：

1. 确认项目目录存在；
2. 计算 `project-key`；
3. 创建控制目录；
4. 把人类目标写入 `intent.md`；
5. 创建空的 `state.md`、`task.md`、`result.md`、`decision.txt`、`worker-thread.txt` 和 `last-orchestra-output.md`；
6. 不调用模型，不分析项目，不生成路线图。

重复初始化已有项目时必须拒绝，除非人类显式要求覆盖。首版不设计状态合并。

### 3.2 `decide`

完成一次真正的全新 orchestra 决策轮：

1. 重新读取作为唯一人类意图来源的 `intent.md`；
2. 分别读取当前判断 `state.md`、当前任务 `task.md`、最近结果 `result.md` 和当前机械动作 `decision.txt`；
3. 机械收集以下 Git 事实：
   - 仓库根目录；
   - 当前分支；
   - 当前提交；
   - `git status --short`；
   - `git diff --stat`；
   - 最近若干提交标题；
4. 创建新的 `AIAgent`；
5. 注入固定职责、只读人类意图、当前判断、任务、结果和机械事实；
6. 允许 Orchestra 按需读取和搜索仓库；
7. 保存原始输出到 `last-orchestra-output.md`；
8. 解析当前决定、完整新项目判断和 worker 推进说明；
9. 解析成功后原子覆盖 `state.md`、`task.md` 和 `decision.txt`，不写 `intent.md` 或 `worker-thread.txt`；
10. 结束本轮 Orchestra 会话。

orchestra 输出首版只约定以下形状：

```markdown
决定：开始新任务

# 项目状态
完整的新项目判断正文

# worker 推进说明
完整的当前任务正文
```

决定值只能是五种机械动作。标题缺失、决定未知或正文为空时：

- 保留原始输出；
- 不覆盖旧 `state.md` 和 `task.md`；
- 返回非零状态；
- 由人类查看后重新运行。

首版不增加自动修复、模型重试、旧格式兼容和第二个解析代理。

### 3.3 `run-worker`

根据 `decision.txt` 运行一个真实 worker：

```text
开始新任务
→ 保留旧 worker-thread.txt
→ thread/start
→ 新 ID 就绪后原子替换 worker-thread.txt
→ decision.txt 更新为“继续当前任务”
→ turn/start(固定 worker 约束 + task.md)

继续当前任务
→ thread/resume(worker-thread.txt)
→ thread/compact/start
→ 等待 contextCompaction item 与压缩 turn 完整成功
→ turn/start(固定 worker 约束 + task.md)

等待 / 询问人类 / 停止
→ 不启动 worker
```

worker 运行在目标仓库目录，使用 Codex 的工作区写入隔离。首版默认不允许无限制宿主机访问；需要网络时由人类通过已有 Codex 配置决定，不在 Orchestra 中新增权限系统。

Orchestra 启动的 app-server 会只在该子进程中启用 Codex `remote_compaction_v2`，使当前 OpenAI-compatible Responses 服务通过已验证可用的 v2 压缩协议工作，而不是调用缺失的旧 `/v1/responses/compact` 端点。该参数不修改 `~/.codex/config.toml`、不复制凭据，也不改变 provider 身份和原生工具能力。新 thread 没有历史，因此不主动压缩；恢复 thread 时必须先等待显式压缩完成。压缩失败时不启动业务 turn、不自动重试，并保留原 thread 供人类再次显式恢复。

外层程序固定要求 worker：只完成当前任务，不扩展项目目标，不为未来建设通用机制；承重假设错误时报告而不是扩大范围；最终回答说明实际变化、证据、未完成问题和影响方向的新事实。该回答不使用 schema，也不由程序校验。

执行期间：

- 终端前台显示主要事件；
- 收集最终代理消息；
- Codex turn 返回结果后整体覆盖 `result.md`；
- 不自动启动下一轮 Orchestra；
- `completed`、`failed`、`timed_out` 和 `interrupted` 返回状态都写入结果摘要；启动器在返回结果前直接抛出的异常仍向上抛出。

### 3.4 `step`

`step` 只是交互式组合：

```text
decide
→ 显示决定和 task.md
→ 人类确认
→ run-worker
```

默认必须确认后才启动 worker。首版不提供无人值守连续循环。

### 3.5 `status`

只显示机械事实：

- 项目路径；
- 当前决定；
- 当前 worker thread；
- 各控制文件更新时间；
- Git 分支与当前提交；
- 最近一次 worker 是否成功结束。

`status` 不计算项目进度，不解释收敛缺口，不调用模型。

## 4. orchestra 请求体

每轮请求由五部分组成：

```text
稳定职责
  Orchestra 的角色、禁止项和输出要求

人类意图
  每轮重新读取的 intent.md 原文，唯一来源且只读

当前项目判断
  state.md 原文，空时明确标为尚无判断

本轮变化
  当前 decision.txt、task.md、最近 result.md、Git 机械事实

仓库检查能力
  只读文件与搜索工具
```

固定职责必须强调：

- 只做项目级判断，不接管实现；
- worker 自述不是事实；
- 只核实会改变路线的承重信息；
- 不完整规划整个项目；
- 每轮最多选择一个当前任务；
- 不因既有代码、测试、待办或投入成本继续一条路线；
- 不把 orchestra 自身状态和运行机制扩张成业务目标；
- 方向改变必须指出新增事实、失效假设或此前遗漏的矛盾；
- 输出完整替换后的项目判断，而不是状态补丁；
- 不复制、改写或重新解释 `intent.md`。

首版不启用两段式输入。先用一次性上下文跑通真实闭环；若稳定出现旧策略锚定，再单独比较两段式做法。

## 5. worker 任务边界

是否恢复旧 worker thread 只由当前决定确定：

```text
同一可观察结果仍未完成
且主要承重假设与自主范围没有变化
→ 继续当前任务

期望项目变化、承重假设或自主范围实质变化
→ 开始新任务
```

orchestra 必须在输出中明确选择，不由外层代码根据文本相似度猜测。

Orchestra 只写决定，不提前改变 thread。人类未确认运行或 `thread/start` 前失败时，`worker-thread.txt` 仍指向旧任务；新 thread ID 真正就绪后才原子替换。旧 thread 仍保存在 Codex 自己的会话存储中，但不再属于当前运行状态，也不自动提供给新的 Orchestra 或 worker。

## 6. 实现批次

### 批次零：验证 Codex worker 接入

先写最小 `codex_worker.py`，只验证：

- 能启动 `codex app-server`；
- 能完成初始化握手；
- 能 `thread/start`；
- 能 `turn/start` 并等待结束；
- 能保存 `thread.id`；
- 新进程中能 `thread/resume`；
- 能在 `Ctrl+C` 时中断；
- 能取得最终代理消息。

这一批只在临时仓库中运行，不实现 orchestra，也不接股票项目。

如果现有旧 worker lane 可以满足这些能力，直接抽取其通信代码。不能为了复用它把旧数据库、节点、receipt 或恢复协议一起带回来。

### 批次一：控制目录与机械命令

实现：

- `init`；
- `status`；
- 路径与 `project-key`；
- 原子文件写入；
- Git 机械事实收集；
- orchestra 输出解析。

本批次使用假 orchestra 输出，不调用模型。

### 批次二：接入全新 orchestra

实现：

- 固定职责提示词；
- 每轮新 `AIAgent`；
- 关闭普通记忆和上下文文件；
- 只读代码工具；
- `decide`；
- 原始输出保存；
- 解析失败保护。

本批次只生成 `state.md`、`task.md` 和 `decision.txt`，暂不自动运行 worker。

### 批次三：连接一对一闭环

实现：

- `run-worker`；
- 新任务与继续任务；
- `result.md`；
- 前台事件显示；
- 取消和失败处理；
- `step`。

### 批次四：机械闭环验收

使用一个极小临时仓库连续运行至少三个决策轮，检查：

- 每次 orchestra 都是全新会话；
- 新状态确实覆盖旧状态；
- 新任务启动新 worker thread；
- 同一任务继续恢复原 thread；
- worker 结果能够进入下一轮；
- `等待`、`询问人类` 和 `停止` 不会启动 worker；
- 全程无需人工复制文件内容。

这个临时仓库只验证运行机械性，不评价 orchestra 战略能力。

### 批次五：进入股票模拟系统

只有批次四通过后，才按 [`targets.md`](targets.md) 初始化股票模拟系统，开始真实的一对一调试。

股票项目不是实现 orchestra 的前置步骤，也不是用来掩盖运行闭环尚不存在的演示。

## 7. 测试要求

### 7.1 单元测试

至少覆盖：

- `project-key` 对同一路径稳定；
- 初始化把人类目标只写入 `intent.md`，并拒绝静默覆盖；
- `state.md` 初始化为空；
- 文件更新使用临时文件后原子替换；
- 每次 `decide` 使用不同 Orchestra `session_id` 并重新读取 `intent.md`；
- 请求体不包含上一轮原始 Orchestra 输出和完整 worker 对话；
- 成功与解析失败路径都不覆盖 `intent.md`；
- 五种决定都能正确识别；
- worker prompt 固定包含任务边界和最终回答要求；
- `开始新任务` 在新 thread 就绪前保留旧 ID，就绪后原子替换；
- 中断不会丢失当前可恢复 thread；
- `继续当前任务` 必须存在可恢复 thread；
- `等待`、`询问人类`、`停止` 不启动 worker。

### 7.2 Codex 接入测试

使用假 app-server 进程验证 JSONL 通信：

- 初始化顺序；
- 请求与响应编号匹配；
- 事件流读取；
- 最终消息收集；
- `turn/completed`；
- 服务异常退出；
- 中断；
- 恢复 thread。

### 7.3 真实冒烟测试

在明确启用的情况下运行：

1. 真实 Codex 新建 thread；
2. 修改临时仓库中的一个简单文件；
3. 新进程恢复同一 thread；
4. 要求继续同一任务并完成；
5. 检查最终文件和结果消息。

真实模型测试不进入普通快速测试套件，也不因网络或额度失败阻塞全部单元测试。

## 8. 首版失败处理

首版保持直接：

```text
orchestra 调用失败
→ 不更新 state.md 和 task.md
→ 保存错误并退出

orchestra 输出无法解析
→ 保存原始输出
→ 不更新当前状态
→ 人类处理

Codex 无法启动或认证失败
→ 不创建替代 worker
→ 明确报错

worker thread 无法恢复
→ 当前任务停止
→ 由下一轮 orchestra 或人类决定是否开始新任务

worker 执行失败
→ 把失败事实写入 result.md
→ 不自动重试

程序崩溃
→ 依赖原子文件避免半写状态
→ 不建设恢复状态机
```

首版不允许出现“失败后自动创建修复任务、审查任务或恢复节点”的机制。

## 9. 首版验收与停止线

首版可进入股票项目的最低条件：

- 实际代码已经存在，不再依赖人工复制粘贴；
- orchestra 每轮确实是全新会话；
- 当前项目状态可以整体更新；
- 一个真实 Codex worker 可以新建和恢复；
- 新任务与继续任务边界能实际工作；
- worker 结果可以进入下一轮；
- 三轮机械闭环稳定完成；
- 人类可以随时查看并直接编辑所有控制材料。

以下内容不属于首版验收：

- 守护进程；
- 自动唤醒；
- 多 worker；
- Claude Code 后端；
- 通用 worker 接口；
- 数据库；
- 执行图；
- 状态版本；
- 自动输出修复；
- 两段式上下文；
- 独立评审流水线；
- 完整可视化界面。

如果实施过程中开始围绕上述非目标增加基础设施，应停止并回到“一个全新 orchestra、一个当前任务、一个真实 worker、一次显式运行”这个最小闭环。

## 10. 2026-09-01 实际实施记录

### 10.1 代码落点

Orchestra v1 当前按真实职责拆分：

- `hermes_cli/orchestra_v1_control.py` 管理七个控制文件、稳定 `project-key`、原子写入、Git 机械事实和 `status`；
- `hermes_cli/orchestra_v1_decision.py` 定义五种决定、输出解析和包含独立 `intent.md` 的请求组装；
- `hermes_cli/orchestra_v1_worker.py` 构造固定 worker 行为约束，并负责新建或恢复 thread、结果落盘；
- `hermes_cli/orchestra_v1_codex.py` 负责 Orchestra 专属的 app-server 启动参数、预压缩和业务 turn；
- `hermes_cli/orchestra_v1.py` 保留 fresh `AIAgent`、repository-scoped 只读工具和单轮应用协调，同时重新导出现有公共入口；
- `scripts/orchestra_v1.py` 只负责参数、前台确认、输出和退出码。

每次 `decide` 创建新的 `AIAgent`，不传旧对话和父会话；普通上下文文件、记忆、人格和旧 Kanban 工具保持关闭。本轮只临时注册读取与搜索工具，并把路径限制在目标仓库内。

`hermes_cli/orchestra_v1_codex.py` 提供 Orchestra 专属的最小 `run_codex_turn`，共享 `hermes_cli/codex_worker.py` 不再承载 Orchestra 行为：

- 初始化 `codex app-server`；
- `thread/start` / `thread/resume`；
- 在 `turn/start` 前回调保存 thread ID；
- 读取流式通知并收集最后一条 `agentMessage`；
- 处理 `turn/completed`、服务异常退出、超时、`KeyboardInterrupt` 和 `<turn_aborted>`；
- 中断活动 turn 后清理 app-server 子进程。

`scripts/orchestra_v1.py` 提供 `init`、`decide`、`run-worker`、`step` 和 `status` 五个前台命令。`step` 在启动 worker 前显示决定和任务并要求人类确认，不构成后台循环。

### 10.2 模型与认证接入

Orchestra 仍使用 Hermes 的 `run_agent.AIAgent`，但首版直接复用同一台机器上的 Codex 模型源：

- 模型、provider 和 `base_url` 来自 `~/.codex/config.toml`；
- API key 来自 `~/.codex/auth.json` 的现有 `OPENAI_API_KEY`；
- AIAgent 使用 OpenAI-compatible Responses 路径；
- key 不写入项目控制材料、不进入 Orchestra 请求体，也不复制到新的凭据文件。

当前 Codex 自定义 provider 需要声明：

```toml
wire_api = "responses"
requires_openai_auth = true
```

否则 `auth.json` 中已有 key 不会被该 provider 用于请求，app-server 会得到 `401 Missing API key`。

### 10.3 自动测试与静态检查

使用隔离 Python 环境 `/tmp/hermes-orchestra-venv.2Kovrn` 执行：

```text
Orchestra/Codex 新增单元测试：27 passed
真实 Codex integration：1 passed
既有 Codex worker 回归测试：81 passed
合计：109 passed
ruff：passed
ty：passed
```

新增测试覆盖：

- 稳定 `project-key`；
- 初始化拒绝静默覆盖；
- 原子写入和 symlink 保留；
- 五种决定解析与错误输出保护；
- fresh session ID；
- 上一轮原始 Orchestra 输出不进入下一轮；
- 仓库绝对路径和 symlink 逃逸被拒绝；
- 新任务在 `thread/start` 返回后、`turn/start` 前保存新 thread；
- 继续任务必须恢复已有 thread；
- 三种非运行决定不启动 worker；
- app-server 新建、恢复、通知、最终消息、异常退出、中断和 `<turn_aborted>`；
- 真实 Codex 新进程恢复同一个 thread 并继续修改仓库。

### 10.4 真实三轮机械闭环

在临时 Git 仓库中使用真实 AIAgent 和真实 Codex app-server 连续完成三轮：

```text
第 1 轮：开始新任务
→ 创建 progress.txt，内容为 one
→ worker completed

第 2 轮：继续当前任务
→ 新 app-server 进程恢复第 1 轮的同一 Codex thread
→ 在 progress.txt 追加 two
→ worker completed

第 3 轮：开始新任务
→ 创建不同的新 Codex thread
→ 创建 done.txt，内容为 done
→ worker completed
```

验收观察：

- 三轮使用三个不同的 Orchestra session ID；
- 前两轮 Codex thread ID 相同；
- 第三轮 Codex thread ID 与前两轮不同；
- `progress.txt` 最终严格为 `one\ntwo\n`；
- `done.txt` 最终严格为 `done\n`；
- worker 结果由程序写入并自动进入下一轮；
- 全程不需要人工复制粘贴状态、任务或结果材料。

### 10.5 首版结论与下一步

第 9 节列出的首版最低条件已经全部满足。当前可以声称仓库中已有可运行的 Orchestra v1。

尚未开始的下一步是按 [`rollout.md`](rollout.md) 和 [`targets.md`](targets.md) 初始化股票模拟系统，把它作为首个真实长期目标。该步骤不是首版实现的一部分，也没有在机械验收过程中提前启动。

### 10.6 2026-09-02 长期项目前边界修正

进入第一个真实长期项目前完成以下修正：

- 新增 `intent.md`，人类目标不再存入可被 Orchestra 重写的 `state.md`；
- 删除 `decide` 和 `step` 的 `--human`，避免第二条人类意图输入路径；
- `state.md` 初始化为空，之后只保存 Orchestra 当前项目判断；
- worker 每次都收到固定行为边界以及当前 `task.md`；
- “开始新任务”的决定不再清空旧 thread；新 ID 只在 `thread/start` 成功后原子替换；
- 控制目录按符号链接解析后的真实路径必须位于 worker 项目之外；
- Orchestra 代码和测试按 control、decision、worker 与应用协调职责拆分；
- 未增加 schema、validator、数据库、迁移、调度系统、多 worker 或自动循环；
- 未初始化或运行股票模拟系统。

本次相关验证实际结果：

```text
Orchestra 与 Codex 接口相关测试：36 passed
ruff（本次 Python 文件）：passed
ty（Orchestra v1 模块）：passed
```

测试覆盖 intent 所有权、fresh 决策轮重新读取、解析失败保护、控制目录符号链接边界、固定 worker prompt，以及新 thread 建立前后和取消路径中的恢复能力。验证范围只覆盖本次 Orchestra 文件及其直接 Codex 接口，没有运行整个 Hermes 源项目测试。

### 10.7 2026-09-03 worker 预压缩修正

stock-sim 首个全量数据任务在上下文接近自动压缩阈值时失败。Codex 0.152.0 在 `remote_compaction_v2 = false` 时选择兼容服务不支持的旧 `/v1/responses/compact` 端点，因此返回 404；该配置并不表示关闭远端压缩或自动改用其他路径。

当前 Responses 服务已用临时 thread 验证支持 Codex v2 压缩协议，因此本次只修正 Orchestra worker：

- Orchestra app-server 子进程固定启用 `remote_compaction_v2`，不再调用缺失的旧 compact 端点；
- 不改 provider 名称，继续保留原生 web search 等 OpenAI provider 能力；
- 新 thread 仍直接进入业务 turn；恢复 thread 时先调用 `thread/compact/start`，等待 `contextCompaction` item 和压缩 turn 完整成功后再启动业务 turn；
- 压缩失败、生命周期不完整、超时、中断或 app-server 退出时不发送业务 turn、不自动重试，并保留原 thread；压缩请求和业务 turn 启动请求都受同一个剩余时限约束；
- 全局 `~/.codex/config.toml`、认证材料、其他 Codex 进程和旧 Runtime 行为均不修改；共享 `codex_worker.py` 只保留原有导入路径的兼容导出。

聚焦验证结果：

```text
Orchestra/Codex 相关测试：30 passed
tests/test_orchestra_v1_codex_integration.py：与纯单元测试分文件保存，默认不运行
ruff（本次 Python 文件）：passed
ty（Orchestra worker 与 Codex 模块）：passed
临时真实 thread：remote_compaction_v2 预压缩后继续 turn completed
stock-sim 真实恢复：contextCompaction started/completed 后业务 turn completed
全局 Codex config：运行前后未改变
```

真实恢复继续使用 stock-sim thread `01a06271-aaf8-7a02-89a7-1e5db772169d`，没有新建替代 thread。预压缩成功后当前数据调查 turn 完成，证明兼容服务不再需要 `/v1/responses/compact` 才能恢复长任务。

### 10.8 2026-09-07 设计与运行提示对齐

现有设计、Orchestra 系统提示、决策请求和 worker 固定提示共同区分项目决策与人类批准：授权范围内的新任务不再自动要求人类批准；人类本轮目标、预算、权限和停止条件继续有效。`state.md` 可保留简短可修订方向和任务理由，不增加字段。人类约束不能被模型任务覆盖，当前可核实事实可推翻任务假设。

新任务和恢复任务继续共用 `build_worker_prompt()`；恢复仍先完成预压缩，再发送当前完整任务。Hermes 不拼接设计全文或仓库规则，提示 worker 按需确认当前 `AGENTS.md`、相关规则和工作区变化。请求中明确 Codex 的 `workspace-write`、`approval=never` 和 `.git` 只读能力，提交推送由授权宿主执行，避免再向 worker 下发不可执行的 Git 要求。

内部子代理精简返回，验证保持最小充分，关键测试和运行结果在已有仓库材料中保留可定位引用。`result.md` 沿用现有外层包装和原始最终文本，内容为完成、关键验证、未闭合、方向影响、产物位置；不解析它们，也不新增 schema、事件日志、reviewer 或运行时。

聚焦验证：`scripts/run_tests.sh tests/test_orchestra_v1.py tests/test_orchestra_v1_decision.py tests/test_orchestra_v1_worker.py tests/test_orchestra_v1_codex.py`，38 项通过；本次修改的 Python 文件 Ruff 和 `git diff --check` 通过，未运行全仓测试。

### 10.9 2026-09-09 决策自主性与现场保留

本次只抽取最近三个有记录阶段的授权、下发任务和吸收，不审查股票业务代码，也不用股票测试数量评价方向质量：

| 阶段 | 上层已指定 | 可观察到的 Orchestra 选择 | 吸收与局限 |
| --- | --- | --- | --- |
| 可配置连续账户 | stock-sim `f34e338:AGENTS.md` 已要求可配置、自动承接账户及完成后停止 | 下发任务选择现有 `000002` 两日、每日买/卖/不交易，而非扩大数据 | 状态提交 `7af9c52` 以这项用户过程成立为停止理由；不能说产品方向由 Orchestra 自主产生 |
| 解除主要使用限制 | `ef32fe1:AGENTS.md` 给出限制判断空间，但仍以一个中程结果完成为本轮停止线 | 任务选择 JSON 动作列表及第三日期；原日期 404 后转向可用 2 月 5 日，后来限制未知间隔非零持仓 | `8d1f417` 吸收；可确认方向和替代选择存在，不能由功能形成推断它是所有候选中的最优选择 |
| 非零持仓跨期 | `d718f35:AGENTS.md` 已指定持有跨间隔、原 JSON 入口、有限公司行动确认及局部职责拆分 | 任务进一步限定查询区间、T+1 和初始不可卖持仓边界 | `7f805e7` 按上层停止线结束；这是已指定方向内的任务细化，不是独立发现下一个产品目标 |

任务来源是宿主实际 `decide` 输出（`stock-sim-continuous-decide-20260908.log`、`stock-sim-bottleneck-decide-20260909.log`、`stock-sim-holding-decide-20260909.log`），本次在独立状态仓库的普通观察材料中保留文本。原阶段只有最终吸收后的控制材料提交，启动 worker 前的完整项目状态未被 Git 保留。

历史输入缺口：这三个阶段使用的 Hermes 提交为 `4bf800d`，代码可证明 `_new_orchestra_agent()` 创建 fresh `AIAgent`、请求包含 intent/state/task/result/decision/Git，并要求按需读取 `AGENTS.md`。但它未传入会话数据库，原生 JSON 会话保存默认关闭，本机现有记录没有对应的完整原请求。实际系统提示是 Hermes 基础提示与 `ephemeral_system_prompt` 拼接，并非仅 Orchestra 常量；附加提示也不会自动进入普通持久系统提示。因此不能从当前模板事后声称恢复了当时所有实际输入或工具读取，相关因果强度保持未确认。

另一个已记录故障是 `stock-sim-bottleneck-alternative-20260909.log`：决定曾要求人类安排本来已有授权的宿主下载。系统知道 worker 沙箱受限，却没有清楚区分宿主机械职责与新授权。该问题与产品路线自主性分开处理。

本次替换系统提示和请求中偏向单轮局部目标的措辞，明确整体缺口、中程方向、任务优先理由及根据事实延续/调整；去掉设计中每轮列方案和强制反证流程。stock-sim 的执行者规则不再保存已完成子目标，保留长期意图、真实约束和宿主/worker 分工。worker 仍只完成当前任务。

历史阶段服从当时明确的用户子目标和停止条件，不应被倒推成违反当时授权；问题是不能把这些阶段的成功等同于自主发现中程方向。

本次在普通本地文本中保存实际模型消息的系统提示（包括附加部分）、请求和按需读取内容；启动 worker 前保存当前决定和任务，之后分别保留结果与吸收。权限分类器拒绝发布原始模型输入和工具上下文，因此这些新原始现场仅本地保存，未绕过拒绝；Git 已保留设计改动及允许发布的历史材料。详细观察不默认进入下一轮。没有新增运行时、日志平台、状态字段或结果评审系统；本次最多三个 worker 任务仅由研发调用预算限制。

本次提示改动的聚焦验证仍为 38 项通过，Ruff 与 `git diff --check` 通过。这只验证实现和接口行为，不构成决策质量结论。

本次连续试运行完成三个 worker 任务与四次 fresh 决策。宿主仅在实际请求末尾说明剩余研发预算和已有机械权限，没有给出股票候选功能：

- `round-01` 从整个项目输入缺口出发，自主选择把现有本地逐笔数据接入单次账户入口，理由是历史数据仍需改 Python 登记表；没有沿刚完成的跨期持仓继续加功能。
- `round-02` 吸收清单输入能力后，没有按单功能完成停止，也没有继续清单相邻细节；它降低继续逐笔入口的优先级，转向长期目标缺失的证券身份、历史上市状态、ST 和日线字段，选择 BaoStock 的有界资格判别。
- 第二 worker 因沙箱 DNS 失败报告零响应。宿主依现有权限在允许的临时隔离环境运行同一探针，成功取得小型主表和四个短窗口；原 worker 结果另存，报告明确区分后续宿主事实。`round-03` 实际读取本地响应，明确推翻零响应前提，但不把查询成功等同于主来源合格，选择依据已有样本完成资格裁定。
- `round-04` 吸收“只能作为补充来源”的结论，将中程方向转向下一日线来源或最小来源组合；决定为“等待”，明确仅因本次三个任务预算用尽，长期目标未满足，不存在新的人类产品选择或资源阻塞。没有第四个 worker。

仍有可观察的反例：最终 `state.md` 的集合竞价概述退回“四个固定样本”，未保留首个任务已形成的本地清单输入能力；第三次 fresh 状态原本有这一内容。中程方向虽保留，全项目能力摘要仍可能遗漏重要边界，本次不手工替 Orchestra 修写状态。也不能由三个任务证明选中路线优于其他候选、长期收敛已经改善，或把效果单独归因于提示改动。

本地原始现场：`/root/.hermes/orchestra/ff3397784d64103c/observations/20260909/round-01` 至 `round-04` 的 `input.md`、`context.md`、`decision.md`，及前三轮 `worker-input.md`、`result.md`；它们没有推送。状态仓库 `6173ee9` 仅发布原有控制文件的最终吸收与等待，未包含被拒绝发布的原始请求。试运行项目提交是 `65f499d`、`c100712`、`c768f99`，只作为实际运行所产生的产物索引，不作为决策质量评分。

### 10.10 能力概况与用途条件的定向定位

本次使用上述本地真实现场，不审查股票业务代码，不将测试数量用于评价决策质量。

能力遗漏不是组装时丢失：`round-04-input.md:153` 和最后一次调用的 `round-04-context.md:153` 都完整包含清单输入能力；`round-03-decision.md:8` 将四个直接可运行样本与未登记清单入口分别表达。第四轮实际只读了项目规则、来源资格报告和相关文档搜索，没有查清单说明；其 `decision.md:8` 却把已验证四个样本缩写为整个能力的固定上限。可确认的是输入仍在而输出发生语义缩窄，不是上下文截断，也没有发现能力被删除或反证。现有状态提示把概况与方向混在一个保留项中，当前来源报告成为最新任务的主要展开材料；这与遗漏相容，但不能据此知道模型内部为什么选择省略。

来源条件的传递链同样可定位：第二轮中程方向原为补充证券主数据与日线字段，任务却同时要求 A 股全集定义、上游不可变标识、新克隆严格重取；第三轮任务第 9 项进一步把全集分类、固定上游版本和许可设为主来源三选一的硬条件。第三 worker 报告明确依照任务三选一标准裁定；第四轮将该裁定吸收为排除主管道并换源。这是模型条件被后续报告再次确认，不是每项条件都新增了独立外部依据。

| 条件 | 依据与适用范围 |
| --- | --- |
| 历史真实记录、身份与日期明确、实际使用字段口径、信息边界、合法操作与本地数据边界 | 来自人类目标和当前模拟用途；不能因本次收缩门槛而省略 |
| 全部历史上市 A 股、退市与 ST 及完整目标字段 | 是长期目标；若声称全集成立必须满足，但不自动排除已确认证券的局部用途 |
| 来源字段必须独自无前缀定义全集、只能以官方逐字段定义确定所有语义 | 是 Orchestra 对判别方法的进一步选择；未证明每项都是当前有限用途的必要条件，也不能用不充分的来源或猜测替代 |
| 在线历史永不回改、可指定 revision、新克隆严格等价重取 | 是从固定可复现输入收紧而来的条件；现有本地输入重用与在线服务长期不变必须分开，用户未承诺发布真实样本 |
| 客户端许可不能扩张为服务器数据许可 | 是实际权限区别；未取得明确条款不等于已确认禁止，也不等于已经获得许可，须针对具体本地使用或发布行为判断 |

本次只替换现有状态表达和调查条件提示：同一状态先概括仍有效能力（接口范围与验证覆盖分开），再说明当前方向；请求把旧任务中的资格条件标为此前模型判断，吸收时按当前用途重判；系统要求条件对应当前用途及依据，区分缺证与反证、局部复现与在线不变。没有改写生产 `state.md`、来源接受结论或旧错误现场，也没有增加状态字段、能力账本或自动修复。

实现验证仍为 38 项 Orchestra 聚焦测试、Ruff 与差异检查通过。除提示文本和对应测试之外，没有修改决策逻辑、控制文件协议或模型接入。

只做了一次真实回放：冻结旧第四轮最后一次实际请求的基础系统提示、用户材料、预算零及已读工具返回，仅应用本次提示替换；工具名重新绑定到当次只读工具，仍访问当时未变化的 `stock-sim c768f99`。模型可继续使用正常只读循环，但没有重复抽取结果。输出只存本地，没有调用生产控制文件写回，也未被注入续跑。

回放的支持证据是清单输入与四个已验证窗口同时保留，并把新候选的 OHLCV 用途与长期全部字段分开。反对证据是仍沿旧报告将 BaoStock 说成已被证据排除为主来源，保留固定上游快照和新克隆重取门槛，没有充分重新说明这些条件为何对当前本地用途必要。回放没有证明两问题都已解决；此后没有再次修改提示或重跑挑选。

正常续跑从未手修的原生产状态开始，完成三个真实 worker 任务和四次 fresh 决策：

| 决策现场 | 能力概况观察 | 当前用途与调查条件观察 |
| --- | --- | --- |
| `round-01` | 自行读取仓库后恢复本地清单输入，四样本只作验证覆盖 | 自主选择交易所官方组合，允许其只承担身份或全集补充角色；仍继承 BaoStock 固定上游缺口的主管道排除结论 |
| `round-02` | 清单接口、四样本和有限跨期边界继续保留 | 吸收官方个案与规则后转向既有分钟快照，明确不以 ST、估值等全部长期字段否决 OHLCV 用途；但任务仍以三市场骨架资格裁定替代更细的可用范围判断 |
| `round-03` | 概况仍正确区分接口与验证范围 | 读到宿主补取的分类表前缀，承认实际字段和 2026 行，不把它变成 2025 覆盖；明确禁止再分发不自动否决允许的本地用途。与此同时，又把全年三市场声明设为下一候选进入字节验证前的条件 |
| `round-04` | 最后吸收仍保留清单能力，没有重现原四样本缩窄 | 接受五候选都未达到该门槛，转为外部数据等待；没有充分说明为何有限候选失败足以否定其他当前授权内的局部推进 |

两个 worker 的输出也显示条件传递尚未解决：分钟快照报告以任务明确的骨架条件为依据，把本地与远端对象尚未绑定、身份缺项和未取得分类分别合并为未通过；最后报告将五候选未达元数据门槛表述为没有可以合法进入字节验证的对象，而部分首个失败实际上是字段或覆盖不足，不都是已确认访问禁止。这不表示应当接受任一来源，也不允许绕过真实许可、身份或字段语义，只说明任务自行设定的使用范围和资格标签仍可能替代独立必要性论证。

宿主只完成报告已请求的机械补取：对固定分类 CSV 读取前 65,536 字节，HTTP 206；原 worker 报告单独保留，仓库报告明确标注后续事实。没有下载分类全表，没有提供业务候选，也没有替 Orchestra 更改来源结论。

最终决定为“等待”，没有第四个 worker。实际运行预算已用尽；模型同时把全项目概括为外部数据阻塞。后一个判断仍依赖它自选的全年三市场骨架门槛，本次不能据五个候选证明项目不存在其他可推进工作，也没有把它上升为人类必须改权限或提供数据的结论。

结论边界：能力概况在本次四次续跑决定中保持，比原错误现场有支持性改善；三任务均为来源调查，尚未检验更广泛的跨主题长期保留。来源条件已出现局部用途、补充角色和本地/再分发的区别，但旧裁定惯性、缺证与反证混用及把长期骨架当成唯一实现前置仍有反例。一次回放和这一短样本不能证明稳定改善、因果归因或路线最优。

本地现场位于 `/root/.hermes/orchestra/ff3397784d64103c/observations/20260909-followup/`：`replay-*`、`round-01` 至 `round-04` 的输入、上下文与决定，前三轮 worker 输入及结果，第二 worker 的原始报告。它们和旧现场均不发布。临时捕获及回放脚本仅在 `/tmp` 使用，不成为产品日志系统。试运行提交 `ddad14d`、`803644d`、`413c190` 只用于定位实际任务材料，不作为决策质量评分。

### 10.11 2026-09-10 自拟路线与恢复条件

本次只核对上次最终等待的条件链，不重新调查五个候选。旧现场 `round-04-input.md:236—239` 的预算零独立支持不启动 worker；任务第 2、8 项却将自拟的全年三市场骨架设为当前实现入口，并预设未找到候选时转为外部阻塞。最终决定第 12—16、21 行把条件延伸到项目恢复，要求新预算之外还出现新的数据、身份或授权事实。对象不存在、访问尚未获准、字段缺失分别可能阻止对应操作或用途，但现场没有独立证明自拟路线是项目唯一必要路径；也不需要证明所有路线不可行才允许等待。

先完成一次隔离时序对照，未同时修改提示：保留原实际 system、原只读工具、意图、权限、资源、Git 事实及预算零，先提供原状态中的能力概况，再提供来源裁定、旧任务、原方向、结果与原工具返回的全部原文。同一 fresh 会话分时接收，不写生产控制文件。阶段一仍可读原仓库，实际主动读取了资格报告，所以旧判断通过工具提前出现；没有人为屏蔽这些不利材料，也没有重复对照。

对照最终仍把当前来源路线的失败扩为外部前置，并要求人类选择研究/商业用途或接受现有覆盖后再推进。它保留了能力边界，但没有显示可归因于顺序的范围校正。这个结果不能证明时序绝对无效：工具提前读取使干预不完整，且单次中间恢复过程本身增加了模型判断。没有据此把两段式固定为运行流程，也没有把对照输出或候选注入续跑。

随后只替换既有职责与结果说明：orchestra 可修订自拟阶段目标、路线和验收；worker 结论连同用途、条件与证据范围吸收；预算结束、路线受阻及确需外部条件的项目依赖分别说明，恢复条件对应实际受阻行为。设计的“等待”原先只解释外部依赖，本次同步纳入预算结束，避免为机械等待附加项目阻塞。没有新增控制字段、日志系统、评审角色或决策运行逻辑。

原始材料位于 `/root/.hermes/orchestra/ff3397784d64103c/observations/20260910-reconsider/`，仅本地保存；旧错误现场保留，正常续跑从未经宿主修写的生产状态开始。最多两个 worker 的预算仅属于本次研发调用。

本次提示替换的实现验证为 38 项聚焦测试、Ruff 和差异检查通过，没有重新运行第二次隔离对照。正常续跑完成两个真实 worker 及三次 fresh 决策，没有注入对照输出或人工业务候选：

| 现场 | 支持重新判断的可观察行为 | 仍需限定的结论 |
| --- | --- | --- |
| `round-01-decision.md:10—14` | 主动明确全市场骨架资格失败不证明所有局部推进都要等待，选择既有材料的限定用途判别 | 任务仍预写判别失败后恢复等待，不能只看初次选择就说失败分支已摆脱惯性 |
| `round-01-result.md:8—16` | worker 得到目标价格映射的实际反证，停止条件式实现，结论限定到该映射及估值用途 | 没有因为生成了任务就认定数据可用；该结果不否定其他用途或全部项目 |
| `round-02-decision.md:6、10—12` | fresh 吸收反证后没有执行预写的全局等待，重新区分旧来源的市场级失败与单证券用途，选择已定位对象的有界字节判别 | 实际许可、身份、时序和价格仍需满足；不是预定该来源被接受，也不证明其所有原资格缺口已经消除 |
| `round-03-decision.md:14—21` | 吸收限定用途结果后调整中程方向，明确只因预算零等待，恢复不再绑定新增数据或人类产品选择 | 仍保留完整市场骨架的条件性缺口；下一方向只有判断未执行，不是所有路线已被比较或最优性证明 |

宿主只按第二任务明确对象完成有界机械取得，没有推荐来源或替 worker 校准；数据本地忽略，worker 原始结果与宿主取得记录分开保留。试运行项目提交 `24a6711` 是限定假设反证，`a607662` 是下一任务产物，仅用于定位实际过程，不把功能增加或股票测试数量当作决策质量指标。

结论是短程中观察到了自拟阶段目标和失败分支被修订、条件性来源结论被重新限定，以及预算等待与外部阻塞分开；没有证明稳定性、因果归因或新路线最优。时序对照没有干净隔离旧判断，不能据此判断两段式绝对无效；本次没有固化两段流程、追加反复回放或启动第三个 worker。原始 system、请求和工具返回不发布。
