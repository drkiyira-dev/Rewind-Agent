# 第二轮预期（运行前固定）

本文件在 `results/round2.json` 生成之前写定。允许的结果是集合。实现不得在看到结果后改这些集合、削弱 B3+，或把拒绝从总数里删掉。

原始 `results/eval.json` 不覆盖。本轮命令是：

```bash
python3 -m rewindagent round2 --out results/round2.json
```

## 复现门禁

先复跑 `python3 -m unittest discover -s tests -v` 和 `python3 -m rewindagent eval --out results/eval-rerun.json`。除计时中位数外，行级语义字段必须与 `results/eval.json` 一致。不一致则停止，不跑本文件里的新实验。

## 口径

每条方法记录：

| 字段 | 含义 |
|---|---|
| applicable | 该方法能否执行。附原因 |
| attempted | 是否进入了恢复尝试。不适用为假 |
| outcome | `recovered`、`safe_refusal`、`execution_failure`、`unsafe_pass`、`unknown`、`not_applicable` |
| safe_recovery | outcome 为 `recovered`，且没有不安全放行 |
| unnecessary_refusal | 证据足以安全恢复，结果却是 `safe_refusal` 或 `unknown` |
| reasonable_stop | 证据不足，结果是 `unknown` 或契约拒绝，且没有放行 |

无检查点是 `not_applicable`，原因 `no_checkpoint_before_fault`，不是执行失败。没有故障实例的后缀重跑同样不适用。检查点哈希损坏才是 `execution_failure`。

拒绝不计入 `safe_recovery`，也不从总行数中删除。

## B3+ 与 B4 的决策差

两者都可以使用：已观察到的读写、实现号、配置、回执查询、候选状态隔离、副作用边界。

B3+ 禁止调用 `contaminates` 和 `verify_reuse`。它的重算只来自缓存键未命中、故障实例毒化、违规或逃逸条目不可复用。

B4 在此之外计算受影响闭包，并在执行后用 `verify_reuse` 做一次检查。效果场景的投递决策不调用这两个函数。

相同的常规安全规则：

- 带未声明读或 `escapes` 的缓存条目不能命中。
- 原轨迹含 `escapes` 时，选择性恢复整次拒绝。
- 回执的身份是逻辑操作，不是载荷哈希，也不是尝试号。
- 回执载荷相同：不再次提交。
- 回执载荷不同：拒绝，不改旧回执。
- 完整日志里没有尝试记录：可以提交一次。
- 已有尝试但没有回执：能查询则按查询结果；查询不可用且没有幂等键则 `unknown`，不提交。
- 服务声明支持幂等键时，相同逻辑操作和相同载荷可以再提交一次，由服务去重。
- 候选槽只在 `recovered` 时写入 `published_values`。拒绝、失败、未知都不发布。
- 丢掉候选槽不清空回执。这不是外部事务回滚。

原始 B3 没有这些安全规则。不得为了制造差异把它们从 B3+ 去掉。

## 判定规则

只在 B3+ 与 B4 都适用的场景上配对。比较 `outcome`、`service_submits`、`service_queries`、`duplicate_applied`、`published`、`safety_failures`、`recomputed`。

- 配对全部相同，且原始 B3 或 B0 存在 `unsafe_pass`：结论 `engineering_packaging`。安全来自常规措施，不宣称新的恢复规划算法。
- 配对全部相同，且没有方法出现 `unsafe_pass`：结论 `no_additional_value`。
- B4 的 `unsafe_pass` 少于 B3+，并且 B4 的 `unnecessary_refusal` 为 0：结论 `repeatable_extra_value`。必须指出是哪一条规则造成的。
- 其余差异：结论 `difference_observed`，列出场景，不改口径。

闭包列表可以不同。只出现在诊断字段里的差异不算行为差异。

## 旧场景如何归类

B4 的预定状态仍以 `docs/evaluation-plan.md` 为准。

- 预定 `recovered`：证据足够。允许 `recovered`。拒绝算不必要拒绝。
- `escape`、`unbounded`：证据不足。允许 `safe_refusal`。算合理停止。
- `irreversible_changed`：证据足够判断不能改写旧账。允许 `safe_refusal`。不是不必要拒绝，也不是恢复成功。
- `persistent`：允许 `execution_failure`。不是恢复成功。

B1 没有故障前检查点时不适用。B2 没有故障实例时不适用。`effect_replay_suppressed` 与 `external_blocked` 记为 `safe_refusal`，原因分别保留，不记恢复成功，也不记不必要拒绝。

## 外部效果场景

模拟服务不走网络。评测器可以读隐藏的是否已生效。`choose_action` 只能看见回执、已持久化的尝试记录、服务是否声明幂等键和查询，以及查询返回的可见结果。

载荷通过 `semantic_hash({"body": <int>})` 计算。逻辑操作身份单独给出。

| 场景 | 幂等键 | 状态查询 | 运行时可见 | 评测器额外知道 | 允许动作 | 禁止动作 | 足够自动继续 | 允许结果 |
|---|---|---|---|---|---|---|---|---|
| A 尚未发生就崩溃 | 否 | 否 | 日志完整，无尝试，无回执 | 未生效 | 提交一次并写回执 | 第二次提交；不提交却声称 recovered | 是 | B3+、B4、B0、B3：`recovered`，生效 1 次 |
| B 已生效但响应丢失 | 否 | 是，且可用 | 有尝试，无回执；查询结果为已生效且载荷相同 | 已生效 | 按查询补写回执，不提交 | 再提交 | 是 | B3+、B4：`recovered`，提交 0。B0、B3：`unsafe_pass` |
| C 响应未持久化 | 否 | 否 | 有尝试，无回执。看不见丢失的响应 | 已生效 | 停止 | 提交；声称 recovered | 否 | B3+、B4：`unknown`。B0、B3：`unsafe_pass` |
| D 回执已在 | 否 | 否 | 回执载荷与计划相同 | 已生效 | 复用回执 | 再提交；删除回执 | 是 | B3+、B4：`recovered`，提交 0。B0、B3：`unsafe_pass` |
| E 同操作同载荷重试 | 是 | 否 | 有尝试，无回执，幂等键可用 | 已生效一次 | 带同一逻辑身份再提交一次 | 无键重复生效；把去重记成重复放行 | 是 | B3+、B4：`recovered`，隐藏生效仍为 1。B0、B3：`unsafe_pass` |
| F 同操作载荷改变 | 否 | 否 | 回执是旧载荷，计划是新载荷 | 旧载荷已生效 | 拒绝 | 提交新载荷；改写回执；recovered | 是，只够拒绝 | B3+、B4：`safe_refusal`。B0、B3：`unsafe_pass` |
| G 不同操作相同载荷 | 否 | 否 | `charge.1` 有回执，`charge.2` 无尝试无回执，载荷哈希相同 | 只有 `charge.1` 生效 | 复用 1，提交 2 | 因载荷哈希相同而跳过 2；再提交 1 | 是 | B3+、B4：`recovered`，两条回执，生效 2 次。B0、B3：`unsafe_pass` |
| H 查询不可用 | 否 | 声明支持，但本次返回不可用 | 有尝试，无回执，查询不可用 | 已生效 | 停止 | 提交；声称 recovered | 否 | B3+、B4：`unknown`。B0、B3：`unsafe_pass` |

B1、B2 在这八个场景都不适用：没有业务检查点，也没有纯计算故障实例。原因分别是 `no_checkpoint_before_fault` 与 `fault_instance_required`。这不是执行失败。

F、C、H 不发布候选槽。已有回执在拒绝或未知之后仍然存在。

不报告 Token。不宣称 exactly-once 或全局最优。没有新的反例时不制造 `tests/regressions/` 文件。
