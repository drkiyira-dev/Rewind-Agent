"""Loopback fixture for the Responses subset. Not a live model.

The script is synthetic test data. The agent process is not given this file,
the fault plan, or the request log.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


class _Server(ThreadingHTTPServer):
    def __init__(self, store: Path, script: dict[str, Any]) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        try:
            self.store = store
            self.script = script
            self.model = str(script["model"])
            self.responses = list(script["responses"])
            self.fault: dict[str, Any] | None = None
            self.lock = threading.Lock()
            self._init()
        except BaseException:
            self.server_close()
            raise

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
        except BaseException:
            conn.close()
            raise
        return conn

    def _init(self) -> None:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS protocol_log (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    method TEXT NOT NULL,
                    path TEXT NOT NULL,
                    output_count INTEGER,
                    call_ids TEXT,
                    auth_ok INTEGER NOT NULL
                )
                """
            )
            conn.execute("COMMIT")
        finally:
            conn.close()


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _send(self, status: int, payload: dict[str, Any] | None, *, raw: bytes | None = None) -> None:
        body = raw if raw is not None else json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/v1/admin/state":
            self._send(404, {"error": {"message": "not found"}})
            return
        conn = self.server.connect()
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM protocol_log WHERE path='/v1/responses'"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT output_count, call_ids FROM protocol_log WHERE path='/v1/responses' ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
        self._send(
            200,
            {
                "requests": count,
                "turns": [{"output_count": row[0], "call_ids": json.loads(row[1] or "[]")} for row in rows],
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b""
        if self.path == "/v1/admin/fault":
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send(400, {"error": {"message": "malformed"}})
                return
            with self.server.lock:
                self.server.fault = payload
            self._send(200, {"ok": True})
            return
        if self.path != "/v1/responses":
            self._send(404, {"error": {"message": "not found"}})
            return
        with self.server.lock:
            fault = self.server.fault
            self.server.fault = None
        if fault and fault.get("mode") == "timeout":
            time.sleep(float(fault.get("sleep_s", 30)))
        if fault and fault.get("mode") == "close":
            self.connection.close()
            return
        if fault and fault.get("mode") == "http_500":
            self._record(0, [], self._auth_ok())
            self._send(500, {"error": {"message": "fixture http 500"}})
            return
        if fault and fault.get("mode") == "malformed":
            self._record(0, [], self._auth_ok())
            self._send(200, None, raw=b"not-json")
            return
        if fault and fault.get("mode") == "redirect":
            self._record(0, [], self._auth_ok())
            self.send_response(302)
            self.send_header("Location", "https://api.openai.com/v1/responses")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send(400, {"error": {"message": "malformed request"}})
            return
        if not isinstance(request, dict):
            self._send(400, {"error": {"message": "malformed request"}})
            return
        problem = _check_request(request, self.server.model)
        items = request.get("input") if isinstance(request.get("input"), list) else []
        output_count = sum(1 for item in items if isinstance(item, dict) and item.get("type") == "function_call_output")
        call_ids = [
            item.get("call_id")
            for item in items
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        self._record(output_count, call_ids, self._auth_ok())
        if problem:
            self._send(400, {"error": {"message": problem}})
            return
        if fault and fault.get("mode") == "replace":
            response = _envelope(request, fault["output"], status=fault.get("status", "completed"), error=fault.get("error"))
            self._send(200, response)
            return
        if output_count >= len(self.server.responses):
            self._send(400, {"error": {"message": "fixture has no further response"}})
            return
        step = self.server.responses[output_count]
        response = _envelope(
            request,
            step.get("output", []),
            status=step.get("status", "completed"),
            error=step.get("error"),
        )
        self._send(200, response)

    def _auth_ok(self) -> int:
        return 1 if self.headers.get("Authorization") == "Bearer rewind-local-test-credential" else 0

    def _record(self, output_count: int, call_ids: list[Any], auth_ok: int) -> None:
        conn = self.server.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO protocol_log (method, path, output_count, call_ids, auth_ok)
                VALUES ('POST', '/v1/responses', ?, ?, ?)
                """,
                (output_count, json.dumps(call_ids), auth_ok),
            )
            conn.execute("COMMIT")
        finally:
            conn.close()


def _check_request(request: dict[str, Any], model: str) -> str | None:
    if request.get("model") != model:
        return "model mismatch"
    if request.get("stream") is True:
        return "stream is outside this fixture"
    if request.get("parallel_tool_calls") is not False:
        return "parallel_tool_calls must be false"
    if request.get("store") is not False:
        return "store must be false for local continuation"
    if request.get("include") != ["reasoning.encrypted_content"]:
        return "opaque reasoning continuation must be requested"
    if request.get("previous_response_id"):
        return "previous_response_id is not the local continuation source"
    items = request.get("input")
    if not isinstance(items, list):
        return "input must be a list"
    pending: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            return "input item"
        if item.get("type") == "function_call":
            pending.append(str(item.get("call_id")))
        if item.get("type") == "function_call_output":
            call_id = str(item.get("call_id"))
            if call_id not in pending:
                return "function_call_output without the preceding function_call"
            pending.remove(call_id)
    if pending:
        return "function_call missing function_call_output"
    return None


def _envelope(request: dict[str, Any], output: list[Any], *, status: str, error: dict[str, Any] | None) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": f"resp_fixture_{int(time.time() * 1000)}",
        "object": "response",
        "created_at": int(time.time()),
        "model": request.get("model"),
        "status": status,
        "parallel_tool_calls": False,
        "tool_choice": request.get("tool_choice", "auto"),
        "tools": request.get("tools", []),
        "output": output,
    }
    if error is not None:
        body["error"] = error
    if status == "incomplete":
        body["incomplete_details"] = {"reason": "max_output_tokens"}
    return body


def serve(store: Path, fixture: Path, ready: Path) -> None:
    script = json.loads(fixture.read_text(encoding="utf-8"))
    if script.get("source") != "synthetic_fixture_not_a_live_model":
        raise SystemExit("fixture source")
    server = _Server(store, script)
    try:
        host, port = server.server_address[:2]
        ready.write_text(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "base_url": f"http://{host}:{port}/v1",
                    "source": "synthetic_fixture_not_a_live_model",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        server.serve_forever()
    finally:
        server.server_close()
