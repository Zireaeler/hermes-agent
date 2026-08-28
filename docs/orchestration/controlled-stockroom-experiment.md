# 第三次受控实验：本地库存管理 CLI

## 1. 为什么需要第三次实验

前两对完整实验中，负向组成品在四个维护任务上都更快、更低成本地完成修改，且没有损坏已测能力。但两对都在第一次 review 之前发生了相同混杂：普通组自行生成并持续维护大量项目内测试，负向组没有项目内测试。维护成本差异不能干净归因于 review。

第三次实验只修正这个问题：先由一个共同 Worker 完成 R1–R3，在第一次 review 前复制完全相同的代码、项目内测试和数据格式，再分别施加普通 review 与负向 review。之后两组继续相同 R4–R7。

仍只验证：

> 相比普通独立 review，只允许 `keep / remove / merge / simplify / doubt` 的负向回顾，是否会在能力不受损的情况下减少后续维护负担？

这不是 Orchestra Runtime 的建设任务。结果不足就停止，不增加实验平台。

## 2. 项目选择

项目是纯本地库存管理 CLI，入口固定为：

```text
python stockroom.py --store STORE.json COMMAND ...
```

选择它的原因：

- 与 scheduler 的时间、并发和进程管理不同；
- 与规则解释器的纯递归求值不同；
- 有真实的本地状态、批量导入、调拨、预留和派生报表；
- 可通过 CLI 与显式 CSV 做稳定黑盒测试；
- 后续既能增加盘点批次，也能彻底删除预留能力；
- 不需要网络、数据库服务、常驻进程或多进程并发。

数量全部使用非负十进制整数，不支持小数、负库存或单位换算。不测试文件锁、崩溃恢复、符号链接、权限、插件或通用迁移框架。

## 3. 受控分组

流程：

```text
共同 Worker 完成 R1 → R2 → R3
→ 共同基线通过 R1–R3 外部测试
→ 复制同一目录为 ordinary 和 negative
→ 两组分别运行 Review 1 和 executor
→ 分别完成相同 R4、R5、Review 2、R6、R7、Review 3
→ 最终测试和维护任务
```

复制发生在 Review 1 前，必须包含共同 Worker 创建的所有代码、测试和文档。两组初始 SHA256、文件清单和 LOC 记录在运行日志中。

两组唯一有意差异仍是 reviewer 动作空间。复制后的 Worker、reviewer 和 executor 使用相同模型、effort、工具、隔离和 prompt。

## 4. 共同 CLI 与错误输出

每个命令成功时 stdout 输出一个 JSON 文档。失败时：

- 返回非零退出码；
- stdout 为空；
- stderr 输出：

```json
{"error":{"code":"stable_code","message":"..."}}
```

测试精确比较 `error.code`，不比较 message 文本、JSON 键顺序、空格或内部存储格式。

内部 `STORE.json` 格式不是公共 API。外部测试只通过多个独立 CLI 进程复用同一个 `--store`，不会读取或修改内部文件。显式导出的 CSV 是公共行为。

## 5. R1–R7

### R1：商品目录

命令：

```text
python stockroom.py --store STORE.json init
python stockroom.py --store STORE.json item add SKU --name NAME
python stockroom.py --store STORE.json item update SKU --name NAME
python stockroom.py --store STORE.json item show SKU
python stockroom.py --store STORE.json item list
```

要求：

- `init` 创建空 store；重复执行不破坏数据；
- SKU 是非空、区分大小写的用户标识；
- SKU 创建后不可修改；
- 重复 SKU：`duplicate_item`；
- 未知 SKU：`item_not_found`；
- `item list` 按 SKU 升序；
- item 输出至少包含 `sku`、`name`；
- 无效操作不得改变已有状态。

### R2：地点和现有库存

命令：

```text
python stockroom.py --store STORE.json location add CODE --name NAME
python stockroom.py --store STORE.json location show CODE
python stockroom.py --store STORE.json location list
python stockroom.py --store STORE.json stock set SKU --location CODE --quantity N
python stockroom.py --store STORE.json stock show SKU --location CODE
python stockroom.py --store STORE.json stock list [--sku SKU] [--location CODE]
```

要求：

- location code 非空、唯一、区分大小写；
- 重复 code：`duplicate_location`；未知：`location_not_found`；
- `stock set` 设置绝对数量；数量是非负整数；
- 未出现过的 SKU/location 组合库存为 0；
- 未知 SKU 或 location 不能设置库存；
- `stock show/list` 至少输出 `sku`、`location`、`on_hand`；
- `stock list` 按 `(sku, location)` 升序；
- R1 store 继续可用。

### R3：增量调整和历史

命令：

```text
python stockroom.py --store STORE.json stock adjust SKU --location CODE --delta SIGNED_INTEGER --reason TEXT
python stockroom.py --store STORE.json history [--sku SKU] [--location CODE] [--after EVENT_ID]
```

要求：

- adjust 在当前库存上增加或减少；
- 结果低于 0：`insufficient_stock`，库存和历史都不改变；
- 每次成功的 `stock set` 或 `stock adjust` 产生单调递增整数 `event_id`；
- history 至少包含：`event_id`、`type`、`sku`、`location`、`before`、`after`、`reason`；
- `stock set` 可使用固定 reason；
- history 按 event_id 升序；
- 过滤器可组合；`--after` 只返回更大的 event_id；
- R2 已有库存继续可用，不要求为升级前状态伪造历史；
- 单次 CLI 失败不留下部分写入。

R3 通过后复制共同基线并运行 Review 1。

### R4：库存快照 CSV 导入导出

命令：

```text
python stockroom.py --store STORE.json inventory export --output FILE.csv
python stockroom.py --store STORE.json inventory import --input FILE.csv --mode merge
python stockroom.py --store STORE.json inventory import --input FILE.csv --mode replace
```

CSV 固定列：

```text
sku,item_name,location,location_name,on_hand
```

要求：

- 使用 Python 标准 CSV 语义，名称可含逗号、引号和换行；
- 导出只包含已经存在的库存组合，按 `(sku, location)` 排序；
- `merge` 创建不存在的商品和地点，更新已有名称，设置 CSV 出现的库存，不删除未提及数据；
- `replace` 后商品、地点和库存与 CSV 完全一致；
- history 保留；导入对每个实际库存变化产生事件；
- CSV 行无效、重复组合、数量非法或引用冲突时，整次导入失败且无部分状态/历史；
- 导出的 CSV 可由同版本重新导入；
- import/export 错误使用稳定错误码，如 `invalid_csv`、`duplicate_inventory_row`。

### R5：原子调拨和地点更名

命令：

```text
python stockroom.py --store STORE.json stock transfer SKU --from SOURCE --to DESTINATION --quantity N --reason TEXT
python stockroom.py --store STORE.json location rename OLD_CODE NEW_CODE
```

要求：

- transfer quantity 必须为正整数；
- 来源不足：`insufficient_stock`；来源=目标：`same_location`；
- 来源扣减与目标增加同时成功或同时失败；
- 一次调拨只产生一个逻辑历史事件，包含 `from_location`、`to_location`、双方 before/after；
- rename 的新 code 已存在：`duplicate_location`；
- rename 后当前库存、CSV 导出和后续操作只使用新 code；
- 用新 code 过滤 history 时能找到更名前同一地点的事件；
- 导入旧 code 时，把它当作普通新地点，不建立永久 alias；
- R1–R4 行为保持。

R5 通过后运行 Review 2。

### R6：库存预留与履约

命令：

```text
python stockroom.py --store STORE.json reservation create ID --sku SKU --location CODE --quantity N
python stockroom.py --store STORE.json reservation show ID
python stockroom.py --store STORE.json reservation list [--sku SKU] [--location CODE]
python stockroom.py --store STORE.json reservation release ID
python stockroom.py --store STORE.json reservation fulfill ID --reason TEXT
```

要求：

- reservation ID 非空且唯一；重复：`duplicate_reservation`；未知：`reservation_not_found`；
- quantity 为正整数；
- `available = on_hand - 活动预留总量`；
- create 不改变 on_hand，且 quantity 不得超过 available；
- release 删除活动预留，不改变 on_hand；
- fulfill 原子扣减完整预留数量并结束预留，产生库存历史事件；
- 已 release/fulfill 的 ID 不能再次操作；
- `stock show/list` 从本轮起输出 `on_hand`、`reserved`、`available`；
- adjust、set、transfer 不能导致任何地点 `on_hand < reserved`；
- location rename 后已有预留使用新 code 查询；
- reservation list 按 ID 升序。

### R7：补货阈值和短缺报表

命令：

```text
python stockroom.py --store STORE.json threshold set SKU --location CODE --quantity N
python stockroom.py --store STORE.json threshold clear SKU --location CODE
python stockroom.py --store STORE.json report shortage
python stockroom.py --store STORE.json report shortage --format csv --output FILE.csv
```

要求：

- threshold 是具体 `(sku, location)` 的非负整数；
- 短缺条件：`available < threshold`；
- JSON/CSV 行至少包含 `sku`、`item_name`、`location`、`location_name`、`on_hand`、`reserved`、`available`、`threshold`、`shortfall`；
- `shortfall = threshold - available`；
- 排序：shortfall 降序，再 SKU、location 升序；
- adjust、set、transfer、reservation create/release/fulfill、rename 后立即反映；
- shortage CSV 和 R4 inventory CSV 是不同格式，不能互相误用；
- clear 不存在阈值：`threshold_not_found`；
- R1–R6 store 继续可用。

R7 通过后运行 Review 3。

## 6. Review 规则

Review 1/2/3 分别在 R3/R5/R7 公开测试通过后运行。

普通 reviewer 可以提出真实 bug、缺失实现、普通重构、删除、合并、简化或 no-op。

负向 reviewer 只能提出 `keep/remove/merge/simplify/doubt` 或 no-op，不得增加产品能力、替代架构、未来兼容、流程、迁移框架或新状态。

Reviewer 只看截至当前轮的需求、当前代码/测试和公开测试通过事实；不看另一组、此前 review、隐藏测试或维护任务。Executor 只执行有事实依据的建议，`doubt` 不修改。

## 7. 最终隐藏测试

最终测试只变化 R1–R7 已有输入：

1. SKU/code 大小写和排序；
2. 名称中的逗号、引号、换行与 CSV round-trip；
3. import 整批失败无部分状态/历史；
4. replace 删除未提及目录/地点/库存但保留 history；
5. transfer 原子性和单事件；
6. rename 后历史过滤与 CSV；
7. 多预留的 available、release、fulfill；
8. adjust/transfer/set 不得侵占 reserved；
9. shortage 排序和实时派生；
10. shortage CSV 不能作为 inventory CSV 导入；
11. 多次独立进程调用后 event_id 单调；
12. 所有错误操作保持状态不变。

最终测试在正式实验开始前写好，只用于评分，不回灌。

## 8. 两个独立维护任务

R7 Review 3 executor 后，每组成品复制两份，四个新维护 agent 独立运行。

### 维护任务一：增加盘点批次

命令：

```text
stocktake start COUNT_ID --location CODE
stocktake count COUNT_ID --sku SKU --quantity N
stocktake show COUNT_ID
stocktake close COUNT_ID --reason TEXT
stocktake cancel COUNT_ID
```

要求：

- 同一 location 同时最多一个 open 批次；
- count 记录/更新 SKU 的实际点数；
- close 原子地把所有已盘点 SKU 的 on_hand 调整到点数；
- 每个差异产生正常 history，并包含同一 `stocktake_id`；
- 任一 count 小于活动 reserved 时，整个 close 返回 `count_below_reserved`，无库存/历史变化；
- 未盘点 SKU 不变；cancel 不改变库存；
- closed/cancelled 批次不能继续 count/close；
- shortage 报表立即使用新库存；
- 不增加外部依赖或通用 workflow。

### 维护任务二：彻底删除预留

要求：

- 删除所有 reservation 命令和只为它存在的代码/测试；
- `stock show/list` 不再输出 `reserved`、`available`；
- threshold/shortage 改为 `on_hand < threshold`，shortfall=`threshold-on_hand`；
- shortage JSON/CSV 删除 reserved/available；
- adjust/set/transfer 只需防止 on_hand<0；
- rename 不处理 reservation；
- 打开含旧 reservation 数据的 R7 store 时，读操作忽略这些记录，下一次成功写操作可直接移除；
- 不增加迁移命令、备份、tombstone、兼容视图、隐藏 alias 或恢复入口；
- 已发生的 fulfill 库存变化和普通 history 保留；
- R4 inventory CSV 继续可用。

## 9. 记录与判断

记录每个 agent turn 的实际模型、wall time、token、成本、permission denial、公开失败和修复轮。

维护任务优先比较：

- 正确性和回归；
- wall time、token、成本；
- 失败/返工；
- 修改文件和模块范围；
- 是否先撤销 review 引入结构；
- 是否增加无关机制。

LOC 和文件数只作旁证。

## 10. 模型与隔离

- GPT-5.6 Sol，high effort；
- Bubblewrap 严格隔离；
- 独立 HOME、配置和 session；
- 不读取宿主机 AGENTS.md、CLAUDE.md、memory、skills、历史会话、实验文档或另一组；
- evaluator 不挂载给 Worker、reviewer 或 executor；
- 每个 turn 有限，不自动 retry/循环 review；
- 不建设数据库服务、ledger、checkpoint、审批或实验平台。

## 11. 实际结果

第三次受控实验已经完整运行。

受控复制点：共同 Worker 完成 R1–R3 后，两组都只有同一个 `stockroom.py`，753 行、25,316 bytes，SHA256 完全相同，无项目内测试文件。

能力结果：

- 两组 R1–R7 公开测试全部通过；
- 最终隐藏测试的有效行为维度两组全部通过；
- 普通 review 和负向 review 都发现并修复了真实组合边界问题；
- 没有证据表明负向 review 损坏能力。

Treatment 阶段从 Review 1 到 Review 3 executor，两组各 10 个 agent turn：

| | 普通组 | 负向组 |
|---|---:|---:|
| wall time | 65.39 分钟 | 70.18 分钟 |
| 成本 | USD 6.347 | USD 7.443 |
| input token | 488,135 | 588,946 |

负向组 wall time 高约 7%，成本高约 17%。最终普通组 1,994 行，负向组 1,815 行，负向组少约 9%。

维护任务：

- 盘点批次：两组都把 `stocktake show/cancel` 实现成无 ID 命令，而契约要求 `show COUNT_ID`、`cancel COUNT_ID`；两组外部维护测试都失败，不能比较成功维护成本；
- 删除预留：两组都正确完成。普通组 463 秒、USD 0.910；负向组 413 秒、USD 0.900。成本基本相同，负向组 input token 反而更多。

受控实验消除项目内测试和初始架构混杂后，没有复现前两对显著维护优势。代码更少没有转化为可确认的净维护收益。

综合三次实验，当前证据不支持为负向 review 建设 Orchestra Runtime 或自动化系统。负向 review 可以发现真实问题和做局部 cleanup，但相对普通独立 review 的稳定净增量没有在受控实验中成立。实验到此停止。
