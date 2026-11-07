# 公开接口

只记录已经能调用的进程内接口。导入 `rewindagent.api` 不会执行流程。

## 调用

```python
from rewindagent.api import (
    EffectView,
    FaultSpec,
    LocalEffectSimulator,
    Step,
    ToolSpec,
    Workflow,
    execute,
    recover,
    resolve_effect,
)
```

`execute(workflow, tools, initial, fault=None)` 记录一条受控轨迹。`fault` 会注入这条轨迹，得到的是 `recorded`，不是修好的结果。

`recover(executed, profile="safety")` 从同一次进程里的 `execute` 结果恢复。`safety` 是默认路径，不运行历史规划器。`profile="experiment"` 才运行受影响闭包和 `verify_reuse`。没有故障实例时返回 `not_applicable`，原因码 `fault_required`。

`resolve_effect(view, simulator)` 处理一个外部逻辑操作。决策只看 `EffectView` 里的逻辑标识、计划载荷、回执、尝试标志，以及模拟器的 `query`。它不读取模拟器的隐藏生效表。尝试已经开始之后，查询结果为 absent 或 not_found 时返回 `unknown`，不再提交。从未开始的操作仍可以首次提交。研究评测里的 `choose_action` 仍保留 absent 可发送的旧规则，公开入口不走那一条。

`LocalEffectSimulator.stage_hidden` 只给测试安排模拟器内部状态。恢复函数不会调用它，调用方也不应把它当成可见证据。

## 结果

`ExecutionResult` 没有单独的 `success` 字段。

| status | 含义 |
|---|---|
| `recorded` | 初始轨迹已记下。注入故障时，正式值里含有那个故障。 |
| `recovered` | 契约内恢复完成，`published` 为真，`values` 是正式结果。 |
| `safe_refusal` | 已知不能安全继续。不发布。 |
| `unknown` | 可见证据不够，外部结果不能确认。不提交、不发布。 |
| `execution_failure` | 执行失败，例如持续故障或重复生效。不发布。 |
| `not_applicable` | 调用前提不满足，例如没有故障实例。 |
| `unsupported` | 接口不支持这次输入，例如空流程或未知工具。 |

`reason_code` 是稳定原因码，`message` 是说明。`recomputed`、`reused`、`abandoned` 是步骤或操作标识。`evidence` 是这次运行实际持有的可见信息；槽位流程没有查询计数时，该字段为 `"unavailable"`。

`published` 为假时 `values` 是 `None`。`diagnosis["candidate_values"]` 是另一份深拷贝，只供诊断。

`to_dict()` 写出 `schema` 为 `rewindagent.execution_record` 的 JSON。这个文件不能用来在新进程里恢复。

## 只读诊断

```bash
python -m rewindagent inspect record.json
```

命令只解析 JSON 并打印状态、发布标志、重算、复用、废弃、契约和未知操作。缺字段打印 `unavailable`。它不执行文件内容。

## 持久运行

进程内的 `execute` / `recover` 不会在新进程里继续。跨进程继续使用 `rewindagent.durable`：

- `start_run(store, run_id, workflow, initial, tools)` 创建并执行。
- `resume_run(store, run_id, tools)` 只根据 SQLite 和调用方再次提供的工具登记表继续。
- `query_run` 与 `render_store` 只读，不执行工具。

`examples/durable_run.py` 把受信任工具定义在脚本里。任务 JSON 和数据库都不触发模块导入。

只支持有序纯计算。外部效果在执行前返回 `unsupported_effect`。未提交的纯函数可以再执行一次，这不是 exactly-once。正式结果以数据库里的发布事务为准，不以 stdout 为准。

```bash
python -m rewindagent durable status --store task.sqlite --run-id demo
python -m rewindagent durable inspect --store task.sqlite --run-id demo
```

`durable run` 和 `durable resume` 还要求调用方在命令行给出可导入的登记表。脚本入口不把登记表放进数据库。契约和崩溃点见 `docs/phase2a-contract.md`。

混合外部效果不在上面的纯计算入口里。显式使用 `examples/effect_run.py` 和 `python -m rewindagent effect-serve`。命令有 `start`、`resume`、`reconcile`、`status`。`reconcile` 只核对，不提交新业务操作。服务只监听 loopback。详见 `docs/phase2b-contract.md`。
