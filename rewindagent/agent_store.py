"""Agent session tables. They do not replace the durable run schema.

user_version stays the durable schema version. Agent tables are created together
and only when none of them exist yet.
"""

from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .canonical import semantic_hash

AGENT_FORMAT = 1
_TABLES = ("agent_meta", "agent_sessions", "agent_attempts", "agent_calls", "agent_items")


def session_lock_path(store: Path) -> Path:
    return Path(str(store) + ".lock")


class SessionLock:
    """Hold one agent coordinator across temporary durable-executor handoffs."""

    def __init__(self, store: Path) -> None:
        from .durable import _Lock

        self.coordinator = _Lock(Path(str(store) + ".agent.lock"))
        self.executor = _Lock(session_lock_path(store))

    def acquire(self) -> str | None:
        busy = self.coordinator.acquire()
        if busy:
            return busy
        try:
            busy = self.acquire_executor()
        except BaseException:
            self.coordinator.release()
            raise
        if busy:
            self.coordinator.release()
        return busy

    def acquire_executor(self) -> str | None:
        return self.executor.acquire()

    def release_executor(self) -> None:
        self.executor.release()

    def release(self) -> None:
        self.release_executor()
        self.coordinator.release()


def connect(store: Path, *, write: bool) -> sqlite3.Connection:
    mode = "rwc" if write else "ro"
    uri = f"{store.resolve().as_uri()}?mode={mode}"
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        if write:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=0")
        else:
            conn.execute("PRAGMA query_only=ON")
        row = conn.execute("PRAGMA integrity_check").fetchone()
        if row is None or row[0] != "ok":
            raise sqlite3.DatabaseError("integrity_check")
    except BaseException:
        conn.close()
        raise
    return conn


def schema_state(conn: sqlite3.Connection) -> str:
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    present = [name for name in _TABLES if name in names]
    if not present:
        return "absent"
    if len(present) != len(_TABLES):
        return "incomplete"
    required = {
        "agent_meta": "format_version",
        "agent_sessions": "run_id status phase task instructions snapshot_json snapshot_digest registry_json registry_digest provider_json provider_digest effect_slot effect_tool effect_child_id summary_json summary_ref budgets_json model_attempts model_turns tool_calls reason_code publication_id published_json",
        "agent_attempts": "run_id attempt_seq state response_json",
        "agent_calls": "run_id call_id name arguments_json arguments_digest child_run_id state output_json adopted",
        "agent_items": "run_id seq kind body_json",
    }
    for table, columns in required.items():
        actual = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not set(columns.split()) <= actual:
            return "incomplete"
    rows = conn.execute("SELECT format_version FROM agent_meta").fetchall()
    if len(rows) != 1 or rows[0][0] != AGENT_FORMAT:
        return "unknown_format"
    return "ok"


def ensure_schema(conn: sqlite3.Connection) -> str:
    state = schema_state(conn)
    if state != "absent":
        return state
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("CREATE TABLE agent_meta (format_version INTEGER NOT NULL)")
        conn.execute(
            """
            CREATE TABLE agent_sessions (
                run_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                phase TEXT NOT NULL,
                task TEXT NOT NULL,
                instructions TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                registry_json TEXT NOT NULL,
                registry_digest TEXT NOT NULL,
                provider_json TEXT NOT NULL,
                provider_digest TEXT NOT NULL,
                effect_slot TEXT NOT NULL,
                effect_tool TEXT NOT NULL,
                effect_child_id TEXT,
                summary_json TEXT,
                summary_ref TEXT,
                budgets_json TEXT NOT NULL,
                model_attempts INTEGER NOT NULL,
                model_turns INTEGER NOT NULL,
                tool_calls INTEGER NOT NULL,
                reason_code TEXT,
                publication_id TEXT,
                published_json TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE agent_attempts (
                run_id TEXT NOT NULL,
                attempt_seq INTEGER NOT NULL,
                state TEXT NOT NULL,
                response_json TEXT,
                PRIMARY KEY (run_id, attempt_seq)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE agent_calls (
                run_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                name TEXT NOT NULL,
                arguments_json TEXT NOT NULL,
                arguments_digest TEXT NOT NULL,
                child_run_id TEXT NOT NULL,
                state TEXT NOT NULL,
                output_json TEXT,
                adopted INTEGER NOT NULL,
                PRIMARY KEY (run_id, call_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE agent_items (
                run_id TEXT NOT NULL,
                seq INTEGER NOT NULL,
                kind TEXT NOT NULL,
                body_json TEXT NOT NULL,
                PRIMARY KEY (run_id, seq)
            )
            """
        )
        conn.execute("INSERT INTO agent_meta (format_version) VALUES (?)", (AGENT_FORMAT,))
        conn.execute("COMMIT")
    except sqlite3.Error:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    return "ok"


def canon(value: Any) -> str:
    semantic_hash(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def protocol_json(value: Any) -> str:
    """Opaque provider JSON. Floats are protocol fields, not business values."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def registry_public(tools: list[Any]) -> list[dict[str, Any]]:
    rows = []
    for tool in tools:
        rows.append(
            {
                "name": tool.name,
                "kind": tool.kind,
                "impl_id": tool.impl_id,
                "parameters": copy.deepcopy(tool.parameters),
                "service_id": tool.service_id,
                "description": tool.description,
            }
        )
    rows.sort(key=lambda item: item["name"])
    return rows


def provider_public(config: Any) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "model": config.model,
        "endpoint_class": "loopback",
        "store": False,
        "stream": False,
        "parallel_tool_calls": False,
        "max_retries": 0,
    }


def load_session(conn: sqlite3.Connection, run_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM agent_sessions WHERE run_id=?", (run_id,)).fetchone()


def items(conn: sqlite3.Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT body_json FROM agent_items WHERE run_id=? ORDER BY seq",
        (run_id,),
    ).fetchall()
    return [json.loads(row[0]) for row in rows]


def append_item(conn: sqlite3.Connection, run_id: str, kind: str, body: Mapping[str, Any]) -> None:
    seq = conn.execute(
        "SELECT COALESCE(MAX(seq), 0) + 1 FROM agent_items WHERE run_id=?",
        (run_id,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO agent_items (run_id, seq, kind, body_json) VALUES (?, ?, ?, ?)",
        (run_id, seq, kind, protocol_json(dict(body))),
    )


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        hidden = {"encrypted_content", "api_key", "authorization"}
        return {key: ("opaque" if key in hidden else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value
