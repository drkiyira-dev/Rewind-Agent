# 评测方案 v0.1

本文件在查看 `results/eval.json` 之前写定。实现若与本文件冲突，修实现，不改判定。

## 要检验的问题

在受控纯计算和一条模拟账本上：

1. 已知错误步骤之后，未受影响的纯计算能否不重算，且业务槽位仍与独立参照一致。
2. 这种少重算，是否超过普通输入哈希增量（B3）。
3. 不安全复用、重复入账、把拒绝说成成功，是否会被判定为失败。

不检验故障归因、token 账单、真实 API、全局最优。

## 工作流与种子

手写场景由 `rewindagent/cases.py` 固定，不在看到结果后增删。

随机纯图：20 个，种子 `20261106` 到 `20261125`。生成器是 5 个节点的前向图，工具只含 `set`、`inc`、`add`。每个图有一个 `removed` 故障实例。

计时重复：只对 diamond 的 B4 用 `time.perf_counter_ns` 跑 5 次，报告中位数。这 5 次不是新样本。

确定性重复不计算置信区间。样本单位是场景，不是计时次数。`failure_localization` 固定为 `given`。

`actual_model_calls = 0`，`actual_token_usage = 0`。工具重算次数写入 `simulated_cost`，不得称为 token 或费用。

## 基线

| 代号 | 定义 |
|---|---|
| B0 | 空缓存，从 genesis 全量解释。账本用原账的副本，不清空 |
| B1 | 恢复故障之前最近的**显式**检查点，跳过该点及以前的实例，其后全量重算。没有合格检查点则为 `no_checkpoint`，不得暗中改跑 B0 |
| B2 | 恢复故障实例之前的自动前缀快照，跳过此前实例，从故障实例起重算 |
| B3 | 与 B4 相同的实例+实现+配置+输入值哈希缓存。不拒绝未声明读，不拒绝 escapes，不可逆步骤会再次调用账本 |
| B4 | 契约选择性恢复：不恢复业务检查点；违规缓存不命中；escapes 拒绝；不可逆载荷变化拒绝且不投递；验证器不读参照答案 |

同一场景的初始槽位、故障模式、工具、账本副本和成本（每重算 1）相同。

若某场景 B1 与 B2 的重算实例集合相同，摘要里标 `b1_equals_b2=true`，不当成两份独立证据。

## 指标与分母

每次方法尝试一行，拒绝和失败留在表里。

- `recovery_success`：状态恰好是 `recovered`，业务槽位与独立参照一致，`duplicate_applied=0`，`safety_failures` 为空。
- 成功分母一：全部尝试（场景 × 方法）。
- 成功分母二：只含预先标为 `success_eligible` 的 B4 尝试。拒绝场景不进这个分子。
- 业务一致性：`oracle_match`。参照由评测侧公式或干净解释产生，规划器函数不接收参照字典。
- 重算数、复用数、前缀跳过数、废弃数、新事件数。
- `duplicate_applied`、`external_attempts`。
- 拒绝、`persistent_fault`、`no_checkpoint`、`write_conflict`、装载失败的计数。
- `plan_ns` 与 `total_ns`。

`recovery_success` 为假的拒绝，测试仍可以因为“符合预定状态”而通过。那种通过不算恢复成功。

## 预定结果

手写场景里，B4 的预定状态：

| 场景 | 预定 B4 状态 | 是否可算恢复成功 | 备注 |
|---|---|---|---|
| linear | recovered | 是 | 重算集合与 B2 相同，对后缀无优势 |
| diamond | recovered | 是 | 重算集合必须真包含于 B2。R 复用 |
| multi_reader | recovered | 是 | A 与 B 都重算 |
| multi_version | recovered | 是 | 只有坏版本的读者重算；覆盖写及其读者可复用 |
| control_flip | recovered | 是 | 旧分支废弃，新分支出现 |
| loop_carried | recovered | 是 | 展开实例，后续迭代重算 |
| new_event | recovered | 是 | 原轨迹没有的实例被执行 |
| undeclared | recovered | 是 | spy 不得复用；输出等于新的 hidden |
| escape | refused_unobservable_dependency | 否 | |
| impl_change | recovered | 是 | inc 实例重算 |
| unbounded | refused_unbounded_control | 否 | |
| irreversible_same | recovered | 是 | 账本行数与载荷不变，external_attempts=0 |
| irreversible_changed | refused_external_payload_changed | 否 | 旧账保留 |
| persistent | persistent_fault | 否 | 该实例只进入重算列表一次 |
| random_00..19 | recovered | 是 | 与测试内独立公式一致 |

另外两条不进入成功表、但必须成立：

- 写冲突：直接调用写入守卫，槽位不变。
- 坏计划：手写“复用了故障读者”的事件，验证器拒绝。
- 损坏日志、损坏检查点：装载抛错或 B1 为 `checkpoint_corrupt`。
- 内部快照恢复不改变账本。

## 安全门禁

下列任一出现，则安全门禁失败：

- 任一方法 `duplicate_applied > 0`。
- B4 的 `safety_failures` 非空。
- B4 复用了受影响闭包中的实例（本原型的损坏方式总会改变值哈希，因此闭包里的实例不应复用）。
- 不可逆场景的账本条目被删除或改写。
- 损坏日志被忽略后继续给出 `recovered`。

B3 在 `undeclared` 上复用违规缓存，预定为安全失败。这不记成 B3 的恢复成功，也不用来证明 B4 “比一个坏基线更好所以安全”。B4 自己的安全门禁是零违规，不是相对 B3 的差值。

## 收益判定

预先写死，不因结果好看而改：

- `diamond` 上若 B4 重算数不小于 B2，则“少于后缀重跑”不成立。
- 所有 `success_eligible` 且纯计算的场景上，若 B4 与 B3 重算数全部相同，则结论是：纯计算收益来自普通增量缓存，不是新增算法。
- `linear` 上 B4 与 B2 重算数相同是预定事实，必须写进摘要，不能删掉该场景。
- 枚举只对 diamond 这一张 5 节点图。相符也只写成“这张图上与独立枚举的最小变化集相同”。

## 没有跑的东西

不跑模型，不跑 PaperCore，不跑 Crab/LangGraph/VCR 的真实程序做对照。那些项目的行为证据在 `docs/review.md`，不混进本原型的性能数字。
