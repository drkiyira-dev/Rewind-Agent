# RewindAgent 0.1.0

[English](README.md) | 简体中文

实验性的 Python 恢复组件，研究结论保持 `engineering_packaging`。第三阶段 A 只增加一个受限的 Responses 工具循环，不是新的 Agent 框架。后续阶段没有开始。

四层能力分别是：

- 进程内接口：`execute`、`recover`、`resolve_effect`，恢复记录只活在当前进程。
- 纯计算持久恢复：单个本机 SQLite 工作库，进程被 SIGKILL 后由新进程继续已提交的纯计算步骤。
- 受限 loopback 效果恢复：工作库与模拟追加服务库分开，只监听 `127.0.0.1`，依据回执与查询继续、拒绝或保持 `unknown`。
- Responses 工具循环：一次只接受一个非流式 function call。模型响应先落库，然后才执行工具。已验证的是本地协议夹具，不是真实模型。契约见 `docs/phase3a-contract.md`。

任务结果看 `status` 和 `published`；退出码 0 只表示命令正常返回。`unknown` 不等于成功，也不能推出外部效果未发生。未发布的候选槽不是正式结果。

## 恢复边界维护

回执必须属于当前逻辑操作，即使载荷相同也不能跨操作复用。SQLite 文件名中的 `#`、`?` 和 `%` 按原文件名处理。追加客户端只从 HTTP 200 采纳确认；HTTP 409 保留提交冲突语义，畸形响应和 HTTP 错误保持不可用。幂等重试在网络等待后和实际发送前重新检查时间；达到保留期终点或时钟早于准备时间时保持 `unknown`。显式 `now_ms` 仅用于确定性测试，不是生产时钟。

这些修复不改变公开 API 或存储格式，原研究解释器和冻结证据保持不变。当前测试另覆盖上述边界，不以历史 82 项的数量代替新验证。

## 范围与研究结论

本轮只验证 macOS 26.6 arm64、Python 3.14.3、SQLite 3.53.4、本机磁盘和单存储文件单活动执行者。核心包只用标准库。可选 extra `responses` 安装官方 OpenAI SDK，已核对的版本是 3.46.0。Linux、Windows、其他 Python 版本、断电、分布式执行、真实模型和真实外部服务均未验证。`py3-none-any` 是 wheel 标签，不是跨平台验收结论。

在既有 42 个受控离线场景中，B4 与补齐常规检查的 B3+ 在预先指定的配对行为字段上未观察到差异。安全性来自共享检查，不来自恢复规划器。默认 `recover(..., profile="safety")` 使用常规安全路径；`profile="experiment"` 保留历史规划器。没有新的恢复规划算法、全局最优恢复证明、生产级 exactly-once、真实模型 Token 节省或任意第三方 API 自动恢复保证。`actual_model_calls = 0`。

工具必须来自受信任登记表，并遵守纯函数和受控读写约定；这不是 Python 沙箱，未声明的磁盘、网络、环境变量访问不会被自动发现。故障位置由调用方指定。公开工作流是有序步骤；历史解释器的受控循环上界为 4。

纯函数在返回后、提交前被杀死，可以再次执行。持久存储使用 `journal_mode=DELETE`、`synchronous=FULL`、显式事务及 `fcntl.flock`。输入、工作流或工具身份不一致时拒绝沿用旧任务。

效果恢复只适用于模拟服务契约 `append.v1`。跨 run 不去重；新 `run_id` 是新调用方范围。客户端按持久化的准备时间和保留期限停止不安全重试；服务端 TTL 清理未实现。曾尝试发送后，查询 `not_found` 不授权再 POST；旧请求仍可能完成，之后可 `reconcile`。P2 是非幂等、可查询配置，同身份同载荷直接 POST 两次会生效两次。效果行中的身份键可供查询，不能单凭有键的行数称其为“去重行”。

API、持久化与服务契约分别见 [docs/api.md](docs/api.md)、[docs/phase2a-contract.md](docs/phase2a-contract.md)、[docs/phase2b-contract.md](docs/phase2b-contract.md)。原研究定义见 [docs/evaluation-plan.md](docs/evaluation-plan.md) 和 [docs/round2-expectations.md](docs/round2-expectations.md)。

## 测试与离线实验

在仓库根目录运行：

```bash
python3 -W always::ResourceWarning -m unittest discover -s tests -v
python3 -m rewindagent demo
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/rewind-demo.XXXXXX")"
python3 -m rewindagent eval --out "$run_dir/eval.json"
python3 -m rewindagent round2 --out "$run_dir/round2.json"
```

当前完整套件为 148 项；安装 Responses 可选依赖后全部通过、无跳过。不安装可选依赖时，明确跳过 23 项 SDK 集成测试，其余核心和边界测试仍运行。原交付套件为 82 项。测试需要允许本机 loopback 监听。所有测试产物默认写入独立临时目录；需要保留时设置 `REWIND_TEST_OUTPUT` 指向输出父目录，每次仍新建子目录。旧 `PHASE2A_WRITE` / `PHASE2B_WRITE` 回写开关已移除。

七份冻结原始证据由所有者决定保留本机，不在公开仓库和安装包中。有原始材料的本地工作区可另外运行：

```bash
python3 -m unittest tests.history_audit -v
```

该模块包含 3 项历史审计，缺原始材料会报错；它不属于普通功能测试，也没有用合成数据冒充历史证据。当前 eval 相对最早冻结结果除了计时，还有新增 `planner_used` / `published` 字段及非 B4 的 `closure` 诊断差异。B4 的 closure 与预先指定的语义字段仍由历史审计逐项检查。不能说与最早结果仅有计时差异。

`eval` / `round2` 拒绝覆盖源码树的三个冻结研究输出路径。新实验输出应使用自己的目录。

## 构建与本地安装

以下命令接着使用上面创建的 `run_dir`，始终在仓库根目录、同一终端执行。构建工具安装可能访问 PyPI；安装已有 wheel 则明确禁用包索引和依赖解析。

```bash
python3 -m venv "$run_dir/build-env"
"$run_dir/build-env/bin/python" -m pip install \
  build==1.6.1 setuptools==84.2.1 packaging==26.3 pyproject_hooks==1.3.3
"$run_dir/build-env/bin/python" -m build --no-isolation --outdir "$run_dir/dist" .
python3 -m venv "$run_dir/install-env"
"$run_dir/install-env/bin/python" -m pip install \
  --no-index --no-deps --disable-pip-version-check \
  "$run_dir/dist/rewindagent-0.1.0-py3-none-any.whl"
py="$run_dir/install-env/bin/python"
(cd "$run_dir" && env -u PYTHONPATH "$py" -c 'import rewindagent; print(rewindagent.__file__)')
(cd "$run_dir" && env -u PYTHONPATH "$py" -m rewindagent demo)
```

本轮构建使用上述版本，本机构建工具安装日志显示使用缓存；构建使用 `--no-isolation` 和已安装工具。没有验证无缓存新机器离线构建。新版 setuptools 的最低声明改为 77，以支持 SPDX 许可证元数据。核心运行时没有第三方依赖；Responses 循环使用下文说明的可选依赖。

wheel 只包含包及标准元数据；sdist 另包含测试、示例与文档。数据库、日志、原始结果、虚拟环境与本地构建副本均不分发。不要设置指向原源码的 `PYTHONPATH` 来冒充独立安装。

## 进程内与纯计算示例

继续使用上面的已安装解释器和临时目录，示例与输入路径均相对仓库根目录：

```bash
"$py" examples/branch_recover.py \
  --snapshot examples/data/snapshot.json --out "$run_dir/example" --mode compute
"$py" examples/branch_recover.py --out "$run_dir/example" --mode unknown
"$py" -m rewindagent inspect "$run_dir/example/unknown.json"
"$py" examples/durable_run.py start \
  --store "$run_dir/task.sqlite" --run-id demo \
  --spec examples/data/durable_workflow.json --snapshot examples/data/snapshot.json
"$py" examples/durable_run.py resume --store "$run_dir/task.sqlite" --run-id demo
"$py" -m rewindagent durable inspect --store "$run_dir/task.sqlite" --run-id demo
```

进程内 compute 示例重算 `left`、`join`，复用 `right`，发布 `total=9`；unknown 示例提交 0 次且不发布。持久纯计算示例发布 `total=9`，重复 resume 不再次调用工具。`inspect` 只解析记录，不执行记录中的文本。

## Loopback 效果示例

仍在仓库根目录、同一终端执行。服务在后台启动，下面只停止本终端创建的那个 PID：

```bash
"$py" -m rewindagent effect-serve \
  --store "$run_dir/append.sqlite" --profile P0 \
  --ready "$run_dir/append-ready.json" --retention-ms 60000 \
  >"$run_dir/service.stdout" 2>"$run_dir/service.stderr" &
service_pid=$!
trap 'kill "$service_pid" 2>/dev/null; wait "$service_pid" 2>/dev/null' EXIT
service_url="$("$py" - "$run_dir/append-ready.json" <<'PY'
import json, pathlib, sys, time
ready = pathlib.Path(sys.argv[1])
for _ in range(100):
    if ready.exists():
        print(json.loads(ready.read_text())["base_url"])
        break
    time.sleep(0.1)
else:
    raise SystemExit("service did not become ready")
PY
)"
"$py" examples/effect_run.py start \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url" \
  --spec examples/data/effect_workflow.json --snapshot examples/data/effect_snapshot.json
"$py" examples/effect_run.py resume \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url"
"$py" examples/effect_run.py reconcile \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url"
"$py" examples/effect_run.py status --store "$run_dir/effect-run.sqlite" --run-id demo
kill "$service_pid"
wait "$service_pid" 2>/dev/null || true
trap - EXIT
```

P0/P1 支持幂等，P0/P2 支持查询。正常示例发布 `n2=3`、`report=4`。`reconcile` 只查询并采纳证据，不发新的业务 POST。

检查完临时结果后，仅删除这次创建的目录（会同时删除示例虚拟环境和本地包）：

```bash
rm -rf -- "$run_dir"
unset run_dir py service_pid service_url
```

## 本地 Responses 工具循环

现有 3A 每轮只接受一个普通 function call，先持久化响应再执行工具，保存输出后才请求下一轮。`unknown` 会停止循环；`reconcile` 只核对已有效果，不调用模型、不新增业务 POST。真实模式仍返回 `live_not_authorized`。本地 fixture 是明确标注的合成数据，不是真实模型记录。

恢复在执行或采纳子任务前检查已保存的参数与子任务身份；不一致时返回 `record_corrupt`，不改写证据。崩溃留下需要写库回滚的 SQLite 日志时，`status` 和 `inspect` 返回 `unknown / store_recovery_required`，调用方应使用 `resume` 在执行锁保护下恢复。新增 SIGKILL 测试覆盖尚未提交的请求意图、已接受调用、效果输出、最终响应和发布，包括日志回滚与锁竞争。详见[边界覆盖与限制](docs/phase3a-contract.md#additional-recovery-boundaries)。

在仓库根目录运行下列示例。安装本地源码的可选依赖可能访问 PyPI，但运行流程只访问 loopback，不需要真实 API 密钥：

```bash
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/rewind-agent.XXXXXX")"
python3 -m venv "$run_dir/env"
py="$run_dir/env/bin/python"
"$py" -m pip install '.[responses]'
"$py" -m rewindagent effect-serve \
  --store "$run_dir/effect.sqlite" --profile P0 \
  --ready "$run_dir/effect-ready.json" --retention-ms 60000 &
effect_pid=$!
"$py" -m rewindagent protocol-serve \
  --fixture examples/data/agent_fixture.json \
  --store "$run_dir/protocol.sqlite" \
  --ready "$run_dir/protocol-ready.json" &
protocol_pid=$!
cleanup_3a() {
  kill "$effect_pid" "$protocol_pid" 2>/dev/null || true
  wait "$effect_pid" "$protocol_pid" 2>/dev/null || true
}
trap cleanup_3a EXIT
"$py" - "$run_dir/effect-ready.json" "$run_dir/protocol-ready.json" <<'PY_READY'
import json, pathlib, sys, time
paths = [pathlib.Path(name) for name in sys.argv[1:]]
for _ in range(200):
    try:
        [json.loads(path.read_text()) for path in paths]
        break
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.05)
else:
    raise SystemExit("local services did not become ready within 10 seconds")
PY_READY
effect_url="$( "$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"])' "$run_dir/effect-ready.json" )"
protocol_url="$( "$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"])' "$run_dir/protocol-ready.json" )"
"$py" examples/responses_agent.py start \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --snapshot examples/data/agent_snapshot.json \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
"$py" examples/responses_agent.py status --store "$run_dir/agent.sqlite" --run-id demo
"$py" examples/responses_agent.py resume \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
"$py" examples/responses_agent.py reconcile \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
cleanup_3a
trap - EXIT
```

正常流程发布 `summary.count=2`、`summary.total=7`，追加效果一次。可选依赖固定为 `openai==3.46.0`、`httpx2==2.16.0`；不依赖项目已发布到 PyPI。预算只在 `start` 时设定，恢复保留原预算并拒绝覆盖参数。

Agent 协调锁在子任务交接期间持续持有；已有子任务结果必须匹配工作流、输入和工具身份才能采纳。新响应重复 call ID 会被拒绝；不同 call ID 可以复用同一授权槽的相同效果。纯工具只读取导入快照，不接受模型参数。不透明 reasoning 字段用于本地续接，`inspect` 隐藏其内容。`store=False` 不是提供方完全不保留数据的保证。

检查完本次结果后可用 `rm -rf -- "$run_dir"` 清理仅由此示例创建的临时目录。没有调用真实模型、测量真实 Token、发布包或部署服务。

## 交付与许可证

采用 [GNU GPL 第 3 版](LICENSE)，SPDX 标识 `GPL-3.0-only`；第三方阅读记录保留在 [NOTICE.md](NOTICE.md)。本轮不发布 PyPI 包、GitHub Release 或部署服务。

本轮接管与验证范围见 [docs/CODEX_HANDOFF.md](docs/CODEX_HANDOFF.md)，仓库候选文件及留存材料分类见 `artifacts/handoff-manifest.json`。没有 CI 配置或 CI 通过徽章。未接 MCP、邮件、支付或生产数据库。第三阶段 A 是上面的受限 Responses 循环；真实模型验证没有执行。
