"""Durable effect protocol. Query result not_found never authorizes a submit."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .canonical import semantic_hash
from .effectsvc import CONTRACT_VERSION, SERVICE_ID

MAX_ATTEMPTS = 3
EFFECT_FORMAT = 1
_EFFECT_COLUMNS = {
    "effect_ops": {
        "run_id",
        "logical_id",
        "step_id",
        "service_id",
        "contract_version",
        "idempotent",
        "query_supported",
        "retention_ms",
        "idempotency_key",
        "payload_json",
        "payload_digest",
        "state",
        "prepared_at_ms",
        "attempt_count",
        "receipt_json",
    },
    "effect_attempts": {"run_id", "logical_id", "attempt_seq", "attempt_id"},
}


@dataclass
class EffectStop:
    status: str
    reason: str
    evidence: dict[str, Any]


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def effect_schema_state(conn: sqlite3.Connection) -> str:
    """Return absent, ok, incomplete, or unknown_format. Does not create tables."""
    names = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    wanted = {"effect_meta", "effect_ops", "effect_attempts"}
    present = wanted & names
    if not present:
        return "absent"
    if present != wanted:
        return "incomplete"
    if _table_columns(conn, "effect_meta") != {"format_version"}:
        return "incomplete"
    for table, columns in _EFFECT_COLUMNS.items():
        if not columns <= _table_columns(conn, table):
            return "incomplete"
    row = conn.execute("SELECT format_version FROM effect_meta").fetchone()
    if row is None or int(row[0]) != EFFECT_FORMAT:
        return "unknown_format"
    return "ok"


def ensure_tables(conn: sqlite3.Connection) -> str:
    state = effect_schema_state(conn)
    if state != "absent":
        return state
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE effect_meta (format_version INTEGER NOT NULL)")
        conn.execute(
            """
            CREATE TABLE effect_ops (
                run_id TEXT NOT NULL,
                logical_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                service_id TEXT NOT NULL,
                contract_version TEXT NOT NULL,
                idempotent INTEGER NOT NULL,
                query_supported INTEGER NOT NULL,
                retention_ms INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                state TEXT NOT NULL,
                prepared_at_ms INTEGER NOT NULL,
                attempt_count INTEGER NOT NULL,
                receipt_json TEXT,
                PRIMARY KEY (run_id, logical_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE effect_attempts (
                run_id TEXT NOT NULL,
                logical_id TEXT NOT NULL,
                attempt_seq INTEGER NOT NULL,
                attempt_id TEXT NOT NULL,
                PRIMARY KEY (run_id, logical_id, attempt_seq)
            )
            """
        )
        conn.execute("INSERT INTO effect_meta (format_version) VALUES (?)", (EFFECT_FORMAT,))
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return "ok"


def effect_key(service_id: str, contract_version: str, run_id: str, logical_id: str) -> str:
    return semantic_hash(
        {
            "service_id": service_id,
            "contract_version": contract_version,
            "caller_scope": run_id,
            "logical_id": logical_id,
        }
    )


def _canon(value: Any) -> str:
    semantic_hash(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(conn: sqlite3.Connection, run_id: str, logical_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM effect_ops WHERE run_id=? AND logical_id=?",
        (run_id, logical_id),
    ).fetchone()


def _within_retention(row: sqlite3.Row, now_ms: int) -> bool:
    if not row["idempotent"]:
        return False
    return now_ms - int(row["prepared_at_ms"]) <= int(row["retention_ms"])


def _matches(row: sqlite3.Row, view: Mapping[str, Any]) -> bool:
    return (
        view.get("status") == "confirmed"
        and view.get("logical_id") == row["logical_id"]
        and view.get("payload_digest") == row["payload_digest"]
        and view.get("idempotency_key") == row["idempotency_key"]
    )


def advance_effect(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    step: dict[str, Any],
    state: dict[str, Any],
    service: Any,
    boundary: Any,
    now_ms: int,
    reconcile_only: bool,
) -> tuple[dict[str, Any], bool] | EffectStop:
    """Return updated slot state and whether the local step was already stored.

    A stop leaves the run open when the reason is unknown, and refuses when the
    receipt or payload is explicitly unacceptable.
    """
    if service is None or service.service_id != step["service_id"]:
        return EffectStop("safe_refusal", "service_unregistered", {"logical_id": step["logical_id"]})
    if step["service_id"] != SERVICE_ID:
        return EffectStop("safe_refusal", "service_contract_mismatch", {"logical_id": step["logical_id"]})
    schema = ensure_tables(conn)
    if schema == "unknown_format":
        return EffectStop("unsupported", "unknown_effect_format", {"logical_id": step["logical_id"]})
    if schema != "ok":
        return EffectStop("execution_failure", "effect_schema_incomplete", {"logical_id": step["logical_id"]})
    logical_id = step["logical_id"]
    payload = {"values": [state[slot] for slot in step["reads"]]}
    digest = semantic_hash(payload)
    boundary.reach("E0")
    row = _load(conn, run_id, logical_id)
    if row is None:
        caps = service.capabilities()
        if caps.get("status") == "unavailable" or caps.get("service_id") != SERVICE_ID:
            return EffectStop("unknown", "capabilities_unavailable", {"logical_id": logical_id})
        if caps.get("contract_version") != CONTRACT_VERSION:
            return EffectStop("safe_refusal", "service_contract_mismatch", {"logical_id": logical_id})
        key = effect_key(SERVICE_ID, CONTRACT_VERSION, run_id, logical_id)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO effect_ops (
                run_id, logical_id, step_id, service_id, contract_version, idempotent,
                query_supported, retention_ms, idempotency_key, payload_json, payload_digest,
                state, prepared_at_ms, attempt_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, 0)
            """,
            (
                run_id,
                logical_id,
                step["step_id"],
                SERVICE_ID,
                CONTRACT_VERSION,
                1 if caps.get("idempotent") else 0,
                1 if caps.get("query") else 0,
                int(caps.get("retention_ms", 0)),
                key,
                _canon(payload),
                digest,
                now_ms,
            ),
        )
        conn.execute("COMMIT")
        row = _load(conn, run_id, logical_id)
    assert row is not None
    if row["payload_digest"] != digest:
        _set_state(conn, row, "rejected", None)
        _refuse_run(conn, run_id, "effect_conflict")
        return EffectStop("safe_refusal", "effect_conflict", {"logical_id": logical_id})
    boundary.reach("E1")
    if row["state"] == "confirmed" and row["receipt_json"]:
        saved = json.loads(row["receipt_json"])
        return _confirm(conn, run_id, row, step, state, saved, boundary)
    if row["state"] == "rejected":
        return EffectStop("safe_refusal", "effect_conflict", {"logical_id": logical_id})
    live = service.capabilities()
    if live.get("status") == "unavailable":
        return EffectStop("unknown", "capabilities_unavailable", {"logical_id": logical_id})
    if (
        live.get("service_id") != row["service_id"]
        or live.get("contract_version") != row["contract_version"]
        or bool(live.get("idempotent")) != bool(row["idempotent"])
        or bool(live.get("query")) != bool(row["query_supported"])
    ):
        return EffectStop("safe_refusal", "service_contract_mismatch", {"logical_id": logical_id})
    view = _query(service, row)
    if view is not None and _matches(row, view):
        return _confirm(conn, run_id, row, step, state, view, boundary)
    if view is not None and view.get("status") == "confirmed":
        _set_state(conn, row, "rejected", _canon(dict(view)))
        _refuse_run(conn, run_id, "receipt_mismatch")
        return EffectStop("safe_refusal", "receipt_mismatch", {"logical_id": logical_id})
    if view is not None and view.get("status") == "conflict":
        _set_state(conn, row, "rejected", _canon(dict(view)))
        _refuse_run(conn, run_id, "effect_conflict")
        return EffectStop("safe_refusal", "effect_conflict", {"logical_id": logical_id})
    if row["state"] == "prepared":
        if reconcile_only:
            return EffectStop("unknown", "reconcile_no_submit", {"logical_id": logical_id, "effect_state": "prepared"})
        return _first_submit(conn, run_id, row, step, state, service, boundary, payload)
    # MAY_HAVE_EXECUTED or UNKNOWN. not_found is not permission to submit.
    if view is not None and view.get("status") == "not_found":
        _set_state(conn, row, "unknown", None)
        return EffectStop(
            "unknown",
            "effect_not_found",
            {"logical_id": logical_id, "effect_state": "unknown", "query_status": "not_found"},
        )
    if not row["idempotent"] or not _within_retention(row, now_ms):
        reason = "effect_unknown" if not row["idempotent"] else "retention_expired"
        _set_state(conn, row, "unknown", None)
        return EffectStop("unknown", reason, {"logical_id": logical_id, "effect_state": "unknown"})
    if view is not None and view.get("status") == "unavailable":
        _set_state(conn, row, "unknown", None)
        return EffectStop("unknown", "effect_unknown", {"logical_id": logical_id, "effect_state": "unknown"})
    if reconcile_only or row["attempt_count"] >= MAX_ATTEMPTS:
        reason = "reconcile_no_submit" if reconcile_only else "attempt_budget_exhausted"
        _set_state(conn, row, "unknown", None)
        return EffectStop("unknown", reason, {"logical_id": logical_id, "effect_state": "unknown"})
    if view is None:
        return _retry_same_key(conn, run_id, row, step, state, service, boundary, payload)
    _set_state(conn, row, "unknown", None)
    return EffectStop("unknown", "effect_unknown", {"logical_id": logical_id, "effect_state": "unknown"})


def _query(service: Any, row: sqlite3.Row) -> dict[str, Any] | None:
    if not row["query_supported"]:
        return None
    return service.query(row["idempotency_key"])


def _first_submit(conn, run_id, row, step, state, service, boundary, payload):
    _mark_attempt(conn, row, boundary)
    response = service.submit(_request(row, payload))
    return _after_response(conn, run_id, row, step, state, response, boundary)


def _retry_same_key(conn, run_id, row, step, state, service, boundary, payload):
    if row["attempt_count"] >= MAX_ATTEMPTS:
        return EffectStop("unknown", "attempt_budget_exhausted", {"logical_id": row["logical_id"]})
    _mark_attempt(conn, row, boundary)
    response = service.submit(_request(row, payload))
    return _after_response(conn, run_id, row, step, state, response, boundary)


def _request(row: sqlite3.Row, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "idempotency_key": row["idempotency_key"],
        "logical_id": row["logical_id"],
        "payload_digest": row["payload_digest"],
        "payload": payload,
        "contract_version": row["contract_version"],
    }


def _mark_attempt(conn: sqlite3.Connection, row: sqlite3.Row, boundary: Any) -> None:
    seq = int(row["attempt_count"]) + 1
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE effect_ops SET state='may_have_executed', attempt_count=?
        WHERE run_id=? AND logical_id=?
        """,
        (seq, row["run_id"], row["logical_id"]),
    )
    conn.execute(
        """
        INSERT INTO effect_attempts (run_id, logical_id, attempt_seq, attempt_id)
        VALUES (?, ?, ?, ?)
        """,
        (row["run_id"], row["logical_id"], seq, uuid.uuid4().hex),
    )
    conn.execute("COMMIT")
    boundary.reach("E2")


def _after_response(conn, run_id, row, step, state, response, boundary):
    boundary.reach("E5")
    fresh = _load(conn, run_id, row["logical_id"])
    assert fresh is not None
    if response.get("status") == "confirmed" and _matches(fresh, response):
        return _confirm(conn, run_id, fresh, step, state, response, boundary)
    if response.get("status") in {"conflict", "confirmed"}:
        _set_state(conn, fresh, "rejected", _canon(dict(response)))
        _refuse_run(conn, run_id, "effect_conflict" if response.get("status") == "conflict" else "receipt_mismatch")
        reason = "effect_conflict" if response.get("status") == "conflict" else "receipt_mismatch"
        return EffectStop("safe_refusal", reason, {"logical_id": fresh["logical_id"]})
    _set_state(conn, fresh, "unknown", None)
    return EffectStop("unknown", "effect_unknown", {"logical_id": fresh["logical_id"], "effect_state": "unknown"})


def _confirm(conn, run_id, row, step, state, view, boundary):
    projected = _project(state, step, row["payload_digest"])
    from .durable import _canon as canon_state
    from .durable import _insert_event

    output = [row["payload_digest"]]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE effect_ops SET state='confirmed', receipt_json=?
            WHERE run_id=? AND logical_id=?
            """,
            (_canon(dict(view)), run_id, row["logical_id"]),
        )
        boundary.reach("E6")
        conn.execute(
            """
            INSERT INTO step_commits (
                run_id, position, instance_id, tool_name, impl_id, config_digest,
                input_digest, output_json, output_digest, state_json, state_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                _position(conn, run_id, step["step_id"]),
                step["step_id"],
                row["logical_id"],
                f"effect:{row['service_id']}",
                semantic_hash(step["config"]),
                semantic_hash([semantic_hash(state[slot]) for slot in step["reads"]]),
                canon_state(output),
                semantic_hash(output),
                canon_state(projected),
                semantic_hash(projected),
            ),
        )
        _insert_event(
            conn,
            run_id,
            "effect_confirmed",
            {"logical_id": row["logical_id"], "payload_digest": row["payload_digest"]},
        )
        conn.execute("COMMIT")
    except SystemExit:
        conn.execute("ROLLBACK")
        raise
    boundary.reach("E7")
    return projected, False


def _position(conn: sqlite3.Connection, run_id: str, step_id: str) -> int:
    workflow = json.loads(conn.execute("SELECT workflow_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    for index, step in enumerate(workflow["steps"]):
        if step["step_id"] == step_id:
            return index
    raise KeyError(step_id)


def _project(state: dict[str, Any], step: dict[str, Any], digest: str) -> dict[str, Any]:
    import copy

    projected = copy.deepcopy(state)
    for slot in step["writes"]:
        projected[slot] = digest
    return projected


def _set_state(conn: sqlite3.Connection, row: sqlite3.Row, state: str, receipt: str | None) -> None:
    conn.execute(
        "UPDATE effect_ops SET state=?, receipt_json=COALESCE(?, receipt_json) WHERE run_id=? AND logical_id=?",
        (state, receipt, row["run_id"], row["logical_id"]),
    )


def _refuse_run(conn: sqlite3.Connection, run_id: str, reason: str) -> None:
    conn.execute(
        "UPDATE runs SET status='refused', reason_code=?, reason_message=? WHERE run_id=?",
        (reason, reason, run_id),
    )
