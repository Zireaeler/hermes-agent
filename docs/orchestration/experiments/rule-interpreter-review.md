# 第二对实验：JSON 决策规则解释器

> 状态：已完成。本文保留第二对协议和结果；因 treatment 前测试策略混杂，不能单独证明 review 因果效果。最终判断见 `orchestra-design.md`。

## 1. 目标

第二对仍只验证同一个问题：

> 相比普通独立 review，只允许 `keep / remove / merge / simplify / doubt` 的独立负向回顾，能否在不损害已有能力的情况下，让后续维护更容易？

第一对 scheduler 实验中，负向组成品在两个维护任务上都更快、更低成本，但两组从 R1 起就选择了不同存储架构，而且 scheduler 的时间、并发和 SIGTERM 测试产生了 evaluator 时序噪声。第二对改用完全确定、无持久状态、无真实时间和无并发的规则解释器，检查维护收益是否能跨问题类型复现。

这仍不是完整 Orchestra 的验证。结果不好就停止，不建设实验平台或 Runtime 基础设施。

## 2. 共同约束

两组从空目录开始，完成完全相同的 R1–R7。

- Python 标准库；
- 不使用网络、数据库、常驻进程或外部服务；
- 项目入口固定为 `python verdict.py`；
- policy 和 input 从 JSON 文件读取；
- stdout 输出一个 JSON 文档；
- 输入或 policy 错误时返回非零退出码，并在 stderr 输出含稳定 `error.code` 的 JSON；
- 测试只比较退出码和解析后的 JSON，不比较键顺序、空格、内部类名或模块结构；
- Worker 自行决定直接递归、节点对象、AST、visitor、表驱动或其他内部实现；
- 每轮只给当前新增需求，不透露后续轮次、最终变体或维护任务。

统一命令：

```text
python verdict.py eval --policy POLICY.json --input INPUT.json
python verdict.py eval --policy POLICY.json --input INPUT.json --explain
```

`--explain` 到 R7 才成为要求。

## 3. R1–R7

### R1：单条原子条件

policy 包含一条规则和默认决策：

```json
{
  "rules": [
    {
      "id": "r1",
      "when": {"op": "eq", "path": "/country", "value": "CN"},
      "decision": "allow"
    }
  ],
  "default": "deny"
}
```

要求：

- 支持 `eq`、`ne`；
- path 只需支持 `/field` 形式的顶层字段；
- 比较 JSON 标量；
- 条件为真时返回规则 decision 和规则 ID；
- 条件为假时返回 default，`matched_rule` 为 `null`。

输出至少包含：

```json
{"decision": "allow", "matched_rule": "r1"}
```

### R2：嵌套 JSON Pointer 和缺失语义

新增要求：

- path 使用 RFC 6901 JSON Pointer，可访问嵌套对象和数组；
- 支持 `~0`、`~1` 转义；
- 显式 `null` 是普通 JSON 值；
- 路径缺失与值为 `null` 不同；
- 路径缺失时，原子条件结果为 `false`；
- 非法 pointer、非法 JSON、非法 policy 和非法 input 返回机器可读错误；
- policy 错误使用 `invalid_policy`，input 错误使用 `invalid_input`。

### R3：布尔组合

新增条件：

```json
{"all": [COND1, COND2]}
{"any": [COND1, COND2]}
{"not": COND}
```

要求：

- 任意嵌套；
- `all`、`any` 的子项按数组顺序求值；
- 空 `all` 为 `true`；
- 空 `any` 为 `false`；
- `not` 恰好包含一个子条件；
- 非法形状属于 `invalid_policy`。

R3 公开测试通过后运行 Review 1。

### R4：有序多规则

新增要求：

- policy 可以包含任意数量规则；
- 按数组顺序求值；
- 第一条结果为 `true` 的规则获胜；
- 普通模式允许短路，不求值后续规则；
- 所有条件为 `false` 时返回 default；
- 规则 ID 必须是非空字符串且在 policy 中唯一；
- decision 和 default 必须是 JSON 标量。

### R5：有类型的叶子操作符

新增操作符：

- `lt`
- `lte`
- `gt`
- `gte`
- `in`
- `starts_with`

要求：

- 数值操作只接受 JSON number；布尔值不视为数字；
- `starts_with` 只接受字符串；
- `in` 表示 input 中的值是否与 policy 数组中的某项按 JSON 值相等；
- input 值类型不匹配时条件为 `false`；
- policy 中操作符参数类型错误属于 `invalid_policy`；
- 不增加 regex、日期、locale 排序或浮点近似比较。

R5 公开测试通过后运行 Review 2。

### R6：数组量词和当前元素作用域

新增：

```json
{
  "some": {
    "path": "/orders",
    "where": {"op": "gt", "path": "/amount", "value": 100}
  }
}
```

同时支持 `every`、`none`。

要求：

- 量词 path 相对当前求值输入解析；
- `where` 中的 pointer 以当前数组元素为根；
- 量词和布尔组合可任意嵌套；
- 空数组：`some=false`、`every=true`、`none=true`；
- path 缺失或现有值不是数组时结果为 `false`；
- 不增加根对象回跳、变量绑定或表达式语言。

### R7：确定性解释树

新增 `--explain`。

输出在原有字段之外包含：

```json
{
  "decision": "allow",
  "matched_rule": "r2",
  "explanation": {
    "kind": "all",
    "result": true,
    "children": []
  }
}
```

解释节点至少区分：

- `comparison`
- `all`
- `any`
- `not`
- `some`
- `every`
- `none`

要求：

- 子节点顺序与实际求值顺序一致；
- 短路时只报告实际求值过的节点；
- comparison 节点包含 `op`、`path` 和 `result`；
- 量词节点包含 `path`、`result` 和实际求值子节点；
- 不包含时间、耗时、对象地址或源码位置；
- 不固定 JSON 键顺序；
- 不使用 `--explain` 时，R1–R6 输出保持不变。

R7 公开测试通过后运行 Review 3。

## 4. 两组 review

Review 发生在 R3、R5、R7，通过当轮公开测试后才运行。

### 普通组

Reviewer 可以提出：

- 修复真实行为问题；
- 完成需求中确实缺失的实现；
- 普通重构；
- 删除、合并或简化；
- no-op。

### 负向组

Reviewer 只能提出：

- `keep`
- `remove`
- `merge`
- `simplify`
- `doubt`
- no-op

不得增加产品能力、替代架构、兼容层、流程、未来扩展点或新的持久状态。

两组 reviewer 都只看截至当前轮的需求、当前代码、项目内测试和公开测试通过事实；不看另一组、此前 review 辩护、最终测试或维护任务。相同配置 executor 执行本组建议，`doubt` 不自动转成修改。

具体共同要求沿用 [`review-prompts.md`](review-prompts.md)。

## 5. 公开测试

R1–R7 每轮都有独立外部黑盒测试，只通过 CLI 运行，不 import 项目代码。

测试使用临时目录写入 policy 和 input JSON，检查：

- 退出码；
- stdout JSON；
- stderr `error.code`；
- 旧轮次行为回归；
- 顺序、短路、类型和作用域语义。

测试不依赖真实时间、随机数、文件遍历顺序或平台权限。公开测试在两组对应 Worker 启动前写好；发现 evaluator 自身错误时必须单独记录并对两组统一处理，不能把某组实现反馈给 Worker。

## 6. 最终未公开行为变体

最终测试只变化 R1–R7 已要求的输入，不增加新功能：

1. JSON Pointer 的 `~0`、`~1` 和数组索引；
2. 缺失路径、显式 `null`、空字符串和零；
3. 深层 `all`、`any`、`not`；
4. 空组合边界；
5. 多规则顺序和短路；
6. JSON number 与 boolean 严格区分；
7. `in` 中对象、数组和标量的 JSON 相等；
8. 多层 `some`、`every`、`none` 当前元素作用域；
9. 量词指向缺失值和非数组；
10. explain 只包含实际求值节点；
11. explain 与普通模式产生相同 decision 和 matched rule；
12. policy 验证错误不会因运行时短路而漏掉。

最终测试在正式两组启动前写好，只用于评分，结果不回灌。

## 7. 两个独立维护任务

R7 Review 3 executor 后，从每组成品分别复制两份。四个全新维护 agent 独立运行，不看另一组结果、review 历史或 evaluator 测试。

### 维护任务一：缺失路径改为三值语义

把 R2 的“缺失路径直接为 false”改为：

```text
true / false / unknown
```

只有路径缺失产生 `unknown`；显式 `null` 仍是普通值，现有类型不匹配规则不变。

要求：

- `not T=F`、`not F=T`、`not U=U`；
- `all`：有 F 则 F，全 T 则 T，否则 U；
- `any`：有 T 则 T，全 F 则 F，否则 U；
- `some` 使用 any 规则；
- `every` 使用 all 规则；
- `none = not some`；
- 有序规则遇到 F 继续、遇到 T 返回 decision、遇到 U 立即返回未决定；
- 未决定输出至少包含：

```json
{
  "status": "unknown",
  "decision": null,
  "matched_rule": "r1",
  "missing_paths": ["/user/age"]
}
```

- explain 节点的 result 改为 `true`、`false` 或 `unknown`；
- 既有 R1–R7 非缺失输入行为保持。

该任务测试原子求值、组合、量词、规则选择、explain 和输出之间的语义传播范围。

### 维护任务二：彻底删除 explain

产品不再提供解释能力：

- 删除 `--explain`；
- 传入时返回非零；
- 删除 explanation 输出；
- 删除只为解释存在的节点、trace、visitor、中间结果、测试和兼容路径；
- 保留 R1–R6 的全部决策行为；
- 不保留隐藏开关、弃用转发或替代解释 API；
- 不增加外部依赖。

该任务测试 explain 是否与核心求值合理分离，以及 Agent 是否能直接删除自身创建的结构。

## 8. 记录和判断

主轨迹记录：

- 每个 Worker、reviewer、executor 的实际模型；
- wall time、token、成本、permission denial；
- 公开测试和最终测试结果；
- review 建议及实际执行；
- 最终文件数和 LOC，仅作旁证。

维护任务优先记录：

- 是否正确完成；
- 外部维护测试和旧能力回归；
- wall time、token、成本；
- 失败尝试；
- 修改文件和模块范围；
- 是否先撤销此前 review 引入的结构；
- 是否增加与任务无关的机制。

第二对若与第一对同方向，说明负向回顾值得继续研究；若方向冲突，再分析任务类型和实现差异，只有必要时才考虑第三对。

## 9. 模型和隔离

沿用第一对已验证的运行条件：

- GPT-5.6 Sol；
- `high` reasoning effort；
- Bubblewrap 严格隔离；
- `/workspace` 是唯一项目工作目录；
- 空 `/root`；
- 独立 HOME、Claude 配置和 session；
- `--bare`、`--safe-mode`、关闭 skills/plugins/MCP；
- 不暴露宿主机 `AGENTS.md`、`CLAUDE.md`、memory、历史会话或另一组结果；
- 每个 agent turn 为有限任务，不自动 retry、循环 review 或生成下一轮；
- 不建设数据库、ledger、状态机、审批流程或实验平台。

## 10. 第二对实际结果

第二对已经完整运行。

能力结果：

- 两组 R1–R7 公开测试全部通过；
- 两组最终 10 项未公开行为测试全部通过；
- 两组两个维护任务都正确完成，无旧能力回归；
- 普通 review 修复了孤立 surrogate 导致半截 stdout JSON 的真实问题；
- 负向 review 修复了超长合法数组索引触发 `internal_error` 的真实问题。

维护结果：

| 维护任务 | 普通组 wall time | 负向组 wall time | 普通组成本 | 负向组成本 |
|---|---:|---:|---:|---:|
| 三值缺失语义 | 401 秒 | 272 秒 | USD 0.751 | USD 0.473 |
| 删除 explain | 304 秒 | 174 秒 | USD 0.545 | USD 0.366 |

两个维护任务合计，负向组 wall time 少约 37%，成本少约 35%。方向与第一对 scheduler 一致。

但第二对仍存在关键混杂：普通组在 R1、第一次 review 之前就生成了项目内测试文件，并最终增长到 907 行；负向组从 R1 起没有项目内测试文件。两组最终实现文件本身几乎同规模：566 行与 571 行。普通维护 agent 必须同步更新或删除大量项目内测试，负向维护 agent 只需修改实现并由统一外部 evaluator 验证。

第一对也出现了相同的 treatment 前测试策略分叉。因此两对结果可以支持“较小、没有大量 Agent 自建测试负担的成品更容易维护”，但仍不能证明负向 review 导致了这种成品。第二对中负向 review 实际只执行了两个小 cleanup、一个边界修复和一个 no-op，并没有删除测试。

不应据此建设 Orchestra Runtime。若继续验证，不应原样跑第三对，而应先消除 treatment 前混杂：用共同 Worker 完成 R1–R3，在第一次 review 前复制完全相同的代码和测试，再分别施加两种 review。
