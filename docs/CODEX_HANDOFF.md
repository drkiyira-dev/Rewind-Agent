# Codex 接管说明

本说明对应本轮有限收尾。研究结论保持 `engineering_packaging`，版本保持 0.1.0，第三阶段未开始。它不是后续扩建、发布包、部署或更改仓库设置的授权。

## 本轮事实与来源

接管时原清单 71 项的大小及 SHA-256 全部匹配，没有接管前漂移。前清单 SHA-256：`765116ff0a26fa5984455f4595e2d17f1c3b3fdebfa7233eb7aa9028228cda26`。原清单及旧交接说明另存本机新结果目录的 `prior/`，原 `results/finalization/20261107-handoff/` 的 16 份文件保持原字节。

本轮证据父目录：`results/codex-handoff/20261107T224828-JST/`，仅保留本机。当前文件摘要、上传分类和验证摘要以 `artifacts/handoff-manifest.json` 为准；清单不计算自己的摘要。最终 commit SHA 在提交后的外部回执报告，避免自引用。

实际验证平台为 macOS 26.6 arm64、Python 3.14.3、SQLite 3.53.4、本机磁盘。其他平台、版本、断电和真实业务服务未验证。运行时未调用模型。原安装记录和旧 wheel/sdist 已查阅，但不用于证明本轮修改后的安装内容。

## 资源清理

基线完整套件 80 项：沙箱内有 14 项因 loopback bind 权限失败；允许 loopback 后 79 通过、1 失败，并出现 78 条 ResourceWarning。两次基线原日志均保留。

主要根因在 `tests/test_effect_crash.py`：服务 finally 只发 SIGKILL，没有 wait/communicate；部分工作进程只 wait 而未处理 PIPE；启动、超时、断言和 before_kill 回调异常时没有完整所有权。对象被替换或销毁时仍可能没有已记录的 returncode，管道也未关闭。`tests/test_finalization.py` 的部分异常路径也缺兜底所有权。

三个进程测试模块现在在 Popen 创建后立即登记 unittest cleanup。`tests/process_support.py` 的清理先保留 SIGKILL，再 drain/communicate 和回收；无管道时 wait。正常、超时、启动失败和中途断言失败均有明确清理。没有过滤警告、按进程名杀进程或使用 pkill。

基线另有一个时序竞争：query outage 测试先释放响应再杀客户端，客户端可能已经发布。修复将 SIGKILL 放在已提交但响应仍暂停的既定 E4 窗口，再释放响应。安全断言没有放宽，故障窗口的定义没有改变。

逐个检查子进程 stderr 又发现损坏 SQLite 文件的警告。`rewindagent/durable.py::_open` 在连接成功、配置/完整性检查抛错时未关闭连接。本轮只补异常关闭，不改持久化格式、公开接口或恢复决策。

当前完整可移植套件 82 项，通过 82，失败/错误/跳过/预期失败均为 0。`logs/final-unittest.txt` 与 `final-tests/test-summary.json` 记录 149 个子进程，returncode 均明确、管道均关闭、每个 PID 的 waitpid 检查均为已回收。主日志和捕获的子进程 stderr 没有 ResourceWarning。新增 4 项资源异常路径测试；3 项历史证据审计移出自动发现；新增 1 项 P2 直接重复 POST 测试。原安全案例保留。

## P2 与 J/K

P2 配置为 `idempotent=False, query=True`。服务表 `effects` 的主键是 effect_id；仅 P0/P1 创建身份键唯一索引，且只有幂等配置在 POST 中检查已有键。P2 用身份键查询效果记录，不会因此去重。

原验收字段 `dedup_rows` 实际执行 `COUNT(*) FROM effects WHERE idempotency_key IS NOT NULL`。不是单独的去重表；新报告称 `keyed_effect_rows`。旧证据不修改。

独立新服务库、直接 HTTP 连续提交两次同身份同载荷：POST 2、效果 2、带身份键的效果行 2、不同 effect_id、无 effects_key 索引。查询返回已有匹配记录。独立 P2 inflight 案例仍证明：当前查不到时不再次 POST，旧请求完成后 reconcile 发布，POST 始终为 1。

本轮 J：提交前杀服务、同库重启，效果 0、POST 1、恢复 unknown 不发布。K：提交后响应前杀服务，同库重启核对，效果 1、POST 1、发布。P2：最终效果 1、POST 1、发布。详细配置、PID、回执和评分保留在 `final-tests/rewind-tests-*/acceptance.json`。

## 测试、研究与交付分离

普通功能测试：`python3 -W always::ResourceWarning -m unittest discover -s tests -v`。无需七份原始研究结果。测试输出使用独立临时目录；设置 `REWIND_TEST_OUTPUT` 只指定父目录，每次仍新建目录。已移除 PHASE2A_WRITE/PHASE2B_WRITE 历史回写路径，并移除 finalization 测试对旧 acceptance.json 的直接覆盖。

本地原始证据审计：`python3 -m unittest tests.history_audit -v`，3 项全部通过。保留原比较断言，哈希范围扩为全部七份冻结文件。缺材料时报错，不静默跳过；公开仓库没有这些原始文件，因此不能声称其完成了原始证据审计。

demo、eval、round2 均本轮独立执行，退出码 0。round2 仍为 `engineering_packaging`。与前次 finalization eval 相比只有计时差异；与最早 eval 比较还有新增 planner_used/published 及 B0/B1/B2/B3 的 closure 诊断差异（92 处）。B4 closure 与既定语义字段通过历史审计。round2 对冻结结果的差异为计时与源码摘要。没有扩大忽略名单抹去 closure。

从实际候选文件另建干净目录，构建 wheel/sdist、安全解包 sdist 再构建、新环境本地 wheel 无索引无依赖安装及七个安装流程的命令/返回码/摘要，记录在本轮清单的 verification 与本机证据中。包内容扫描检查成员名、成员内容、路径穿越、符号/硬链接、私密路径和凭据模式。扫描是有限检查，不保证零秘密。

构建工具：build 1.6.1、setuptools 84.2.1、packaging 26.3、pyproject_hooks 1.3.3，独立虚拟环境。工具安装允许官方 PyPI，日志显示从本机缓存获取。构建采用 --no-isolation 和已有工具；不声称无缓存新机器离线构建成功。源码和 sdist 的产物都重新生成，旧包只作为旧版本证据。

## 七份冻结证据

所有者决定本轮全部留在本机，不上传。原清单中的六项 decision_required 已据此转为 local_only；第七项原本就是 local_only，数量从来不矛盾。

| 路径 | 本轮分类 | 理由 |
| --- | --- | --- |
| results/eval.json | local_only | 原始合成研究记录；所有者选择不上传 |
| results/eval-rerun.json | local_only | 原始复跑；所有者选择不上传 |
| results/round2.json | local_only | 原始第二轮实验；所有者选择不上传 |
| results/phase2a/pure-crash/crash-results.json | local_only | 含原工作区绝对路径；保持原字节 |
| results/phase2a/pure-crash/installed-k3.json | local_only | 原安装记录；所有者选择不上传 |
| results/phase2b/effect-crash/crash-results.json | local_only | 原效果崩溃记录；所有者选择不上传 |
| results/phase2b/effect-crash/installed.json | local_only | 原安装记录；所有者选择不上传 |

完整 SHA-256、大小、用途和理由在清单。旧 finalization、新结果、包和完整日志均为 local_only。build、egg-info、字节码和系统文件为 exclude，留在本机不等于不存在。没有生成脱敏实验副本。

## Git 与所有者决定

接管时目录及父目录均无 Git 仓库，不存在原索引、原 HEAD、分支、远程或待推送历史。系统 Git 仍因 Xcode 许可返回 69；本轮使用已安装的 Xcode 内 Git 2.54.0 的明确路径，没有接受许可或更改系统设置。

所有者本轮确认新建公开 Rewind Agent 仓库、main、允许初始化、GPL-3.0、仅上传审查通过的源码/测试/示例/文档/清单。名称规范化为 `drkiyira-dev/Rewind-Agent`；账号登录名和 ID 经连接及 CLI 核实。提交使用已核实登录名及其 GitHub noreply 地址，不使用私人邮箱、不修改全局身份。许可证采用 `GPL-3.0-only`，GNU 官方原文在 LICENSE；NOTICE 的第三方记录保留。setuptools 最低要求提升至 77 仅用于 SPDX 元数据支持。

本轮只按清单白名单暂存，检查 staged 内容、配置/hooks/过滤器及实际待推送历史。精确提交、远程可见性、分支 SHA 和 CI 的最终事实由提交后回执给出，不在本文件预填 SHA。没有 CI 配置；不能把本地成功写成远程 CI 成功。

当前授权不包括 force push、改写历史、改可见性、删分支或仓库、自动合并、发布包/Release、部署、调用真实业务服务或进入第三阶段。
