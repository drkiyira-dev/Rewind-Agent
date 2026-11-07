# 第二阶段 A 契约（实现前写定）

本文件在持久化代码和崩溃测试之前写定。允许的结果是集合。实现不得在看到测试结果后改这些集合，也不得把失败改记成恢复成功。

这不是新的恢复规划算法。默认仍是已验证的常规安全规则。历史 `experiment` 规划器不在本阶段增强。

## 恢复的含义

恢复的是同一个纯计算任务在进程被杀死后继续执行。

不是修正输入、更换工具实现，或自动沿用旧结果。输入、工作流、工具版本或步骤配置变化时，拒绝恢复原任务，不创建迁移后的新任务。

纯函数在返回之后、提交之前被杀死，允许重新执行。这不是失败，也不是 exactly-once。

持续失败的工具在失败被提交后必须是 `execution_failure`。再次 resume 不得调用该工具，也不得变成已发布。

## 范围

支持：有序步骤、确定性纯函数、可规范化的 JSON 值、同一份输入和工作流、单机本地 SQLite、一个存储文件同时只有一个活动执行或恢复进程。

不支持，并在执行前拒绝：外部效果、会逃逸受控接口的工具、未声明读、任意控制流、任意 Python 对象。拒绝不是恢复成功。

不支持网络文件系统、多机锁、租约和 WAL 并发。本阶段不测机器断电。

## 数据

只使用 JSON 里的 null、bool、字符串、有限整数、数组和字符串键对象。浮点数、非有限数、字节串和自定义对象拒绝，原因码 `unsupported_value`。

工作流文档的 `schema_version` 必须是 1。数据库 `user_version` 必须是 1。无法识别时原因码分别是 `unsupported_workflow_schema` 与 `unknown_schema_version`。

摘要使用现有 `semantic_hash`。摘要不符、槽位引用无效、输出摘要与保存的状态对不上，都停止。原因码见下表。

不使用 pickle、eval、闭包序列化、数据库连接或进程内对象身份。

工具登记表由受信任调用方在每个进程里提供。库只保存工具名和 `impl_id`。数据库或任务 JSON 里的字符串不能用来导入模块或执行命令。`impl_id` 不能证明未声明的代码改动都被发现。

## 存储

单个 SQLite 文件是正式状态的唯一真相源。诊断 JSON 只是派生读出，不参与提交。

每个读写连接使用：

- `isolation_level = None`，由代码显式 `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`
- `journal_mode = DELETE`
- `synchronous = FULL`
- `foreign_keys = ON`

选择 DELETE 和 FULL，是为了用回滚日志而不是 WAL 多读者配置。实际 SQLite 版本、平台和 pragma 回读值写进当次结果文件，不在本契约里预填。

`start` 可以创建新库。`resume` 打开已有库；文件不存在、不是数据库、或 schema 不对时，不得新建空库冒充原任务已恢复。

一个步骤的输出、提交后状态、完成标记和完成事件在同一个事务里提交。工具函数执行时不持有该写事务。

最终 `published_json`、`published_digest` 和 `status='published'` 在同一个发布事务里提交。中间步骤已提交不等于任务已发布。stdout 不在这个事务里。

活动执行权使用 POSIX `fcntl.flock` 排他锁，锁在进程退出（含 SIGKILL）后由操作系统释放。不靠 PID 文件判断旧进程是否存活。竞争者返回 `executor_busy`。非 POSIX 平台返回 `unsupported_platform`，本机只测 macOS，Linux 未测。

## 原因码

| 原因码 | 状态 | 是否发布 |
|---|---|---|
| `published` | `published` | 是 |
| `executor_busy` | `busy` | 否 |
| `unsupported_platform` | `unsupported` | 否 |
| `unsupported_effect` | `unsupported` | 否 |
| `unsupported_escape` | `unsupported` | 否 |
| `unsupported_value` | `unsupported` | 否 |
| `unsupported_workflow_schema` | `unsupported` | 否 |
| `empty_workflow` | `unsupported` | 否 |
| `invalid_slot_reference` | `unsupported` | 否 |
| `store_not_found` | `not_applicable` | 否 |
| `store_corrupt` | `execution_failure` | 否 |
| `store_uninitialized` | `execution_failure` | 否 |
| `unknown_schema_version` | `unsupported` | 否 |
| `record_corrupt` | `execution_failure` | 否 |
| `run_not_found` | `not_applicable` | 否 |
| `run_exists` | `not_applicable` | 否 |
| `workflow_mismatch` | `safe_refusal` | 否 |
| `input_mismatch` | `safe_refusal` | 否 |
| `tool_missing` | `safe_refusal` | 否 |
| `tool_version_mismatch` | `safe_refusal` | 否 |
| `persistent_tool_failure` | `execution_failure` | 否 |

查询未完成任务时返回状态 `open`。这不是发布，也不计入恢复成功。

## 崩溃点

标准任务：`left`、`right` 两个独立 `inc`，再 `add` 合并。输入是 `{"left_in": 2, "right_in": 5}`。发布值只允许：

```json
{"left_in": 2, "left_out": 3, "right_in": 5, "right_out": 6, "total": 9}
```

该对象由任务规格直接得到，不由被测恢复器事先生成。

终止点由测试父进程观察子进程自己写出的同步文件后发送 SIGKILL。resume 进程不接收终止点、预期值或同步文件路径。

K2 和 K4 是“事务已经写入了一部分、`COMMIT` 尚未调用”的进程杀死点。不宣称覆盖了 SQLite 内部 COMMIT 的每个瞬间。暂停时若看不到回滚日志，该点不算被覆盖。

| 点 | 杀死时已提交的内容 | 后续进程允许的结果 |
|---|---|---|
| K0 | 任务登记已提交，没有步骤 | 发布上述值。三个工具都在后续进程执行 |
| K1 | 第一步函数已返回，提交事务还没开始 | 第一步可以再执行。最终仍发布上述值 |
| K2 | 同 K1，且暂停时能看到回滚日志；杀死后该事务不可见 | 同 K1。不得留下只有输出或只有事件的半截步骤 |
| K3 | 第一步已提交，下一步还没执行 | 第一步不重算。后两步执行。最终发布上述值 |
| K4 | 三个步骤都已提交，发布事务已写入一部分但未 COMMIT | 不再调用工具。后续进程发布一次。杀死前不得已是 published |
| K5 | 发布事务已 COMMIT，进程还没打印成功 | 不再调用工具。发布身份不变。stdout 可以是空的 |

另外：

- 恢复进程在下一个 K3 再被杀死后，第三个进程继续，并只补尚未提交的步骤。
- 已发布任务再次 resume：返回同一次发布，工具调用为零，published 事件仍是一条。
- 锁被占用时，另一进程得到 `executor_busy`，并且不调用工具。
- 缺工具、版本不同、输入摘要不同、工作流摘要不同：对应拒绝，不沿用旧结果冒充新任务。
- 未知 `user_version`、损坏文件、步骤摘要被改过：停止，不新建空库。
- 总是抛错的工具：提交 `persistent_tool_failure` 后不再执行。

这些重复运行只检查可复现，不是独立业务样本，也不构成生产可靠性置信度。
