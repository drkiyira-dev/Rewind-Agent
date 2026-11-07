"""Local SQLite continuation for pure ordered steps.

The database stores tool names and implementation ids, never callables.
A resume process must be given a trusted registry by its caller.
"""

from __future__ import annotations

import copy
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from .api import ToolSpec
from .canonical import semantic_hash

SCHEMA_VERSION = 1
STOP_POINTS = ("K0", "K1", "K2", "K3", "K4", "K5", "E0", "E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8")
_RUN_ID = re.compile(r"[A-Za-z0-9._-]{1,80}")

LIMITS: tuple[str, ...] = (
    "只支持有序纯计算步骤和可规范化的 JSON 值。这不是任意进程快照。",
    "未提交的纯函数可以再次执行。这不是 exactly-once，也不是断电保证。",
    "工具版本字符串不能发现全部未声明的代码改动。",
    "一个本地 SQLite 文件同时只允许一个活动执行进程。不做多机锁。",
    "外部效果、逃逸工具和未声明读在执行前拒绝，不会改走进程内模拟器。",
    "stdout 不属于发布事务。",
)


def _message(code: str) -> str:
    return {
        "published": "任务结果已在数据库里提交。",
        "executor_busy": "这个存储文件已有活动的执行或恢复进程。",
        "unsupported_platform": "当前平台没有实现进程级互斥。",
        "unsupported_effect": "持久任务不执行外部效果。",
        "unsupported_escape": "工具可能离开受控接口，持久任务拒绝执行。",
        "unsupported_undeclared": "工具带有未声明读，持久任务拒绝执行。",
        "unsupported_value": "值不是受支持的 JSON 数据。",
        "unsupported_workflow_schema": "工作流 schema_version 不受支持。",
        "unsupported_stop_point": "这个命令没有该终止点。",
        "empty_workflow": "工作流没有步骤。",
        "invalid_slot_reference": "步骤读取了尚未定义的槽。",
        "duplicate_step": "步骤标识重复。",
        "store_not_found": "resume 不会创建缺失的库。",
        "store_corrupt": "存储文件损坏，没有用空库替换它。",
        "store_uninitialized": "存储文件没有完成初始化。",
        "unknown_schema_version": "数据库 schema 版本无法识别。",
        "record_corrupt": "已提交记录的摘要不一致，停止且不重建任务。",
        "run_not_found": "库里没有这个 run_id。",
        "run_exists": "run_id 已经存在。继续任务请使用 resume。",
        "workflow_mismatch": "工作流摘要与已保存任务不一致。",
        "input_mismatch": "输入摘要与已保存任务不一致。",
        "tool_missing": "受信任登记表里没有任务需要的工具。",
        "tool_version_mismatch": "工具实现版本与已保存任务不一致。",
        "open": "任务尚未发布。已提交步骤仍然保留。",
        "persistent_tool_failure": "工具失败已被提交，不再执行。",
        "tool_output_arity": "工具返回值个数与写入槽不一致，失败已提交。",
        "effect_unknown": "外部效果结果未知。没有发布，也没有把它当成确定失败。",
        "effect_not_found": "查询当前未找到。这不能证明操作没有执行，所以没有重新提交。",
        "effect_conflict": "同一逻辑操作的载荷或身份冲突，已拒绝。",
        "receipt_mismatch": "服务回执与已保存的操作不一致，没有发布。",
        "retention_expired": "幂等保留期限已过，没有重新提交。",
        "service_contract_mismatch": "服务身份或契约版本与已保存的记录不一致。",
        "service_unregistered": "没有登记这个效果服务。",
        "capabilities_unavailable": "读不到服务公开契约，操作还没有提交。",
        "reconcile_no_submit": "核对不会发起新的业务提交。",
        "attempt_budget_exhausted": "发送次数已用完，没有继续提交。",
        "unknown_effect_format": "效果库格式标识无法识别，已停止。",
        "effect_schema_incomplete": "效果表不完整，没有补建空表。",
        "tool_not_callable": "登记的工具不可调用。",
    }.get(code, "持久任务停止。没有更具体的已记录原因。")


@dataclass
class DurableResult:
    status: str
    reason_code: str
    message: str
    published: bool
    values: dict[str, Any] | None
    recomputed: list[str]
    reused: list[str]
    run_id: str
    publication_id: str | None
    attempt_id: str | None
    committed_steps: list[str]
    warnings: list[str] = field(default_factory=list)
    limits: list[str] = field(default_factory=lambda: list(LIMITS))
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason_code": self.reason_code,
            "message": self.message,
            "published": self.published,
            "values": copy.deepcopy(self.values),
            "recomputed": list(self.recomputed),
            "reused": list(self.reused),
            "run_id": self.run_id,
            "publication_id": self.publication_id,
            "attempt_id": self.attempt_id,
            "committed_steps": list(self.committed_steps),
            "warnings": list(self.warnings),
            "limits": list(self.limits),
            "evidence": copy.deepcopy(self.evidence),
        }


class Boundary:
    """Test hook. Resume callers leave stop_at empty; the library never reads a fault file."""

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


def _result(
    status: str,
    code: str,
    run_id: str = "",
    *,
    published: bool = False,
    values: dict[str, Any] | None = None,
    recomputed: list[str] | None = None,
    reused: list[str] | None = None,
    publication_id: str | None = None,
    attempt_id: str | None = None,
    committed: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> DurableResult:
    return DurableResult(
        status=status,
        reason_code=code,
        message=_message(code),
        published=published,
        values=copy.deepcopy(values) if published else None,
        recomputed=list(recomputed or []),
        reused=list(reused or []),
        run_id=run_id,
        publication_id=publication_id if published else None,
        attempt_id=attempt_id,
        committed_steps=list(committed or []),
        evidence={} if evidence is None else evidence,
    )


def _canon(value: Any) -> str:
    semantic_hash(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _check_value(value: Any) -> str | None:
    if value is None or isinstance(value, str):
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None
    if isinstance(value, list):
        for item in value:
            bad = _check_value(item)
            if bad:
                return bad
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return "unsupported_value"
            bad = _check_value(item)
            if bad:
                return bad
        return None
    return "unsupported_value"


def _parse_workflow(
    raw: Mapping[str, Any], *, allow_effects: bool = False
) -> tuple[list[dict[str, Any]], str] | str:
    if raw.get("schema_version") != SCHEMA_VERSION:
        return "unsupported_workflow_schema"
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        return "empty_workflow"
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in steps:
        if not isinstance(item, dict):
            return "unsupported_value"
        effect = bool(item.get("effect") or item.get("kind") == "effect")
        if effect and not allow_effects:
            return "unsupported_effect"
        step_id = item.get("step_id")
        reads = item.get("reads", [])
        writes = item.get("writes", [])
        config = item.get("config", {})
        if not isinstance(step_id, str):
            return "unsupported_value"
        if not isinstance(reads, list) or not isinstance(writes, list):
            return "unsupported_value"
        if not all(isinstance(name, str) for name in reads + writes):
            return "unsupported_value"
        if _check_value(config):
            return "unsupported_value"
        if step_id in seen:
            return "duplicate_step"
        seen.add(step_id)
        if effect:
            logical_id = item.get("logical_id")
            service_id = item.get("service_id")
            if not isinstance(logical_id, str) or not isinstance(service_id, str):
                return "unsupported_value"
            normalized.append(
                {
                    "step_id": step_id,
                    "kind": "effect",
                    "logical_id": logical_id,
                    "service_id": service_id,
                    "reads": list(reads),
                    "writes": list(writes),
                    "config": copy.deepcopy(config),
                }
            )
            continue
        tool = item.get("tool")
        if not isinstance(tool, str):
            return "unsupported_value"
        normalized.append(
            {
                "step_id": step_id,
                "kind": "tool",
                "tool": tool,
                "reads": list(reads),
                "writes": list(writes),
                "config": copy.deepcopy(config),
            }
        )
    return normalized, semantic_hash({"schema_version": SCHEMA_VERSION, "steps": normalized})


def _parse_input(raw: Mapping[str, Any], steps: list[dict[str, Any]]) -> tuple[dict[str, Any], str] | str:
    if not isinstance(raw, dict) or _check_value(dict(raw)):
        return "unsupported_value"
    initial = copy.deepcopy(dict(raw))
    defined = set(initial)
    for step in steps:
        for slot in step["reads"]:
            if slot not in defined:
                return "invalid_slot_reference"
        defined.update(step["writes"])
    return initial, semantic_hash(initial)


def _tool_index(tools: Sequence[ToolSpec]) -> dict[str, ToolSpec] | str:
    found: dict[str, ToolSpec] = {}
    for spec in tools:
        if spec.name in found:
            return "duplicate_step"
        if not callable(spec.fn):
            return "tool_not_callable"
        if spec.escapes:
            return "unsupported_escape"
        if spec.undeclared_slot:
            return "unsupported_undeclared"
        impl = spec.impl_id or f"{spec.name}.v1"
        found[spec.name] = ToolSpec(spec.name, spec.fn, impl, spec.escapes, spec.undeclared_slot)
    return found


def _specs_for(steps: list[dict[str, Any]], tools: dict[str, ToolSpec]) -> tuple[list[dict[str, str]], str] | str:
    rows = []
    for step in steps:
        if step.get("kind") == "effect":
            continue
        spec = tools.get(step["tool"])
        if spec is None:
            return "tool_missing"
        rows.append({"name": spec.name, "impl_id": spec.impl_id})
    rows.sort(key=lambda item: item["name"])
    unique = []
    for item in rows:
        if unique and unique[-1] == item:
            continue
        unique.append(item)
    return unique, semantic_hash(unique)


class _Lock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> str | None:
        if os.name != "posix":
            return "unsupported_platform"
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.fd)
            self.fd = None
            return "executor_busy"
        return None

    def release(self) -> None:
        if self.fd is None:
            return
        import fcntl

        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        self.fd = None


def _uri(path: Path, mode: str) -> str:
    return f"file:{path.resolve().as_posix()}?mode={mode}"


def _configure(conn: sqlite3.Connection, *, write: bool) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if write:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=0")
    else:
        conn.execute("PRAGMA query_only=ON")


def _open(path: Path, *, write: bool) -> sqlite3.Connection:
    mode = "rwc" if write else "ro"
    try:
        conn = sqlite3.connect(_uri(path, mode), uri=True, isolation_level=None)
    except sqlite3.Error as exc:
        raise sqlite3.DatabaseError(str(exc)) from exc
    try:
        _configure(conn, write=write)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError("integrity_check")
    except BaseException:
        conn.close()
        raise
    return conn


def _version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _has_runs(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'"
    ).fetchone()
    return row is not None


def _init(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            status TEXT NOT NULL,
            workflow_json TEXT NOT NULL,
            workflow_digest TEXT NOT NULL,
            input_json TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            tool_specs_json TEXT NOT NULL,
            tool_specs_digest TEXT NOT NULL,
            published_json TEXT,
            published_digest TEXT,
            reason_code TEXT,
            reason_message TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE step_commits (
            run_id TEXT NOT NULL,
            position INTEGER NOT NULL,
            instance_id TEXT NOT NULL,
            tool_name TEXT NOT NULL,
            impl_id TEXT NOT NULL,
            config_digest TEXT NOT NULL,
            input_digest TEXT NOT NULL,
            output_json TEXT NOT NULL,
            output_digest TEXT NOT NULL,
            state_json TEXT NOT NULL,
            state_digest TEXT NOT NULL,
            PRIMARY KEY (run_id, instance_id),
            UNIQUE (run_id, position),
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            kind TEXT NOT NULL,
            body_json TEXT NOT NULL,
            body_digest TEXT NOT NULL,
            PRIMARY KEY (run_id, seq),
            FOREIGN KEY (run_id) REFERENCES runs(run_id)
        )
        """
    )
    conn.execute("COMMIT")
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _insert_event(conn: sqlite3.Connection, run_id: str, kind: str, body: Mapping[str, Any]) -> None:
    payload = copy.deepcopy(dict(body))
    text = _canon(payload)
    seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO events (run_id, seq, kind, body_json, body_digest) VALUES (?, ?, ?, ?, ?)",
        (run_id, seq, kind, text, semantic_hash(payload)),
    )


def _load_run(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()


def _committed_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT instance_id FROM step_commits WHERE run_id=? ORDER BY position",
        (run_id,),
    ).fetchall()
    return [row["instance_id"] for row in rows]


def _published_count(conn: sqlite3.Connection, run_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM events WHERE run_id=? AND kind='published'",
        (run_id,),
    ).fetchone()
    return int(row["n"])


def _classify_file(path: Path) -> str:
    if not path.exists():
        return "store_not_found"
    if path.is_dir() or path.stat().st_size == 0:
        return "store_corrupt"
    try:
        conn = _open(path, write=False)
    except sqlite3.Error:
        return "store_corrupt"
    try:
        version = _version(conn)
        has_runs = _has_runs(conn)
    finally:
        conn.close()
    if version == 0 and not has_runs:
        return "store_uninitialized"
    if version == 0 and has_runs:
        return "record_corrupt"
    if version != SCHEMA_VERSION:
        return "unknown_schema_version"
    if not has_runs:
        return "record_corrupt"
    return "ok"


def _evidence(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    return {
        "workflow_digest": row["workflow_digest"],
        "input_digest": row["input_digest"],
        "tool_specs_digest": row["tool_specs_digest"],
        "db_status": row["status"],
        "published_event_count": _published_count(conn, row["run_id"]),
        "reason_code": row["reason_code"] or "unavailable",
    }


def _finish_from_row(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    attempt_id: str | None,
    recomputed: list[str],
    reused: list[str],
) -> DurableResult:
    committed = _committed_ids(conn, row["run_id"])
    evidence = _evidence(conn, row)
    if row["status"] == "refused":
        return _result(
            "safe_refusal",
            row["reason_code"] or "effect_conflict",
            row["run_id"],
            attempt_id=attempt_id,
            committed=committed,
            evidence=evidence,
        )
    if row["status"] == "published":
        values = json.loads(row["published_json"])
        return _result(
            "published",
            "published",
            row["run_id"],
            published=True,
            values=values,
            recomputed=recomputed,
            reused=reused,
            publication_id=row["published_digest"],
            attempt_id=attempt_id,
            committed=committed,
            evidence=evidence,
        )
    code = row["reason_code"] or "record_corrupt"
    status = "execution_failure"
    if code in {"workflow_mismatch", "input_mismatch", "tool_missing", "tool_version_mismatch"}:
        status = "safe_refusal"
    return _result(
        status,
        code,
        row["run_id"],
        attempt_id=attempt_id,
        committed=committed,
        evidence=evidence,
    )


def _check_identity(
    row: sqlite3.Row,
    workflow: Mapping[str, Any] | None,
    initial: Mapping[str, Any] | None,
    tools: dict[str, ToolSpec],
) -> str | None:
    if workflow is not None:
        parsed = _parse_workflow(workflow, allow_effects=True)
        if isinstance(parsed, str):
            return parsed
        _steps, digest = parsed
        if digest != row["workflow_digest"]:
            return "workflow_mismatch"
    if initial is not None:
        if _check_value(dict(initial)):
            return "unsupported_value"
        if semantic_hash(dict(initial)) != row["input_digest"]:
            return "input_mismatch"
    stored = json.loads(row["tool_specs_json"])
    for item in stored:
        spec = tools.get(item["name"])
        if spec is None:
            return "tool_missing"
        if spec.impl_id != item["impl_id"]:
            return "tool_version_mismatch"
    return None


def _rebuild(conn: sqlite3.Connection, row: sqlite3.Row) -> tuple[dict[str, Any], list[dict[str, Any]]] | str:
    workflow = json.loads(row["workflow_json"])
    initial = json.loads(row["input_json"])
    if semantic_hash(workflow) != row["workflow_digest"] or semantic_hash(initial) != row["input_digest"]:
        return "record_corrupt"
    steps = workflow["steps"]
    commits = conn.execute(
        "SELECT * FROM step_commits WHERE run_id=? ORDER BY position",
        (row["run_id"],),
    ).fetchall()
    if len(commits) > len(steps):
        return "record_corrupt"
    state = copy.deepcopy(initial)
    for index, saved in enumerate(commits):
        step = steps[index]
        if saved["instance_id"] != step["step_id"] or saved["position"] != index:
            return "record_corrupt"
        inputs = [semantic_hash(state[slot]) for slot in step["reads"]]
        if semantic_hash(inputs) != saved["input_digest"]:
            return "record_corrupt"
        if semantic_hash(step["config"]) != saved["config_digest"]:
            return "record_corrupt"
        outputs = json.loads(saved["output_json"])
        if semantic_hash(outputs) != saved["output_digest"]:
            return "record_corrupt"
        if not isinstance(outputs, list) or len(outputs) != len(step["writes"]):
            return "record_corrupt"
        for slot, value in zip(step["writes"], outputs):
            state[slot] = value
        stored_state = json.loads(saved["state_json"])
        if stored_state != state or semantic_hash(state) != saved["state_digest"]:
            return "record_corrupt"
        spec_rows = json.loads(row["tool_specs_json"])
        known = {item["name"]: item["impl_id"] for item in spec_rows}
        if step.get("kind") == "effect":
            if saved["tool_name"] != step["logical_id"]:
                return "record_corrupt"
        elif known.get(step["tool"]) != saved["impl_id"]:
            return "record_corrupt"
    return state, steps


def _integrity(conn: sqlite3.Connection, row: sqlite3.Row) -> str | None:
    rebuilt = _rebuild(conn, row)
    if isinstance(rebuilt, str):
        return rebuilt
    if row["status"] != "published":
        return None
    state, _steps = rebuilt
    if not row["published_json"] or not row["published_digest"]:
        return "record_corrupt"
    saved = json.loads(row["published_json"])
    if saved != state or row["published_digest"] != semantic_hash(state):
        return "record_corrupt"
    return None


def _rollback(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def _commit_failure(conn: sqlite3.Connection, run_id: str, code: str, attempt_id: str) -> None:
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE runs SET status='execution_failure', reason_code=?, reason_message=? WHERE run_id=?",
        (code, _message(code), run_id),
    )
    _insert_event(conn, run_id, "tool_failed", {"attempt_id": attempt_id, "reason_code": code})
    conn.execute("COMMIT")


def _run_open(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    tools: dict[str, ToolSpec],
    boundary: Boundary,
    attempt_id: str,
    services: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
    reconcile_only: bool = False,
) -> DurableResult:
    rebuilt = _rebuild(conn, row)
    if isinstance(rebuilt, str):
        return _result("execution_failure", rebuilt, row["run_id"], attempt_id=attempt_id)
    state, steps = rebuilt
    done = {
        item["instance_id"]
        for item in conn.execute(
        "SELECT instance_id FROM step_commits WHERE run_id=?",
        (row["run_id"],),
    ).fetchall()}
    recomputed: list[str] = []
    reused: list[str] = []
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    for index, step in enumerate(steps):
        if step["step_id"] in done:
            reused.append(step["step_id"])
            continue
        if step.get("kind") == "effect":
            from .effectflow import EffectStop, advance_effect

            service = None if services is None else services.get(step["service_id"])
            outcome = advance_effect(
                conn,
                run_id=row["run_id"],
                step=step,
                state=state,
                service=service,
                boundary=boundary,
                now_ms=now_ms,
                reconcile_only=reconcile_only,
            )
            if isinstance(outcome, EffectStop):
                return _result(
                    outcome.status,
                    outcome.reason,
                    row["run_id"],
                    attempt_id=attempt_id,
                    committed=_committed_ids(conn, row["run_id"]),
                    evidence=outcome.evidence,
                )
            state, already = outcome
            (reused if already else recomputed).append(step["step_id"])
            continue
        spec = tools[step["tool"]]
        values = [copy.deepcopy(state[slot]) for slot in step["reads"]]
        try:
            outputs = spec.fn(values)
        except Exception:
            _commit_failure(conn, row["run_id"], "persistent_tool_failure", attempt_id)
            fresh = _load_run(conn, row["run_id"])
            assert fresh is not None
            return _finish_from_row(conn, fresh, attempt_id=attempt_id, recomputed=recomputed, reused=reused)
        boundary.reach("K1")
        if not isinstance(outputs, tuple) or len(outputs) != len(step["writes"]):
            _commit_failure(conn, row["run_id"], "tool_output_arity", attempt_id)
            fresh = _load_run(conn, row["run_id"])
            assert fresh is not None
            return _finish_from_row(conn, fresh, attempt_id=attempt_id, recomputed=recomputed, reused=reused)
        if _check_value(list(outputs)):
            _commit_failure(conn, row["run_id"], "unsupported_value", attempt_id)
            fresh = _load_run(conn, row["run_id"])
            assert fresh is not None
            return _finish_from_row(conn, fresh, attempt_id=attempt_id, recomputed=recomputed, reused=reused)
        projected = copy.deepcopy(state)
        for slot, value in zip(step["writes"], outputs):
            projected[slot] = copy.deepcopy(value)
        output_list = list(outputs)
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO step_commits (
                    run_id, position, instance_id, tool_name, impl_id, config_digest,
                    input_digest, output_json, output_digest, state_json, state_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    index,
                    step["step_id"],
                    step["tool"],
                    spec.impl_id,
                    semantic_hash(step["config"]),
                    semantic_hash([semantic_hash(state[slot]) for slot in step["reads"]]),
                    _canon(output_list),
                    semantic_hash(output_list),
                    _canon(projected),
                    semantic_hash(projected),
                ),
            )
            boundary.reach("K2")
            _insert_event(
                conn,
                row["run_id"],
                "step_committed",
                {"instance_id": step["step_id"], "attempt_id": attempt_id, "output_digest": semantic_hash(output_list)},
            )
            conn.execute("COMMIT")
        except SystemExit:
            _rollback(conn)
            raise
        except sqlite3.Error:
            _rollback(conn)
            return _result("execution_failure", "record_corrupt", row["run_id"], attempt_id=attempt_id)
        state = projected
        recomputed.append(step["step_id"])
        boundary.reach("K3")
    text = _canon(state)
    digest = semantic_hash(state)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE runs
            SET published_json=?, published_digest=?, reason_code='published', reason_message=?
            WHERE run_id=?
            """,
            (text, digest, _message("published"), row["run_id"]),
        )
        boundary.reach("K4")
        conn.execute("UPDATE runs SET status='published' WHERE run_id=?", (row["run_id"],))
        _insert_event(conn, row["run_id"], "published", {"publication_id": digest, "attempt_id": attempt_id})
        conn.execute("COMMIT")
    except SystemExit:
        _rollback(conn)
        raise
    except sqlite3.Error:
        _rollback(conn)
        return _result("execution_failure", "record_corrupt", row["run_id"], attempt_id=attempt_id)
    boundary.reach("K5")
    boundary.reach("E8")
    fresh = _load_run(conn, row["run_id"])
    assert fresh is not None
    return _finish_from_row(conn, fresh, attempt_id=attempt_id, recomputed=recomputed, reused=reused)


def _prepare_boundary(stop_at: str | None, ready_path: Path | None, timeout_s: float) -> Boundary | DurableResult:
    if stop_at is not None and stop_at not in STOP_POINTS:
        return _result("unsupported", "unsupported_stop_point")
    return Boundary(stop_at, ready_path, timeout_s)


def start_run(
    store: Path,
    run_id: str,
    workflow: Mapping[str, Any],
    initial: Mapping[str, Any],
    tools: Sequence[ToolSpec],
    *,
    services: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
    stop_at: str | None = None,
    ready_path: Path | None = None,
    boundary_timeout_s: float = 60,
) -> DurableResult:
    if not _RUN_ID.fullmatch(run_id):
        return _result("unsupported", "unsupported_value", run_id)
    boundary = _prepare_boundary(stop_at, ready_path, boundary_timeout_s)
    if isinstance(boundary, DurableResult):
        return boundary
    parsed = _parse_workflow(workflow, allow_effects=services is not None)
    if isinstance(parsed, str):
        return _result("unsupported", parsed, run_id)
    steps, workflow_digest = parsed
    parsed_input = _parse_input(initial, steps)
    if isinstance(parsed_input, str):
        return _result("unsupported", parsed_input, run_id)
    initial_value, input_digest = parsed_input
    indexed = _tool_index(tools)
    if isinstance(indexed, str):
        return _result("unsupported", indexed, run_id)
    specs = _specs_for(steps, indexed)
    if isinstance(specs, str):
        return _result("safe_refusal" if specs == "tool_missing" else "unsupported", specs, run_id)
    tool_rows, tool_digest = specs
    holder = _Lock(Path(str(store) + ".lock"))
    busy = holder.acquire()
    if busy:
        status = "busy" if busy == "executor_busy" else "unsupported"
        return _result(status, busy, run_id)
    conn: sqlite3.Connection | None = None
    try:
        existed = store.exists()
        if existed:
            kind = _classify_file(store)
            if kind != "ok":
                status = "unsupported" if kind == "unknown_schema_version" else "execution_failure"
                if kind == "store_not_found":
                    status = "not_applicable"
                return _result(status, kind, run_id)
        try:
            conn = _open(store, write=True)
        except sqlite3.Error:
            return _result("execution_failure", "store_corrupt", run_id)
        if not existed or not _has_runs(conn) or _version(conn) != SCHEMA_VERSION:
            if _version(conn) not in {0, SCHEMA_VERSION}:
                return _result("unsupported", "unknown_schema_version", run_id)
            if _version(conn) == SCHEMA_VERSION and not _has_runs(conn):
                return _result("execution_failure", "record_corrupt", run_id)
            if _version(conn) == 0 and _has_runs(conn):
                return _result("execution_failure", "record_corrupt", run_id)
            if _version(conn) == 0:
                _init(conn)
        if _load_run(conn, run_id) is not None:
            return _result("not_applicable", "run_exists", run_id)
        attempt_id = uuid.uuid4().hex
        workflow_text = _canon({"schema_version": SCHEMA_VERSION, "steps": steps})
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, schema_version, status, workflow_json, workflow_digest,
                    input_json, input_digest, tool_specs_json, tool_specs_digest, reason_code
                ) VALUES (?, ?, 'open', ?, ?, ?, ?, ?, ?, 'open')
                """,
                (
                    run_id,
                    SCHEMA_VERSION,
                    workflow_text,
                    workflow_digest,
                    _canon(initial_value),
                    input_digest,
                    _canon(tool_rows),
                    tool_digest,
                ),
            )
            _insert_event(
                conn,
                run_id,
                "registered",
                {
                    "attempt_id": attempt_id,
                    "workflow_digest": workflow_digest,
                    "input_digest": input_digest,
                    "tool_specs_digest": tool_digest,
                },
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            _rollback(conn)
            return _result("execution_failure", "record_corrupt", run_id)
        boundary.reach("K0")
        row = _load_run(conn, run_id)
        assert row is not None
        return _run_open(
            conn,
            row,
            indexed,
            boundary,
            attempt_id,
            services=services,
            now_ms=now_ms,
        )
    finally:
        if conn is not None:
            conn.close()
        holder.release()


def resume_run(
    store: Path,
    run_id: str,
    tools: Sequence[ToolSpec],
    *,
    services: Mapping[str, Any] | None = None,
    workflow: Mapping[str, Any] | None = None,
    initial: Mapping[str, Any] | None = None,
    now_ms: int | None = None,
    reconcile_only: bool = False,
    stop_at: str | None = None,
    ready_path: Path | None = None,
    boundary_timeout_s: float = 60,
) -> DurableResult:
    if not _RUN_ID.fullmatch(run_id):
        return _result("unsupported", "unsupported_value", run_id)
    if stop_at == "K0":
        return _result("unsupported", "unsupported_stop_point", run_id)
    boundary = _prepare_boundary(stop_at, ready_path, boundary_timeout_s)
    if isinstance(boundary, DurableResult):
        return boundary
    indexed = _tool_index(tools)
    if isinstance(indexed, str):
        return _result("unsupported", indexed, run_id)
    if not store.exists():
        return _result("not_applicable", "store_not_found", run_id)
    kind = _classify_file(store)
    if kind != "ok":
        status = {
            "store_not_found": "not_applicable",
            "unknown_schema_version": "unsupported",
        }.get(kind, "execution_failure")
        return _result(status, kind, run_id)
    holder = _Lock(Path(str(store) + ".lock"))
    busy = holder.acquire()
    if busy:
        status = "busy" if busy == "executor_busy" else "unsupported"
        return _result(status, busy, run_id)
    conn: sqlite3.Connection | None = None
    try:
        try:
            conn = _open(store, write=True)
        except sqlite3.Error:
            return _result("execution_failure", "store_corrupt", run_id)
        row = _load_run(conn, run_id)
        if row is None:
            return _result("not_applicable", "run_not_found", run_id)
        mismatch = _check_identity(row, workflow, initial, indexed)
        if mismatch:
            status = "unsupported" if mismatch.startswith("unsupported") or mismatch in {
                "empty_workflow",
                "invalid_slot_reference",
                "duplicate_step",
            } else "safe_refusal"
            return _result(status, mismatch, run_id, committed=_committed_ids(conn, run_id))
        problem = _integrity(conn, row)
        if problem:
            return _result("execution_failure", problem, run_id, committed=_committed_ids(conn, run_id))
        if row["status"] != "open":
            return _finish_from_row(conn, row, attempt_id=None, recomputed=[], reused=_committed_ids(conn, run_id))
        attempt_id = uuid.uuid4().hex
        return _run_open(
            conn,
            row,
            indexed,
            boundary,
            attempt_id,
            services=services,
            now_ms=now_ms,
            reconcile_only=reconcile_only,
        )
    finally:
        if conn is not None:
            conn.close()
        holder.release()


def reconcile_run(
    store: Path,
    run_id: str,
    tools: Sequence[ToolSpec],
    services: Mapping[str, Any],
    *,
    now_ms: int | None = None,
    boundary_timeout_s: float = 60,
) -> DurableResult:
    """Query and adopt an uncertain effect. This does not submit a new business operation."""
    return resume_run(
        store,
        run_id,
        tools,
        services=services,
        now_ms=now_ms,
        reconcile_only=True,
        boundary_timeout_s=boundary_timeout_s,
    )


def query_run(store: Path, run_id: str) -> DurableResult:
    if not store.exists():
        return _result("not_applicable", "store_not_found", run_id)
    kind = _classify_file(store)
    if kind != "ok":
        status = "unsupported" if kind == "unknown_schema_version" else "execution_failure"
        if kind == "store_not_found":
            status = "not_applicable"
        return _result(status, kind, run_id)
    try:
        conn = _open(store, write=False)
    except sqlite3.Error:
        return _result("execution_failure", "store_corrupt", run_id)
    try:
        row = _load_run(conn, run_id)
        if row is None:
            return _result("not_applicable", "run_not_found", run_id)
        problem = _integrity(conn, row)
        if problem:
            return _result("execution_failure", problem, run_id, committed=_committed_ids(conn, run_id))
        if row["status"] == "open":
            return _result(
                "open",
                "open",
                run_id,
                committed=_committed_ids(conn, run_id),
                evidence=_evidence(conn, row),
            )
        reused = _committed_ids(conn, run_id) if row["status"] == "published" else []
        return _finish_from_row(conn, row, attempt_id=None, recomputed=[], reused=reused)
    finally:
        conn.close()


def read_storage_settings(store: Path) -> dict[str, Any]:
    conn = _open(store, write=False)
    try:
        sync = int(conn.execute("PRAGMA synchronous").fetchone()[0])
        names = {0: "OFF", 1: "NORMAL", 2: "FULL", 3: "EXTRA"}
        return {
            "sqlite_version": sqlite3.sqlite_version,
            "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
            "synchronous": names.get(sync, str(sync)),
            "synchronous_value": sync,
            "foreign_keys": int(conn.execute("PRAGMA foreign_keys").fetchone()[0]),
            "user_version": _version(conn),
            "transaction_control": "isolation_level=None with explicit BEGIN IMMEDIATE/COMMIT/ROLLBACK",
        }
    finally:
        conn.close()


def render_store(store: Path, run_id: str) -> str:
    """Read committed rows only. Does not import tools or execute steps."""
    result = query_run(store, run_id)
    lines = [
        f"status: {result.status}",
        f"published: {str(result.published).lower()}",
        f"reason_code: {result.reason_code}",
        f"message: {result.message}",
        f"publication_id: {result.publication_id or 'unavailable'}",
        f"committed_steps: {', '.join(result.committed_steps) if result.committed_steps else '(none)'}",
        f"values: {json.dumps(result.values, ensure_ascii=False, sort_keys=True) if result.values is not None else 'unavailable'}",
    ]
    if not store.exists():
        lines.append("events: unavailable")
        return "\n".join(lines) + "\n"
    try:
        conn = _open(store, write=False)
    except sqlite3.Error:
        lines.append("events: unavailable")
        return "\n".join(lines) + "\n"
    try:
        rows = conn.execute(
            "SELECT seq, kind, body_json FROM events WHERE run_id=? ORDER BY seq",
            (run_id,),
        ).fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn.close()
    if not rows:
        lines.append("events: unavailable")
    else:
        shown = [f"{row['seq']}:{row['kind']}" for row in rows]
        lines.append("events: " + ", ".join(shown))
    return "\n".join(lines) + "\n"
