# orchestra 首版实施计划

> 本文定义真正需要写出的首版代码。当前仓库里还没有可运行的 orchestra，只有设计文档。股票模拟系统是首个真实目标，但必须等最小 orchestra 闭环已经运行后再开始。

## 0. 当前状态与首版完成标准

当前已有：

- orchestra 与 worker 的职责边界；
- 全新 orchestra 决策轮的上下文原则；
- 一对一首版范围；
- 股票模拟系统目标定义。

当前没有：

- 启动全新 orchestra 的实际命令；
- 项目状态与本轮结果的自动组装；
- orchestra 输出到 worker 任务的实际传递；
- worker 新建与恢复会话的实际接入；
- 一次完整的一对一运行闭环。

所以首版不能从股票系统开始。正确顺序是：

```text
先实现机械闭环
→ 用极小仓库确认会话、状态和结果传递正确
→ 再把股票模拟系统接入成为第一个真实长期目标
```

首版完成必须同时满足：

```text
人类可以初始化一个目标项目
→ 每次决定都启动全新的 orchestra 会话
→ orchestra 能读取当前项目状态并按需检查仓库
→ orchestra 只生成一个当前 worker 任务
→ 系统能启动或恢复一个真实 Codex worker
→ worker 结果能回到下一轮 orchestra
→ 连续运行三个决策轮不需要人工复制粘贴材料
```

首版仍然由人类显式触发，不自动持续循环。

## 1. 首版固定选择

### 1.1 代码位置

先只增加少量文件：

```text
hermes_cli/orchestra_v1.py
hermes_cli/codex_worker.py
scripts/orchestra_v1.py
tests/test_orchestra_v1.py
tests/test_codex_worker.py
```

不要一开始建立 `orchestra/` 大目录、插件系统或通用运行时。只有单文件已经明显无法维护时再拆分。

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