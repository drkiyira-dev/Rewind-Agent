"""Public in-process API. It does not import the evaluation suite or its answers.

The default profile runs the conventional safety checks that were measured
under the historical name B3+. Callers do not pass that label. The experiment
profile keeps the historical planner for reproduction and does not claim a
measured benefit over the default.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .canonical import semantic_hash
from .effects import SimulatedService, choose_action
from .engine import Fault, Run, record, rerun
from .model import Tool

LIMITS: tuple[str, ...] = (
    "工具必须是解释器传入参数的纯函数。这不是沙箱，看不到的文件、网络、环境变量和全局状态不会被自动发现。",
    "公开流程是有序步骤列表。任意 Python 控制流不在这个接口里。",
    "故障实例由调用方给出。这里不会自动归因。",
    "外部效果使用进程内模拟服务。这不是跨进程 exactly-once。",
    "循环超过 4 次超出解释器支持范围。",
    "这是脚本化工具流程，不是 LLM Agent。恢复记录只存在于当前进程。",
)

_PROFILES = {"safety": "B3+", "experiment": "B4"}
_FAULT_MODES = {"removed", "persistent"}
_REFUSAL_STATUSES = {
    "effect_replay_suppressed",
    "external_blocked",
    "verifier_rejected",
    "write_conflict",
}
_SCHEMA = "rewindagent.execution_record"
_SCHEMA_VERSION = 1


def _message(code: str) -> str:
    return {
        "recorded": "受控执行已记入轨迹。若注入了故障，故障留在这条轨迹里，这不是修复后的结果。",
        "recovered": "在支持的契约内完成恢复，并发布了正式结果。",
        "fault_required": "选择性恢复需要调用方给出的故障实例。未开始恢复。",
        "empty_workflow": "流程没有步骤。未开始执行。",
        "duplicate_step": "步骤标识重复。未开始执行。",
        "unknown_tool": "步骤引用了未登记的工具。未开始执行。",
        "duplicate_tool": "工具名重复。未开始执行。",
        "empty_tools": "没有登记工具。未开始执行。",
        "unsupported_fault_mode": "故障模式只支持 removed 或 persistent。",
        "unsupported_profile": "profile 只支持 safety 或 experiment。",
        "recording_required": "恢复需要同一次进程里 execute 返回的对象。读取诊断文件不会重新执行。",
        "query_required": "声明了状态查询，但没有提供查询函数。未提交外部效果。",
        "refused_unobservable_dependency": "工具可能绕过受控状态接口，选择性恢复停止。",
        "refused_unbounded_control": "控制流超出支持的循环上界，停止。",
        "refused_external_payload_changed": "同一逻辑操作的载荷与已有回执不一致，拒绝继续，不改旧回执。",
        "refused_new_external_effect": "恢复会新增一条外部投递，拒绝继续。",
        "effect_replay_suppressed": "外部账本抑制了一次重复投递。这不是新的恢复成功发布。",
        "external_blocked": "账本拒绝了第二份载荷，旧记录仍在。",
        "verifier_rejected": "实验规划器的复用检查未通过，未发布正式结果。",
        "write_conflict": "写入基线与当前槽不一致，停止。",
        "persistent_fault": "按已知故障重算一次后仍得到记录中的坏输出，停止。",
        "checkpoint_corrupt": "检查点内容无法通过校验。",
        "unsafe_pass": "执行触到了不安全的外部效果或冲突，未把它当成恢复成功。",
        "effect_unknown": "可见证据不足以确认外部效果。未再次提交，也未发布正式结果。",
        "effect_reused": "可见回执与计划载荷一致，未再次提交。",
        "effect_sent": "按可见记录提交了一次，并写下回执。",
        "effect_deduped": "同一逻辑操作带幂等键再次提交，服务去重后仍是一次生效。",
        "effect_confirmed_by_query": "可见查询说明该载荷已经生效，只补写本地回执，未再次提交。",
        "duplicate_effect": "提交被模拟服务记成重复生效，未发布正式结果。",
        "conflicting_submit": "提交与已生效载荷冲突，未改写已有结果，也未发布正式结果。",
        "tool_not_callable": "工具实现不是可调用对象。",
        "effect_identity": "逻辑操作标识为空。",
    }.get(code, "执行停止。没有更具体的已记录原因。")


@dataclass(frozen=True)
class ToolSpec:
    """A controlled tool. The function sees only the values the interpreter passes."""

    name: str
    fn: Callable[[list[Any]], tuple[Any, ...]]
    impl_id: str = ""
    escapes: bool = False
    undeclared_slot: str | None = None


@dataclass(frozen=True)
class Step:
    step_id: str
    tool: str
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    config: dict[str, Any] | None = None


@dataclass(frozen=True)
class Workflow:
    steps: tuple[Step, ...]


@dataclass(frozen=True)
class FaultSpec:
    step_id: str
    mode: str = "removed"


@dataclass(frozen=True)
class EffectView:
    """What the caller can show the decision: identity, payload, receipt, and attempt flag."""

    logical_id: str
    planned_body: Any
    attempt_started: bool = False
    receipt: Mapping[str, str] | None = None
    idempotent_keys: bool = False
    status_query: bool = False


@dataclass
class ExecutionResult:
    status: str
    reason_code: str
    message: str
    published: bool
    values: dict[str, Any] | None
    recomputed: list[str]
    reused: list[str]
    abandoned: list[str]
    operation_ids: list[str]
    warnings: list[str]
    limits: list[str]
    evidence: dict[str, Any]
    diagnosis: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": _SCHEMA,
            "schema_version": _SCHEMA_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "published": self.published,
            "values": copy.deepcopy(self.values),
            "recomputed": list(self.recomputed),
            "reused": list(self.reused),
            "abandoned": list(self.abandoned),
            "operation_ids": list(self.operation_ids),
            "warnings": list(self.warnings),
            "limits": list(self.limits),
            "evidence": copy.deepcopy(self.evidence),
            "diagnosis": copy.deepcopy(self.diagnosis),
        }


class LocalEffectSimulator:
    """In-process stand-in. Decision code may call query and submit, not hidden apply state."""

    def __init__(
        self,
        *,
        idempotent_keys: bool,
        status_query: bool,
        query_available: bool = True,
    ) -> None:
        self.idempotent_keys = idempotent_keys
        self.status_query = status_query
        self.query_available = query_available
        self._service = SimulatedService(
            idempotent_keys=idempotent_keys,
            status_query=status_query,
            query_available=query_available,
        )
        self._receipts: list[dict[str, str]] = []

    def stage_hidden(self, logical_id: str, body: Any) -> None:
        """Put an effect into the simulator. Callers must not use this as a decision input."""
        self._service.seed(logical_id, semantic_hash({"body": body}))

    def query(self, logical_id: str) -> dict[str, Any]:
        return self._service.query(logical_id)

    def receipt(self, logical_id: str) -> dict[str, str] | None:
        for entry in self._receipts:
            if entry["logical_id"] == logical_id:
                return dict(entry)
        return None

    def save_receipt(self, logical_id: str, digest: str, attempt_id: str) -> None:
        if self.receipt(logical_id) is not None:
            return
        self._receipts.append(
            {"logical_id": logical_id, "payload_hash": digest, "attempt_id": attempt_id}
        )

    def submit(self, logical_id: str, digest: str, *, use_key: bool) -> str:
        key = logical_id if use_key else None
        return self._service.submit(
            logical_id,
            digest,
            idempotency_key=key,
            attempt_id=f"{logical_id}#recovery",
        )

    def visible_log(self) -> dict[str, Any]:
        return {
            "submits": copy.deepcopy(self._service.submits),
            "queries": self._service.queries,
            "receipts": copy.deepcopy(self._receipts),
        }


def _blank(
    status: str,
    code: str,
    *,
    warnings: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
    operation_ids: list[str] | None = None,
) -> ExecutionResult:
    return ExecutionResult(
        status=status,
        reason_code=code,
        message=_message(code),
        published=False,
        values=None,
        recomputed=[],
        reused=[],
        abandoned=[],
        operation_ids=list(operation_ids or []),
        warnings=list(warnings or []),
        limits=list(LIMITS),
        evidence={} if evidence is None else evidence,
        diagnosis={"candidate_values": None, "engine_reason": "unavailable"},
    )


def _tool_map(specs: Sequence[ToolSpec]) -> tuple[dict[str, Tool] | None, ExecutionResult | None]:
    if not specs:
        return None, _blank("unsupported", "empty_tools")
    tools: dict[str, Tool] = {}
    for spec in specs:
        if spec.name in tools:
            return None, _blank("unsupported", "duplicate_tool", operation_ids=[spec.name])
        if not callable(spec.fn):
            return None, _blank("unsupported", "tool_not_callable", operation_ids=[spec.name])
        tools[spec.name] = Tool(
            spec.name,
            spec.impl_id or f"{spec.name}.v1",
            spec.fn,
            escapes=spec.escapes,
            undeclared_slot=spec.undeclared_slot,
        )
    return tools, None


def _check_workflow(workflow: Workflow, tools: dict[str, Tool]) -> ExecutionResult | None:
    if not workflow.steps:
        return _blank("unsupported", "empty_workflow")
    seen: set[str] = set()
    for step in workflow.steps:
        if step.step_id in seen:
            return _blank("unsupported", "duplicate_step", operation_ids=[step.step_id])
        seen.add(step.step_id)
        if step.tool not in tools:
            return _blank("unsupported", "unknown_tool", operation_ids=[step.step_id])
    return None


def _bind(workflow: Workflow):
    def run(engine: Any) -> None:
        for step in workflow.steps:
            config = None if step.config is None else dict(step.config)
            engine.call(step.step_id, step.tool, list(step.reads), list(step.writes), config=config)

    return run


def _warnings(tools: dict[str, Tool], profile: str | None) -> list[str]:
    notes = []
    if any(tool.escapes for tool in tools.values()):
        notes.append("有工具标记了 escapes。选择性恢复会拒绝，而不是追踪工具离开接口后的副作用。")
    if profile == "experiment":
        notes.append(
            "experiment 会运行历史受影响闭包和 verify_reuse。"
            "在已记录的 42 个离线场景里，它相对 safety 没有改变配对结果。"
        )
    return notes


def _classify_run(run: Run) -> tuple[str, str]:
    if run.safety_failures or run.duplicate_applied > 0:
        return "execution_failure", "unsafe_pass"
    status = run.status
    if status == "recorded":
        return "recorded", "recorded"
    if status == "recovered":
        return "recovered", "recovered"
    if status == "persistent_fault":
        return "execution_failure", "persistent_fault"
    if status == "checkpoint_corrupt":
        return "execution_failure", "checkpoint_corrupt"
    if status.startswith("refused_") or status in _REFUSAL_STATUSES:
        return "safe_refusal", status
    if status in {"no_checkpoint", "not_applicable"}:
        return "not_applicable", status
    return "execution_failure", status or "unclassified"


def _result_from_run(
    run: Run,
    *,
    profile: str,
    warnings: list[str],
    evidence: dict[str, Any],
) -> ExecutionResult:
    status, code = _classify_run(run)
    publish = status in {"recorded", "recovered"}
    values = copy.deepcopy(run.published_values) if publish else None
    candidate = copy.deepcopy(run.candidate_values)
    engine_reason = "unavailable"
    for item in run.reasons:
        if item.get("decision") in {"refuse", "stop"} and item.get("why"):
            engine_reason = item["why"]
            break
    if not publish:
        values = None
    return ExecutionResult(
        status=status,
        reason_code=code,
        message=_message(code),
        published=publish,
        values=values,
        recomputed=list(run.recomputed),
        reused=list(run.reused),
        abandoned=list(run.abandoned),
        operation_ids=list(run.order),
        warnings=warnings,
        limits=list(LIMITS),
        evidence=evidence,
        diagnosis={
            "candidate_values": candidate,
            "engine_status": run.status,
            "engine_reason": engine_reason,
            "reasons": copy.deepcopy(run.reasons),
            "planner_used": run.planner_used,
            "profile": profile,
            "safety_failures": list(run.safety_failures),
        },
    )


class _Memory:
    def __init__(
        self,
        run: Run,
        workflow: Workflow,
        tools: dict[str, Tool],
        initial: dict[str, Any],
    ) -> None:
        self.run = run
        self.workflow = workflow
        self.tools = tools
        self.initial = initial


def execute(
    workflow: Workflow,
    tools: Sequence[ToolSpec],
    initial: Mapping[str, Any],
    *,
    fault: FaultSpec | None = None,
) -> ExecutionResult:
    """Record one controlled trace. A fault here is injected into that trace."""
    tool_map, rejected = _tool_map(tools)
    if rejected is not None:
        return rejected
    assert tool_map is not None
    rejected = _check_workflow(workflow, tool_map)
    if rejected is not None:
        return rejected
    if fault is not None and fault.mode not in _FAULT_MODES:
        return _blank("unsupported", "unsupported_fault_mode", operation_ids=[fault.step_id])
    frozen_steps = tuple(
        Step(
            step.step_id,
            step.tool,
            tuple(step.reads),
            tuple(step.writes),
            None if step.config is None else copy.deepcopy(step.config),
        )
        for step in workflow.steps
    )
    frozen = Workflow(steps=frozen_steps)
    initial_copy = copy.deepcopy(dict(initial))
    engine_fault = None if fault is None else Fault(fault.step_id, fault.mode)
    try:
        recorded = record(_bind(frozen), initial_copy, engine_fault, tool_map)
    except Exception as exc:
        return _blank(
            "execution_failure",
            "unclassified",
            evidence={"exception": type(exc).__name__},
        )
    evidence = {
        "fault_instance": "unavailable" if fault is None else fault.step_id,
        "fault_mode": "unavailable" if fault is None else fault.mode,
        "profile": "execute",
        "input_slots": sorted(initial_copy),
        "service_queries": "unavailable",
        "service_submits": recorded.external_attempts,
    }
    result = _result_from_run(
        recorded,
        profile="execute",
        warnings=_warnings(tool_map, None),
        evidence=evidence,
    )
    result._recording = _Memory(recorded, frozen, tool_map, initial_copy)  # type: ignore[attr-defined]
    return result


def recover(executed: ExecutionResult, *, profile: str = "safety") -> ExecutionResult:
    """Recover from an in-process execute() result. Default profile is the safety path."""
    if profile not in _PROFILES:
        return _blank("unsupported", "unsupported_profile")
    memory = getattr(executed, "_recording", None)
    if not isinstance(memory, _Memory):
        return _blank("unsupported", "recording_required")
    if memory.run.fault is None:
        return _blank("not_applicable", "fault_required")
    warnings = _warnings(memory.tools, profile)
    try:
        recovered = rerun(
            _PROFILES[profile],
            _bind(memory.workflow),
            memory.run,
            memory.initial,
            tools=memory.tools,
        )
    except Exception as exc:
        return _blank(
            "execution_failure",
            "unclassified",
            warnings=warnings,
            evidence={"exception": type(exc).__name__, "profile": profile},
        )
    evidence = {
        "fault_instance": memory.run.fault.instance_id,
        "fault_mode": memory.run.fault.mode,
        "profile": profile,
        "planner_used": recovered.planner_used,
        "input_slots": sorted(memory.initial),
        "service_queries": "unavailable",
        "service_submits": recovered.external_attempts,
    }
    return _result_from_run(recovered, profile=profile, warnings=warnings, evidence=evidence)


def resolve_effect(view: EffectView, simulator: LocalEffectSimulator) -> ExecutionResult:
    """Decide one external operation from the visible receipt, attempt, and query."""
    if not view.logical_id:
        return _blank("unsupported", "effect_identity")
    if view.status_query and not simulator.status_query:
        return _blank(
            "unsupported",
            "query_required",
            operation_ids=[view.logical_id],
            evidence={"logical_id": view.logical_id},
        )
    try:
        digest = semantic_hash({"body": view.planned_body})
    except TypeError:
        return _blank(
            "execution_failure",
            "unclassified",
            operation_ids=[view.logical_id],
            evidence={"logical_id": view.logical_id, "exception": "TypeError"},
        )
    receipt = None if view.receipt is None else dict(view.receipt)
    if receipt is None:
        receipt = simulator.receipt(view.logical_id)
    captured: dict[str, Any] = {"value": "unavailable"}

    def watched(logical_id: str) -> dict[str, Any]:
        captured["value"] = simulator.query(logical_id)
        return captured["value"]

    action = choose_action(
        view.logical_id,
        digest,
        receipt,
        view.attempt_started,
        idempotent_keys=view.idempotent_keys and simulator.idempotent_keys,
        status_query=view.status_query,
        query=watched,
    )
    query_result = captured["value"]
    absent = isinstance(query_result, dict) and query_result.get("status") in {"absent", "not_found"}
    if action["action"] == "send" and view.attempt_started and absent:
        action = {
            "action": "unknown",
            "reason": "a not-found query after an attempt does not authorize another submit",
        }
    kind = action["action"]
    submit_result = "unavailable"
    published = False
    status = "execution_failure"
    code = "unclassified"
    reused: list[str] = []
    recomputed: list[str] = []
    if kind == "reuse":
        status, code, published = "recovered", "effect_reused", True
        reused = [view.logical_id]
    elif kind == "refuse_payload":
        status, code = "safe_refusal", "refused_external_payload_changed"
    elif kind == "unknown":
        status, code = "unknown", "effect_unknown"
    elif kind == "persist_from_query":
        simulator.save_receipt(view.logical_id, action["payload_hash"], f"{view.logical_id}#query")
        status, code, published = "recovered", "effect_confirmed_by_query", True
        reused = [view.logical_id]
    elif kind == "send":
        submit_result = simulator.submit(view.logical_id, digest, use_key=bool(action.get("use_key")))
        if submit_result in {"applied", "deduped"}:
            simulator.save_receipt(view.logical_id, digest, f"{view.logical_id}#recovery")
            status = "recovered"
            code = "effect_deduped" if submit_result == "deduped" else "effect_sent"
            published = True
            recomputed = [view.logical_id]
        elif submit_result == "duplicate":
            status, code = "execution_failure", "duplicate_effect"
        else:
            status, code = "execution_failure", "conflicting_submit"
    evidence = {
        "logical_id": view.logical_id,
        "planned_payload_hash": digest,
        "receipt": copy.deepcopy(receipt),
        "attempt_started": view.attempt_started,
        "idempotent_keys": bool(view.idempotent_keys and simulator.idempotent_keys),
        "status_query": view.status_query,
        "query_result": copy.deepcopy(query_result) if query_result != "unavailable" else "unavailable",
        "action": kind,
        "action_reason": action.get("reason", "unavailable"),
        "submit_result": submit_result,
        "service_submits": 0 if submit_result == "unavailable" else 1,
        "service_queries": simulator.visible_log()["queries"],
        "profile": "safety",
    }
    values = None
    candidate = {"logical_id": view.logical_id, "planned_body": copy.deepcopy(view.planned_body)}
    if published:
        stored = receipt if kind == "reuse" else simulator.receipt(view.logical_id)
        values = {"receipt": copy.deepcopy(stored)}
    return ExecutionResult(
        status=status,
        reason_code=code,
        message=_message(code),
        published=published,
        values=values,
        recomputed=recomputed,
        reused=reused,
        abandoned=[],
        operation_ids=[view.logical_id],
        warnings=_warnings({}, "safety"),
        limits=list(LIMITS),
        evidence=evidence,
        diagnosis={
            "candidate_values": candidate,
            "engine_status": "unavailable",
            "engine_reason": action.get("reason", "unavailable"),
            "planner_used": False,
            "profile": "safety",
        },
    )
