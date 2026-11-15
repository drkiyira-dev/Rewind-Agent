"""One Responses tool loop on top of the existing durable kernel.

A model response is committed before any tool runs. Resume does not ask the
model to replace an accepted call. unknown blocks the next model turn.
"""

from __future__ import annotations

import copy
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .adapters.openai_responses import (
    SUPPORTED_OUTPUT_TYPES,
    ResponsesConfig,
    config_problem,
    create_response,
    endpoint_class,
    parse_arguments,
    public_tool_schema,
)
from .agent_store import (
    SessionLock,
    append_item,
    canon,
    connect,
    ensure_schema,
    items,
    load_session,
    protocol_json,
    provider_public,
    redact,
    registry_public,
    schema_state,
)
from .api import ToolSpec
from .canonical import semantic_hash
from .durable import prepare_store, query_run, resume_run, start_run

_RUN_ID = __import__("re").compile(r"[A-Za-z0-9._-]{1,60}")
STOPS = (
    "response_uncommitted",
    "call_accepted",
    "child_done",
    "output_committed",
    "final_saved",
    "published",
)
INSTRUCTIONS_LIMIT = 2000


def _message(code: str) -> str:
    return {
        "published": "Agent 会话已发布。发布身份在数据库里。",
        "model_request_unknown": "模型请求可能已经发出，但响应没有持久接受。没有自动重试，也没有执行工具。",
        "protocol_error": "模型响应无法按本轮协议子集解释。没有执行工具。",
        "unsupported_output": "响应含有本轮不支持的输出项。没有丢弃后继续。",
        "multiple_function_calls": "一个响应里有多个 function call。本轮不执行其中任何一个。",
        "unknown_tool": "工具不在受信任登记表里。",
        "invalid_arguments": "工具参数不是允许的 JSON。",
        "model_incomplete": "模型响应未完成。没有把它当成任务成功。",
        "model_failed": "模型响应失败。没有执行工具。",
        "model_refusal": "模型拒绝了请求。没有发布成功。",
        "unsupported_status": "模型响应状态不在支持范围内。",
        "business_incomplete": "最终文本不能代替已确认的记录效果。",
        "effect_unknown": "效果结果未知。Agent 不再请求下一轮模型。",
        "effect_conflict": "同一授权效果槽的载荷冲突。没有创建第二个效果。",
        "slot_not_ready": "记录效果需要本会话已经生成的汇总。",
        "budget_exhausted": "模型或工具预算已用完。计数跨重启保留。",
        "session_mismatch": "模型、工具、快照或预算与已保存会话不一致。",
        "executor_busy": "这个会话已有活动进程。",
        "live_not_authorized": "真实模型调用没有在本轮执行。",
        "dependency_missing": "可选 Responses 适配器没有安装。",
        "endpoint_not_loopback": "本地协议模式只接受显式 loopback 地址。",
        "unsupported_provider_mode": "provider 模式不受支持。",
        "local_mode_rejects_live_credentials": "本地模式不接受真实凭据开关。",
        "model_required": "模型名称必须由调用方给出。",
        "timeout_out_of_range": "请求超时超出本轮范围。",
        "run_exists": "会话已经存在。继续请使用 resume。",
        "session_not_found": "没有这个 Agent 会话。",
        "reconcile_idle": "没有已存在的不确定效果需要核对。",
        "schema_incomplete": "Agent 表不完整，没有补建空表。",
        "unknown_format": "Agent 存储格式无法识别。",
        "open": "会话尚未发布。",
        "duplicate_call_id": "新响应重复了已经接受的 call_id，未执行工具。",
        "record_corrupt": "会话记录未通过一致性检查，没有继续执行。",
    }.get(code, "Agent 会话停止。")


@dataclass(frozen=True)
class AgentTool:
    """Trusted tool. The model cannot change its kind, service, or implementation."""

    name: str
    description: str
    kind: str
    impl_id: str
    parameters: dict[str, Any]
    fn: Callable[..., tuple[Any, ...]] | None = None
    service_id: str | None = None


@dataclass(frozen=True)
class AgentBudgets:
    max_model_turns: int = 8
    max_tool_calls: int = 6
    max_model_attempts: int = 8
    max_output_chars: int = 4000

    def as_dict(self) -> dict[str, int]:
        return {
            "max_model_turns": self.max_model_turns,
            "max_tool_calls": self.max_tool_calls,
            "max_model_attempts": self.max_model_attempts,
            "max_output_chars": self.max_output_chars,
        }


@dataclass
class AgentResult:
    status: str
    reason_code: str
    message: str
    published: bool
    phase: str
    run_id: str
    values: dict[str, Any] | None = None
    publication_id: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "published": self.published,
            "phase": self.phase,
            "run_id": self.run_id,
            "values": copy.deepcopy(self.values),
            "publication_id": self.publication_id,
            "evidence": copy.deepcopy(self.evidence),
            "warnings": list(self.warnings),
        }


class _Boundary:
    def __init__(self, stop_at: str | None, ready: Path | None, timeout_s: float) -> None:
        self.stop_at = stop_at
        self.ready = ready
        self.timeout_s = timeout_s

    def reach(self, point: str) -> None:
        if self.stop_at != point:
            return
        if self.ready is not None:
            payload = json.dumps({"pid": os.getpid(), "point": point})
            temporary = self.ready.with_suffix(self.ready.suffix + ".tmp")
            temporary.write_text(payload, encoding="utf-8")
            fd = os.open(temporary, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            temporary.replace(self.ready)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            time.sleep(0.05)
        raise SystemExit(86)


def start_agent(
    store: Path,
    run_id: str,
    task: str,
    snapshot: Mapping[str, Any],
    tools: Sequence[AgentTool],
    provider: ResponsesConfig,
    *,
    instructions: str,
    effect_slot: str,
    services: Mapping[str, Any] | None = None,
    budgets: AgentBudgets | None = None,
    stop_at: str | None = None,
    ready_path: Path | None = None,
    boundary_timeout_s: float = 60,
) -> AgentResult:
    budgets = budgets or AgentBudgets()
    problem = _precheck(run_id, task, snapshot, tools, provider, instructions, effect_slot, budgets, stop_at)
    if problem:
        return _result(problem[0], problem[1], run_id)
    boundary = _Boundary(stop_at, ready_path, boundary_timeout_s)
    lock = SessionLock(store)
    busy = lock.acquire()
    if busy:
        return _result("busy" if busy == "executor_busy" else "unsupported", busy, run_id)
    conn = None
    try:
        prepared = prepare_store(store)
        if prepared:
            status = "unsupported" if prepared == "unknown_schema_version" else "execution_failure"
            return _result(status, prepared, run_id)
        conn = connect(store, write=True)
        state = ensure_schema(conn)
        if state != "ok":
            code = "schema_incomplete" if state == "incomplete" else "unknown_format"
            status = "execution_failure" if state == "incomplete" else "unsupported"
            return _result(status, code, run_id)
        if load_session(conn, run_id) is not None:
            return _result("not_applicable", "run_exists", run_id)
        registry = registry_public(list(tools))
        provider_row = provider_public(provider)
        snap = copy.deepcopy(dict(snapshot))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO agent_sessions (
                run_id, status, phase, task, instructions, snapshot_json, snapshot_digest,
                registry_json, registry_digest, provider_json, provider_digest, effect_slot,
                effect_tool, budgets_json, model_attempts, model_turns, tool_calls, reason_code
            ) VALUES (?, 'open', 'awaiting_model', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, 'open')
            """,
            (
                run_id,
                task,
                instructions,
                canon(snap),
                semantic_hash(snap),
                canon(registry),
                semantic_hash(registry),
                canon(provider_row),
                semantic_hash(provider_row),
                effect_slot,
                _effect_tool_name(list(tools)),
                canon(budgets.as_dict()),
            ),
        )
        append_item(conn, run_id, "user", {"role": "user", "content": task})
        conn.execute("COMMIT")
        holder = [conn]
        conn = None
        try:
            return _advance(
                store, holder, lock, run_id, list(tools), services or {}, provider, boundary, authorize_retry=False
            )
        finally:
            holder[0].close()
    except sqlite3_error():
        return _result("execution_failure", "store_corrupt", run_id)
    finally:
        if conn is not None:
            conn.close()
        lock.release()


def resume_agent(
    store: Path,
    run_id: str,
    tools: Sequence[AgentTool],
    provider: ResponsesConfig,
    *,
    services: Mapping[str, Any] | None = None,
    snapshot: Mapping[str, Any] | None = None,
    authorize_model_retry: bool = False,
    stop_at: str | None = None,
    ready_path: Path | None = None,
    boundary_timeout_s: float = 60,
) -> AgentResult:
    if stop_at is not None and stop_at not in STOPS:
        return _result("unsupported", "unsupported_stop_point", run_id)
    boundary = _Boundary(stop_at, ready_path, boundary_timeout_s)
    return _open_and_advance(
        store,
        run_id,
        list(tools),
        provider,
        services or {},
        snapshot,
        boundary,
        authorize_retry=authorize_model_retry,
        reconcile_only=False,
    )


def reconcile_agent(
    store: Path,
    run_id: str,
    tools: Sequence[AgentTool],
    provider: ResponsesConfig,
    *,
    services: Mapping[str, Any] | None = None,
) -> AgentResult:
    """Adopt an existing uncertain effect. No model request and no new business POST."""
    boundary = _Boundary(None, None, 1)
    return _open_and_advance(
        store,
        run_id,
        list(tools),
        provider,
        services or {},
        None,
        boundary,
        authorize_retry=False,
        reconcile_only=True,
    )


def query_agent(store: Path, run_id: str) -> AgentResult:
    if not store.exists():
        return _result("not_applicable", "session_not_found", run_id)
    try:
        conn = connect(store, write=False)
    except sqlite3_error():
        return _result("execution_failure", "store_corrupt", run_id)
    try:
        state = schema_state(conn)
        if state == "absent":
            return _result("not_applicable", "session_not_found", run_id)
        if state != "ok":
            code = "schema_incomplete" if state == "incomplete" else "unknown_format"
            return _result("execution_failure" if state == "incomplete" else "unsupported", code, run_id)
        row = load_session(conn, run_id)
        if row is None:
            return _result("not_applicable", "session_not_found", run_id)
        problem = _record_problem(row)
        if problem:
            return _result("execution_failure", problem, run_id)
        return _from_row(conn, row)
    finally:
        conn.close()


def inspect_agent(store: Path, run_id: str) -> dict[str, Any]:
    result = query_agent(store, run_id)
    body = result.to_dict()
    if result.reason_code in {"record_corrupt", "store_corrupt", "schema_incomplete", "unknown_format"}:
        return body
    if not store.exists():
        return body
    try:
        conn = connect(store, write=False)
    except sqlite3_error():
        return body
    try:
        if schema_state(conn) != "ok" or load_session(conn, run_id) is None:
            return body
        body["items"] = [redact(item) for item in items(conn, run_id)]
        calls = conn.execute(
            "SELECT call_id, name, state, child_run_id, adopted FROM agent_calls WHERE run_id=? ORDER BY call_id",
            (run_id,),
        ).fetchall()
        body["calls"] = [
            {
                "call_id": row["call_id"],
                "name": row["name"],
                "state": row["state"],
                "child_run_id": row["child_run_id"],
                "adopted": bool(row["adopted"]),
            }
            for row in calls
        ]
        return body
    finally:
        conn.close()


def sqlite3_error() -> type[BaseException]:
    import sqlite3

    return sqlite3.Error


def _open_and_advance(
    store: Path,
    run_id: str,
    tools: list[AgentTool],
    provider: ResponsesConfig,
    services: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    boundary: _Boundary,
    *,
    authorize_retry: bool,
    reconcile_only: bool,
) -> AgentResult:
    if not _RUN_ID.fullmatch(run_id):
        return _result("unsupported", "model_required", run_id)
    checked = _tool_problem(tools)
    if checked:
        return _result("unsupported", checked, run_id)
    provider_problem = config_problem(provider)
    if provider_problem and provider_problem != "live_not_authorized":
        status = "unsupported" if provider_problem != "live_not_authorized" else "safe_refusal"
        return _result(status, provider_problem, run_id)
    if provider.mode == "live":
        return _result("safe_refusal", "live_not_authorized", run_id)
    lock = SessionLock(store)
    busy = lock.acquire()
    if busy:
        return _result("busy" if busy == "executor_busy" else "unsupported", busy, run_id)
    conn = None
    try:
        if not store.exists():
            return _result("not_applicable", "session_not_found", run_id)
        conn = connect(store, write=True)
        state = schema_state(conn)
        if state != "ok":
            code = "schema_incomplete" if state == "incomplete" else "unknown_format"
            return _result("execution_failure" if state == "incomplete" else "unsupported", code, run_id)
        row = load_session(conn, run_id)
        if row is None:
            return _result("not_applicable", "session_not_found", run_id)
        problem = _record_problem(row)
        if problem:
            return _result("execution_failure", problem, run_id)
        mismatch = _identity(row, tools, provider, snapshot)
        if mismatch:
            return _result("safe_refusal", mismatch, run_id, phase=row["phase"])
        holder = [conn]
        conn = None
        try:
            return _advance(
                store,
                holder,
                lock,
                run_id,
                tools,
                services,
                provider,
                boundary,
                authorize_retry=authorize_retry,
                reconcile_only=reconcile_only,
            )
        finally:
            holder[0].close()
    except sqlite3_error():
        return _result("execution_failure", "store_corrupt", run_id)
    finally:
        if conn is not None:
            conn.close()
        lock.release()


def _advance(
    store: Path,
    holder: list,
    lock: SessionLock,
    run_id: str,
    tools: list[AgentTool],
    services: Mapping[str, Any],
    provider: ResponsesConfig,
    boundary: _Boundary,
    *,
    authorize_retry: bool,
    reconcile_only: bool = False,
) -> AgentResult:
    warnings: list[str] = []
    # At most 20 accepted turns: one request and one adoption/publication
    # transition per turn, plus a terminal budget check.
    for _ in range(42):
        conn = holder[0]
        row = load_session(conn, run_id)
        assert row is not None
        if row["status"] != "open":
            return _from_row(conn, row, warnings)
        phase = row["phase"]
        if reconcile_only:
            return _reconcile(store, holder, lock, run_id, tools, services, row)
        if phase == "may_have_sent" and not authorize_retry:
            return _result(
                "unknown",
                "model_request_unknown",
                run_id,
                phase=phase,
                evidence=_evidence(row, provider),
                warnings=warnings,
            )
        if phase == "may_have_sent" and authorize_retry:
            warnings.append("新的模型请求尝试可能重复计费，并且不保证与丢失的响应相同。")
            authorize_retry = False
            sent = _send_model(conn, row, tools, provider, boundary)
            if sent is not None:
                sent.warnings.extend(warnings)
                return sent
            continue
        if phase in {"tool_unknown", "tool_pending"}:
            adopted = _settle_call(
                store,
                holder,
                lock,
                run_id,
                tools,
                services,
                boundary,
                unknown_only=phase == "tool_unknown",
            )
            if adopted is not None:
                adopted.warnings.extend(warnings)
                return adopted
            continue
        if phase == "final_saved":
            return _publish_if_ready(store, conn, run_id, boundary)
        if phase == "awaiting_model":
            sent = _send_model(conn, load_session(conn, run_id), tools, provider, boundary)
            if sent is not None:
                sent.warnings.extend(warnings)
                return sent
            continue
        return _from_row(conn, row, warnings)
    return _result("execution_failure", "protocol_error", run_id)


def _send_model(conn, row, tools: list[AgentTool], provider: ResponsesConfig, boundary: _Boundary):
    budgets = json.loads(row["budgets_json"])
    if int(row["model_attempts"]) >= int(budgets["max_model_attempts"]):
        return _stop(conn, row["run_id"], "safe_refusal", "budget_exhausted")
    if int(row["model_turns"]) >= int(budgets["max_model_turns"]):
        return _stop(conn, row["run_id"], "safe_refusal", "budget_exhausted")
    attempt = int(row["model_attempts"]) + 1
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE agent_sessions SET phase='may_have_sent', model_attempts=? WHERE run_id=?",
        (attempt, row["run_id"]),
    )
    conn.execute(
        "INSERT INTO agent_attempts (run_id, attempt_seq, state, response_json) VALUES (?, ?, 'may_have_sent', NULL)",
        (row["run_id"], attempt),
    )
    conn.execute("COMMIT")
    created = create_response(
        provider,
        instructions=row["instructions"],
        input_items=items(conn, row["run_id"]),
        tools=[public_tool_schema(tool.name, tool.description, tool.parameters) for tool in tools],
    )
    if not created.get("ok"):
        if created.get("error") == "dependency_missing":
            return _stop(conn, row["run_id"], "unsupported", "dependency_missing")
        fresh = load_session(conn, row["run_id"])
        return _result(
            "unknown",
            "model_request_unknown",
            row["run_id"],
            phase="may_have_sent",
            evidence=_evidence(fresh, provider),
        )
    response = created.get("response")
    if not isinstance(response, dict):
        return _stop(conn, row["run_id"], "unsupported", "protocol_error")
    boundary.reach("response_uncommitted")
    classified = _classify(response)
    text_size = len(json.dumps(response, ensure_ascii=False))
    if text_size > int(budgets["max_output_chars"]) * 8:
        return _persist_bad(conn, row["run_id"], attempt, response, "unsupported", "protocol_error")
    if classified["kind"] == "bad":
        status = _bad_status(classified["reason"])
        return _persist_bad(conn, row["run_id"], attempt, response, status, classified["reason"])
    if classified["kind"] == "call":
        return _accept_call(conn, row, attempt, response, classified["call"], tools, boundary)
    return _accept_final(conn, row, attempt, response, boundary)


def _accept_call(conn, row, attempt: int, response: dict[str, Any], call: dict[str, Any], tools, boundary: _Boundary):
    budgets = json.loads(row["budgets_json"])
    name = call.get("name")
    call_id = call.get("call_id")
    arguments = call.get("arguments")
    if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(arguments, str):
        return _persist_bad(conn, row["run_id"], attempt, response, "unsupported", "protocol_error")
    tool = next((item for item in tools if item.name == name), None)
    if tool is None:
        return _persist_bad(conn, row["run_id"], attempt, response, "unsupported", "unknown_tool")
    parsed = parse_arguments(arguments, tool.parameters)
    if isinstance(parsed, str):
        return _persist_bad(conn, row["run_id"], attempt, response, "unsupported", "invalid_arguments")
    if int(row["tool_calls"]) >= int(budgets["max_tool_calls"]):
        return _persist_bad(conn, row["run_id"], attempt, response, "safe_refusal", "budget_exhausted")
    digest = semantic_hash(parsed)
    existing = conn.execute(
        "SELECT * FROM agent_calls WHERE run_id=? AND call_id=?",
        (row["run_id"], call_id),
    ).fetchone()
    if existing is not None:
        return _persist_bad(conn, row["run_id"], attempt, response, "safe_refusal", "duplicate_call_id")
    child_id, conflict = _child_for(conn, row, tool, parsed, int(row["tool_calls"]) + 1)
    if conflict:
        return _persist_bad(conn, row["run_id"], attempt, response, "safe_refusal", conflict)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE agent_attempts SET state='accepted', response_json=? WHERE run_id=? AND attempt_seq=?",
        (protocol_json(response), row["run_id"], attempt),
    )
    for item in response.get("output", []):
        append_item(conn, row["run_id"], "model_output", item)
    if existing is None:
        conn.execute(
            """
            INSERT INTO agent_calls (
                run_id, call_id, name, arguments_json, arguments_digest, child_run_id, state, adopted
            ) VALUES (?, ?, ?, ?, ?, ?, 'pending', 0)
            """,
            (row["run_id"], call_id, name, canon(parsed), digest, child_id),
        )
    turns = int(row["model_turns"]) + 1
    calls = int(row["tool_calls"]) + 1
    effect_child = row["effect_child_id"]
    if tool.kind == "effect":
        effect_child = child_id
    conn.execute(
        """
        UPDATE agent_sessions
        SET phase='tool_pending', model_turns=?, tool_calls=?, effect_child_id=?, reason_code='open'
        WHERE run_id=?
        """,
        (turns, calls, effect_child, row["run_id"]),
    )
    conn.execute("COMMIT")
    boundary.reach("call_accepted")
    return None


def _accept_final(conn, row, attempt: int, response: dict[str, Any], boundary: _Boundary):
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE agent_attempts SET state='accepted', response_json=? WHERE run_id=? AND attempt_seq=?",
        (protocol_json(response), row["run_id"], attempt),
    )
    for item in response.get("output", []):
        append_item(conn, row["run_id"], "model_output", item)
    conn.execute(
        "UPDATE agent_sessions SET phase='final_saved', model_turns=?, reason_code='open' WHERE run_id=?",
        (int(row["model_turns"]) + 1, row["run_id"]),
    )
    conn.execute("COMMIT")
    boundary.reach("final_saved")
    return None


def _persist_bad(conn, run_id: str, attempt: int, response: dict[str, Any], status: str, reason: str) -> AgentResult:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE agent_attempts SET state='accepted', response_json=? WHERE run_id=? AND attempt_seq=?",
        (protocol_json(response), run_id, attempt),
    )
    for item in response.get("output", []) if isinstance(response.get("output"), list) else []:
        if isinstance(item, dict):
            append_item(conn, run_id, "model_output", item)
    conn.execute(
        "UPDATE agent_sessions SET status=?, phase='stopped', model_turns=model_turns+1, reason_code=? WHERE run_id=?",
        (status, reason, run_id),
    )
    conn.execute("COMMIT")
    fresh = load_session(conn, run_id)
    return _from_row(conn, fresh)


def _child_for(conn, row, tool: AgentTool, arguments: dict[str, Any], seq: int) -> tuple[str, str | None]:
    if tool.kind == "pure":
        return f"{row['run_id']}.p{seq}", None
    if tool.name != row["effect_tool"]:
        return "", "unknown_tool"
    if not row["summary_ref"]:
        return "", "slot_not_ready"
    if arguments.get("summary_ref") != row["summary_ref"]:
        return "", "effect_conflict"
    child = row["effect_child_id"] or f"{row['run_id']}.effect"
    return child, None


def _settle_call(store, holder, lock, run_id, tools, services, boundary, *, unknown_only: bool):
    conn = holder[0]
    call = conn.execute(
        """
        SELECT * FROM agent_calls
        WHERE run_id=? AND adopted=0
        ORDER BY rowid DESC LIMIT 1
        """,
        (run_id,),
    ).fetchone()
    if call is None:
        return _stop(conn, run_id, "execution_failure", "protocol_error")
    if semantic_hash(json.loads(call["arguments_json"])) != call["arguments_digest"]:
        return _stop(conn, run_id, "execution_failure", "record_corrupt")
    tool = next(item for item in tools if item.name == call["name"])
    row = load_session(conn, run_id)
    conn.close()
    lock.release_executor()
    try:
        outcome = _execute_child(store, row, call, tool, services, allow_start=not unknown_only)
    finally:
        busy = lock.acquire_executor()
    if busy:
        return _result("busy", "executor_busy", run_id, phase="tool_pending")
    reopened = connect(store, write=True)
    holder[0] = reopened
    if outcome["kind"] == "busy":
        return _result("busy", "executor_busy", run_id, phase="tool_pending")
    boundary.reach("child_done")
    if outcome["kind"] == "unknown":
        reopened.execute("BEGIN IMMEDIATE")
        reopened.execute(
            "UPDATE agent_sessions SET phase='tool_unknown', reason_code='effect_unknown' WHERE run_id=?",
            (run_id,),
        )
        reopened.execute(
            "UPDATE agent_calls SET state='unknown' WHERE run_id=? AND call_id=?",
            (run_id, call["call_id"]),
        )
        reopened.execute("COMMIT")
        fresh = load_session(reopened, run_id)
        return _result(
            "unknown",
            "effect_unknown",
            run_id,
            phase="tool_unknown",
            evidence=_evidence(fresh, None),
        )
    if outcome["kind"] != "done":
        return _stop(reopened, run_id, outcome["status"], outcome["reason"])
    _adopt(reopened, run_id, call, tool, outcome["values"])
    boundary.reach("output_committed")
    return None


def _execute_child(store, row, call, tool: AgentTool, services, *, allow_start=True, reconcile_only=False) -> dict[str, Any]:
    workflow, initial = _child_definition(row, tool)
    existing = query_run(store, call["child_run_id"])
    if existing.reason_code == "run_not_found":
        if not allow_start:
            return {"kind": "unknown"}
        result = start_run(
            store, call["child_run_id"], workflow, initial, _child_tools(tool),
            services=_child_services(tool, services),
        )
    else:
        # Even a published child must match this accepted call's inputs, tool
        # identity and workflow before its values can be adopted.
        result = resume_run(
            store, call["child_run_id"], _child_tools(tool),
            services=_child_services(tool, services), workflow=workflow,
            initial=initial, reconcile_only=reconcile_only,
        )
    return _map_durable(result, tool, row)


def _child_definition(row, tool: AgentTool):
    if tool.kind == "pure":
        return ({
            "schema_version": 1,
            "steps": [{"step_id": "call", "tool": tool.name,
                       "reads": ["bound"], "writes": ["result"], "config": {}}],
        }, {"bound": json.loads(row["snapshot_json"])})
    return ({
        "schema_version": 1,
        "steps": [{"step_id": "record", "kind": "effect",
                   "logical_id": row["effect_slot"], "service_id": tool.service_id,
                   "reads": ["summary"], "writes": ["ack"], "config": {}}],
    }, {"summary": json.loads(row["summary_json"])})


def _map_durable(result, tool: AgentTool, row) -> dict[str, Any]:
    if result.published:
        return {"kind": "done", "values": result.values or {}}
    if result.status == "unknown":
        return {"kind": "unknown"}
    if result.reason_code == "executor_busy":
        return {"kind": "busy"}
    return {"kind": "stop", "status": result.status, "reason": result.reason_code}


def _adopt(conn, run_id: str, call, tool: AgentTool, values: dict[str, Any]) -> None:
    output = {"tool": tool.name, "values": values}
    conn.execute("BEGIN IMMEDIATE")
    if tool.name == "summarize" and isinstance(values.get("result"), dict):
        summary = values["result"]
        conn.execute(
            "UPDATE agent_sessions SET summary_json=?, summary_ref=? WHERE run_id=?",
            (canon(summary), semantic_hash(summary), run_id),
        )
    conn.execute(
        "UPDATE agent_calls SET state='committed', output_json=?, adopted=1 WHERE run_id=? AND call_id=?",
        (canon(output), run_id, call["call_id"]),
    )
    append_item(
        conn,
        run_id,
        "function_call_output",
        {
            "type": "function_call_output",
            "call_id": call["call_id"],
            "output": json.dumps(redact(output), ensure_ascii=False, sort_keys=True),
        },
    )
    conn.execute(
        "UPDATE agent_sessions SET phase='awaiting_model', reason_code='open' WHERE run_id=?",
        (run_id,),
    )
    conn.execute("COMMIT")


def _publish_if_ready(store, conn, run_id: str, boundary: _Boundary):
    row = load_session(conn, run_id)
    if not row["summary_json"] or not row["effect_child_id"]:
        return _stop(conn, run_id, "safe_refusal", "business_incomplete")
    child = query_run(store, row["effect_child_id"])
    if not child.published:
        if child.status == "unknown":
            conn.execute(
                "UPDATE agent_sessions SET phase='tool_unknown', reason_code='effect_unknown' WHERE run_id=?",
                (run_id,),
            )
            return _result("unknown", "effect_unknown", run_id, phase="tool_unknown", evidence=_evidence(load_session(conn, run_id), None))
        return _stop(conn, run_id, "safe_refusal", "business_incomplete")
    summary = json.loads(row["summary_json"])
    ack = (child.values or {}).get("ack")
    if ((child.values or {}).get("summary") != summary
            or ack != semantic_hash({"values": [summary]})):
        return _stop(conn, run_id, "safe_refusal", "session_mismatch")
    text = _assistant_text(conn, run_id)
    body = {
        "summary": summary,
        "summary_ref": row["summary_ref"],
        "ack": ack,
        "final_text": text,
        "effect_slot": row["effect_slot"],
    }
    publication = semantic_hash(body)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE agent_sessions
        SET status='published', phase='published', reason_code='published',
            publication_id=?, published_json=?
        WHERE run_id=?
        """,
        (publication, canon(body), run_id),
    )
    conn.execute("COMMIT")
    boundary.reach("published")
    return _from_row(conn, load_session(conn, run_id))


def _reconcile(store, holder, lock, run_id, tools, services, row) -> AgentResult:
    conn = holder[0]
    call = conn.execute(
        "SELECT * FROM agent_calls WHERE run_id=? AND name=? ORDER BY rowid DESC LIMIT 1",
        (run_id, row["effect_tool"]),
    ).fetchone()
    if call is None:
        return _result("not_applicable", "reconcile_idle", run_id, phase=row["phase"], evidence=_evidence(row, None))
    tool = next(item for item in tools if item.name == call["name"])
    conn.close()
    lock.release_executor()
    try:
        outcome = _execute_child(store, row, call, tool, services, allow_start=False, reconcile_only=True)
    finally:
        busy = lock.acquire_executor()
    if busy:
        return _result("busy", "executor_busy", run_id, phase=row["phase"])
    conn = connect(store, write=True)
    holder[0] = conn
    if outcome["kind"] == "busy":
        return _result("busy", "executor_busy", run_id, phase=row["phase"])
    if outcome["kind"] == "stop":
        return _stop(conn, run_id, outcome["status"], outcome["reason"])
    if outcome["kind"] != "done":
        return _result("unknown", "effect_unknown", run_id, phase=row["phase"],
                       evidence=_evidence(load_session(conn, run_id), None))
    if not call["adopted"]:
        _adopt(conn, run_id, call, tool, outcome["values"])
    return _from_row(conn, load_session(conn, run_id))


def _assistant_text(conn, run_id: str) -> str:
    parts: list[str] = []
    for item in items(conn, run_id):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                parts.append(str(content.get("text", "")))
    return "\n".join(parts)


def _child_tools(tool: AgentTool) -> list[ToolSpec]:
    if tool.kind != "pure" or tool.fn is None:
        return []
    return [ToolSpec(tool.name, tool.fn, impl_id=tool.impl_id)]


def _child_services(tool: AgentTool, services: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if tool.kind != "effect":
        return None
    return services


def _record_problem(row) -> str | None:
    """Check stored bindings before using them; this is not tamper authentication."""
    try:
        for data, digest in (("snapshot_json", "snapshot_digest"),
                             ("registry_json", "registry_digest"),
                             ("provider_json", "provider_digest")):
            if semantic_hash(json.loads(row[data])) != row[digest]:
                return "record_corrupt"
        budgets = json.loads(row["budgets_json"])
        for count, limit in (("model_attempts", "max_model_attempts"),
                             ("model_turns", "max_model_turns"), ("tool_calls", "max_tool_calls")):
            if (type(budgets[limit]) is not int or not 1 <= budgets[limit] <= 20
                    or type(row[count]) is not int or not 0 <= row[count] <= budgets[limit]):
                return "record_corrupt"
        if type(budgets["max_output_chars"]) is not int or not 100 <= budgets["max_output_chars"] <= 20000:
            return "record_corrupt"
        if row["summary_json"] is not None and semantic_hash(json.loads(row["summary_json"])) != row["summary_ref"]:
            return "record_corrupt"
        if row["status"] == "published":
            if row["phase"] != "published" or semantic_hash(json.loads(row["published_json"])) != row["publication_id"]:
                return "record_corrupt"
    except (TypeError, ValueError, KeyError, RecursionError):
        return "record_corrupt"
    return None


def _identity(row, tools: list[AgentTool], provider: ResponsesConfig, snapshot: Mapping[str, Any] | None) -> str | None:
    if semantic_hash(registry_public(tools)) != row["registry_digest"]:
        return "session_mismatch"
    if semantic_hash(provider_public(provider)) != row["provider_digest"]:
        return "session_mismatch"
    if snapshot is not None and semantic_hash(dict(snapshot)) != row["snapshot_digest"]:
        return "session_mismatch"
    return None


def _precheck(run_id, task, snapshot, tools, provider, instructions, effect_slot, budgets, stop_at):
    if not _RUN_ID.fullmatch(run_id) or not _RUN_ID.fullmatch(effect_slot):
        return ("unsupported", "model_required")
    if not isinstance(task, str) or not task or len(task) > 4000:
        return ("unsupported", "invalid_arguments")
    if not isinstance(instructions, str) or len(instructions) > INSTRUCTIONS_LIMIT:
        return ("unsupported", "invalid_arguments")
    if not isinstance(snapshot, Mapping):
        return ("unsupported", "invalid_arguments")
    try:
        semantic_hash(dict(snapshot))
    except TypeError:
        return ("unsupported", "invalid_arguments")
    problem = _tool_problem(list(tools), effect_slot)
    if problem:
        return ("unsupported", problem)
    provider_problem = config_problem(provider)
    if provider_problem:
        status = "safe_refusal" if provider_problem == "live_not_authorized" else "unsupported"
        return (status, provider_problem)
    if stop_at is not None and stop_at not in STOPS:
        return ("unsupported", "unsupported_stop_point")
    limits = budgets.as_dict()
    counts = ("max_model_turns", "max_tool_calls", "max_model_attempts")
    if any(type(limits[name]) is not int or not 1 <= limits[name] <= 20 for name in counts):
        return ("unsupported", "budget_exhausted")
    if type(limits["max_output_chars"]) is not int or not 100 <= limits["max_output_chars"] <= 20000:
        return ("unsupported", "budget_exhausted")
    return None


def _tool_problem(tools: list[AgentTool], effect_slot: str | None = None) -> str | None:
    if not tools:
        return "unknown_tool"
    names = [tool.name for tool in tools]
    if len(names) != len(set(names)):
        return "invalid_arguments"
    effects = [tool for tool in tools if tool.kind == "effect"]
    if len(effects) > 1:
        return "unsupported_output"
    for tool in tools:
        if tool.kind not in {"pure", "effect"}:
            return "unsupported_output"
        if tool.kind == "pure" and not callable(tool.fn):
            return "unknown_tool"
        if tool.kind == "pure" and (tool.parameters.get("properties") or tool.parameters.get("required")):
            return "unsupported_output"
        if tool.kind == "effect" and not tool.service_id:
            return "unknown_tool"
        if tool.parameters.get("type") != "object" or tool.parameters.get("additionalProperties") is not False:
            return "unsupported_output"
    if effect_slot is not None and len(effects) != 1:
        return "slot_not_ready"
    return None


def _effect_tool_name(tools: list[AgentTool]) -> str:
    return next(tool.name for tool in tools if tool.kind == "effect")


def _classify(response: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(response, dict):
        return {"kind": "bad", "reason": "protocol_error"}
    output = response.get("output")
    if not isinstance(output, list):
        return {"kind": "bad", "reason": "protocol_error"}
    for item in output:
        if not isinstance(item, dict) or item.get("type") not in SUPPORTED_OUTPUT_TYPES:
            return {"kind": "bad", "reason": "unsupported_output"}
    status = response.get("status")
    if status != "completed":
        if status == "failed":
            return {"kind": "bad", "reason": "model_failed"}
        if status == "incomplete":
            return {"kind": "bad", "reason": "model_incomplete"}
        return {"kind": "bad", "reason": "unsupported_status"}
    for item in output:
        kind = item["type"]
        if kind == "function_call":
            if item.get("status") not in {None, "completed"}:
                return {"kind": "bad", "reason": "model_incomplete"}
            if not all(isinstance(item.get(key), str) and 0 < len(item[key]) <= 200
                       for key in ("call_id", "name")):
                return {"kind": "bad", "reason": "protocol_error"}
        elif kind == "message":
            if item.get("role") != "assistant" or item.get("status") != "completed":
                return {"kind": "bad", "reason": "protocol_error"}
            content = item.get("content")
            if not isinstance(content, list) or not content:
                return {"kind": "bad", "reason": "protocol_error"}
            for part in content:
                if not isinstance(part, dict) or part.get("type") not in {"output_text", "refusal"}:
                    return {"kind": "bad", "reason": "unsupported_output"}
                key = "text" if part["type"] == "output_text" else "refusal"
                if not isinstance(part.get(key), str):
                    return {"kind": "bad", "reason": "protocol_error"}
    calls = [item for item in output if item.get("type") == "function_call"]
    if len(calls) > 1:
        return {"kind": "bad", "reason": "multiple_function_calls"}
    if any(_is_refusal(item) for item in output if item.get("type") == "message"):
        return {"kind": "bad", "reason": "model_refusal"}
    if len(calls) == 1:
        return {"kind": "call", "call": calls[0]}
    if any(item.get("type") == "message" for item in output):
        return {"kind": "final"}
    return {"kind": "bad", "reason": "unsupported_output"}


def _is_refusal(item: dict[str, Any]) -> bool:
    content = item.get("content")
    if not isinstance(content, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == "refusal" for part in content)


def _bad_status(reason: str) -> str:
    if reason in {"model_failed", "model_incomplete", "model_refusal", "budget_exhausted", "effect_conflict"}:
        return "safe_refusal"
    return "unsupported"


def _stop(conn, run_id: str, status: str, reason: str) -> AgentResult:
    conn.execute(
        "UPDATE agent_sessions SET status=?, phase='stopped', reason_code=? WHERE run_id=?",
        (status, reason, run_id),
    )
    return _from_row(conn, load_session(conn, run_id))


def _evidence(row, provider: ResponsesConfig | None) -> dict[str, Any]:
    stored = json.loads(row["provider_json"]) if row is not None and row["provider_json"] else {}
    mode = stored.get("mode", "local_protocol_fixture")
    model = stored.get("model")
    if provider is not None:
        mode = provider.mode
        model = provider.model
    return {
        "provider_mode": mode,
        "endpoint_class": "loopback" if provider is None else endpoint_class(provider.base_url),
        "model": model,
        "model_attempts": None if row is None else int(row["model_attempts"]),
        "model_turns": None if row is None else int(row["model_turns"]),
        "tool_calls": None if row is None else int(row["tool_calls"]),
        "effect_slot": None if row is None else row["effect_slot"],
        "effect_child_id": None if row is None else row["effect_child_id"],
        "actual_provider_api_calls": 0,
        "actual_token_usage": 0,
        "live_model_validation": "NOT_RUN_NOT_AUTHORIZED",
        "fixture_usage_is_not_token_usage": True,
    }


def _from_row(conn, row, warnings: list[str] | None = None) -> AgentResult:
    values = json.loads(row["published_json"]) if row["published_json"] else None
    status = row["status"] if row["status"] != "open" else ("published" if row["phase"] == "published" else "open")
    if row["phase"] == "may_have_sent":
        status = "unknown"
        reason = "model_request_unknown"
    elif row["phase"] == "tool_unknown":
        status = "unknown"
        reason = "effect_unknown"
    else:
        reason = row["reason_code"] or "open"
    return AgentResult(
        status=status,
        reason_code=reason,
        message=_message(reason),
        published=row["status"] == "published",
        phase=row["phase"],
        run_id=row["run_id"],
        values=values,
        publication_id=row["publication_id"],
        evidence=_evidence(row, None),
        warnings=list(warnings or []),
    )


def _result(
    status: str,
    reason: str,
    run_id: str,
    *,
    phase: str = "",
    evidence: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> AgentResult:
    return AgentResult(
        status=status,
        reason_code=reason,
        message=_message(reason),
        published=False,
        phase=phase,
        run_id=run_id,
        evidence=evidence or {
            "actual_provider_api_calls": 0,
            "actual_token_usage": 0,
            "live_model_validation": "NOT_RUN_NOT_AUTHORIZED",
        },
        warnings=list(warnings or []),
    )
