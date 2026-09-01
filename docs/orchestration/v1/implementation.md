# orchestra 首版实施与验收记录

> 本文前半部分保留首版实现选择和行为边界，末尾记录 2026-09-01 实际落地的代码、模型接入、测试与真实三轮验收。股票模拟系统仍是首个真实长期目标，但尚未初始化。

## 0. 当前实现状态

**Orchestra v1 已实现并通过真实一对一闭环验收。**

实际代码：

```text
hermes_cli/orchestra_v1.py
hermes_cli/codex_worker.py
scripts/orchestra_v1.py
tests/test_orchestra_v1.py
tests/test_codex_worker.py
```

当前已经可以：

```text
初始化一个目标项目
→ 每次 decide 创建全新的 Hermes AIAgent
→ 从当前状态、任务、最近结果与 Git 事实重建上下文
→ 只允许 Orchestra 读取和搜索目标仓库
→ 保存完整的新 state.md、task.md 和 decision.txt
→ 根据决定新建或恢复真实 Codex thread
→ 将 worker 最终结果写入 result.md
→ 在下一次 fresh decide 中自动使用该结果
```

首版命令已经可运行：

```text
python scripts/orchestra_v1.py init --project <path> --goal-file <path>
python scripts/orchestra_v1.py decide --project <path> [--human <text>]
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path> [--human <text>]
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
├── state.md
├── task.md
├── result.md
├── decision.txt
├── worker-thread.txt
└── last-orchestra-output.md
```

`project-key` 由仓库绝对路径的稳定摘要生成。首版不建设项目注册中心。

### 2.1 `state.md`

唯一长期语义材料，保存当前有效的：

- 人类意图；
- 已确认项目事实；
- 承重假设及失效信号；
- 当前收敛缺口；
- 重要停止路线；
- 当前决定残留；
- 待人类决定事项。

它由每轮 orchestra 整体重写。

实现禁止：

- 固定章节校验；
- 版本号；
- 迁移；
- 新旧格式兼容；
- 追加式运行日志；
- 自动修复历史状态；
- 要求 worker 维护该文件。

### 2.2 `task.md`

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

### 2.3 `result.md`

最近一次 worker 最终回答，整体覆盖。

worker 应尽量说明：

- 实际改变了什么；
- 证据在哪里；
- 哪些内容没有完成；
- 是否发现改变项目方向的新事实；
- 是否有承重假设失效。

该文件只是下一轮线索，不自动成为项目事实。

### 2.4 `decision.txt`

只保存当前机械动作之一：

```text
继续当前任务
开始新任务
等待
询问人类
停止
```

它不是项目状态机，只用于决定是否启动、恢复或跳过 worker。

### 2.5 `worker-thread.txt`

保存当前任务对应的 Codex `thread.id`。

- 开始新任务时覆盖；
- 继续当前任务时恢复；
- 文件缺失或会话无法恢复时明确失败，不自动创建替代路线；
- 旧任务 thread 不进入下一轮 orchestra 默认上下文。

### 2.6 `last-orchestra-output.md`

保存本轮 orchestra 原始最终回答，便于调试。

它不自动进入下一轮上下文。只有人类排查输出解析问题时读取。

## 3. 首版命令

首版脚本提供五个入口：

```text
python scripts/orchestra_v1.py init --project <path> --goal-file <path>
python scripts/orchestra_v1.py decide --project <path> [--human <text>]
python scripts/orchestra_v1.py run-worker --project <path>
python scripts/orchestra_v1.py step --project <path> [--human <text>]
python scripts/orchestra_v1.py status --project <path>
```

### 3.1 `init`

只做机械初始化：

1. 确认项目目录存在；
2. 计算 `project-key`；
3. 创建控制目录；
4. 把人类目标写入最小 `state.md`；
5. 创建空的 `task.md`、`result.md` 和 `decision.txt`；
6. 不调用模型，不分析项目，不生成路线图。

重复初始化已有项目时必须拒绝，除非人类显式要求覆盖。首版不设计状态合并。

### 3.2 `decide`

完成一次真正的全新 orchestra 决策轮：

1. 读取 `state.md`；
2. 读取当前 `task.md` 和最近 `result.md`；
3. 读取本轮人类变化；
4. 机械收集以下 Git 事实：
   - 仓库根目录；
   - 当前分支；
   - 当前提交；
   - `git status --short`；
   - `git diff --stat`；
   - 最近若干提交标题；
5. 创建新的 `AIAgent`；
6. 注入固定职责、项目状态、本轮变化和机械事实；
7. 允许 orchestra 按需读取和搜索仓库；
8. 保存原始输出到 `last-orchestra-output.md`；
9. 解析当前决定、完整新状态和 worker 推进说明；
10. 解析成功后原子覆盖 `state.md`、`task.md` 和 `decision.txt`；
11. 结束本轮 orchestra 会话。

orchestra 输出首版只约定以下形状：

```markdown
决定：开始新任务

# 项目状态
完整的新状态正文

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
→ thread/start
→ 保存新的 thread.id
→ turn/start(task.md)

继续当前任务
→ thread/resume(worker-thread.txt)
→ turn/start(task.md)

等待 / 询问人类 / 停止
→ 不启动 worker
```

worker 运行在目标仓库目录，使用 Codex 的工作区写入隔离。首版默认不允许无限制宿主机访问；需要网络时由人类通过已有 Codex 配置决定，不在 orchestra 中新增权限系统。

执行期间：

- 终端前台显示主要事件；
- 收集最终代理消息；
- `turn/completed` 后整体覆盖 `result.md`；
- 不自动启动下一轮 orchestra；
- 失败、取消和中断也写入清楚的结果摘要，供下一轮判断。

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

每轮请求由四部分组成：

```text
稳定职责
  orchestra 的角色、禁止项和输出要求

当前项目状态
  state.md 原文

本轮变化
  人类变化、当前 task.md、最近 result.md、Git 机械事实

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
- 输出完整替换后的项目状态，而不是状态补丁。

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

新任务启动后，旧 thread 仍保存在 Codex 自己的会话存储中，但不再属于当前运行状态，也不自动提供给新的 orchestra 或 worker。

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
- 初始化不会静默覆盖已有状态；
- 文件更新使用临时文件后原子替换；
- 每次 `decide` 使用不同 orchestra `session_id`；
- 请求体不包含上一轮原始 orchestra 输出和完整 worker 对话；
- 解析失败不覆盖状态；
- 五种决定都能正确识别；
- `开始新任务` 清除当前 worker thread 后创建新 thread；
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

`hermes_cli/orchestra_v1.py` 实现：

- 按项目绝对路径生成稳定 `project-key`；
- 创建并管理六个自由格式控制文件；
- 单文件临时写入后原子替换，并保留已有 symlink；
- 机械收集 Git 根目录、分支、提交、状态、diff stat 和最近提交；
- 解析五种决定以及完整的新项目状态和 worker 推进说明；
- 每次 `decide` 创建新的 `AIAgent`，不传旧对话和父会话；
- 关闭普通上下文文件、记忆、人格和旧 Kanban 工具；
- 为本轮临时注册只读、只限目标仓库路径的读取与搜索工具，结束后注销；
- 根据当前决定启动新 worker、恢复旧 worker 或跳过 worker；
- 保存 worker thread 与最终结果，并提供机械 `status` 输出。

`hermes_cli/codex_worker.py` 在保留旧 Kanban lane 接口的同时增加独立的最小 `run_codex_turn`：

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
- 新任务清除旧 thread 并在 turn 前保存新 thread；
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