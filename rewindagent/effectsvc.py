"""Loopback append service. The client class cannot see the service database."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .canonical import semantic_hash

SERVICE_ID = "local-append"
CONTRACT_VERSION = "append.v1"
PROFILES = {
    "P0": {"idempotent": True, "query": True},
    "P1": {"idempotent": True, "query": False},
    "P2": {"idempotent": False, "query": True},
    "P3": {"idempotent": False, "query": False},
}


def _canon(value: Any) -> str:
    semantic_hash(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class AppendClient:
    """Public adapter. It only calls capabilities, submit, and query."""

    def __init__(self, base_url: str, *, timeout_s: float = 30) -> None:
        parts = urlsplit(base_url)
        if parts.hostname not in {"127.0.0.1", "localhost"} or not parts.port:
            raise ValueError("service url must be loopback with a port")
        self.base_url = f"http://{parts.hostname}:{parts.port}"
        self.host = parts.hostname
        self.port = parts.port
        self.timeout_s = timeout_s
        self.service_id = SERVICE_ID
        self.contract_version = CONTRACT_VERSION

    def capabilities(self) -> dict[str, Any]:
        return self._call("GET", "/v1/capabilities")

    def submit(self, request: dict[str, Any]) -> dict[str, Any]:
        return self._call("POST", "/v1/effects", request)

    def query(self, idempotency_key: str) -> dict[str, Any]:
        return self._call("GET", "/v1/effects/" + idempotency_key)

    def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        import http.client

        body = None if payload is None else _canon(payload).encode("utf-8")
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)
        try:
            conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            raw = response.read()
            if response.status in {301, 302, 303, 307, 308}:
                return {"status": "unavailable", "reason": "redirect"}
            if not raw:
                return {"status": "unavailable", "reason": f"http_{response.status}"}
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                return {"status": "unavailable", "reason": "malformed"}
            if not isinstance(parsed, dict):
                return {"status": "unavailable", "reason": "malformed"}
            return parsed
        except (TimeoutError, OSError, http.client.HTTPException):
            return {"status": "unavailable", "reason": "transport"}
        finally:
            conn.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, store: Path, profile: str, retention_ms: int) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.store = store
        self.profile = PROFILES[profile]
        self.profile_name = profile
        self.retention_ms = retention_ms
        self.pause_at: str | None = None
        self.phase: str | None = None
        self.query_mode = "normal"
        self.gate = threading.Event()
        self.gate.set()
        self._init()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.store, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init(self) -> None:
        conn = self.connect()
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS effects (
                effect_id TEXT PRIMARY KEY,
                idempotency_key TEXT,
                logical_id TEXT NOT NULL,
                payload_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_ms INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS http_log (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                method TEXT NOT NULL,
                path TEXT NOT NULL,
                idempotency_key TEXT
            )
            """
        )
        if self.profile["idempotent"]:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS effects_key ON effects(idempotency_key)"
            )
        conn.execute("COMMIT")
        conn.close()

    def wait_if(self, point: str) -> None:
        if self.pause_at == point:
            self.phase = point
            self.gate.wait(60)
            self.phase = None


class _Handler(BaseHTTPRequestHandler):
    server: _Server

    def log_message(self, fmt: str, *args: object) -> None:
        del fmt, args

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        raw = _canon(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:
        if self.path == "/v1/admin/state":
            self._send(200, {"phase": self.server.phase, "query_mode": self.server.query_mode})
            return
        if self.path == "/v1/capabilities":
            self._send(
                200,
                {
                    "service_id": SERVICE_ID,
                    "contract_version": CONTRACT_VERSION,
                    "profile": self.server.profile_name,
                    "idempotent": self.server.profile["idempotent"],
                    "query": self.server.profile["query"],
                    "retention_ms": self.server.retention_ms,
                },
            )
            return
        if not self.path.startswith("/v1/effects/"):
            self._send(404, {"status": "unavailable", "reason": "path"})
            return
        if not self.server.profile["query"] or self.server.query_mode == "unavailable":
            self._send(503, {"status": "unavailable"})
            return
        key = unquote(self.path.removeprefix("/v1/effects/"))
        conn = self.server.connect()
        try:
            row = conn.execute(
                "SELECT * FROM effects WHERE idempotency_key=? ORDER BY created_ms",
                (key,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            self._send(200, {"status": "not_found", "idempotency_key": key})
            return
        logical_id = row["logical_id"]
        digest = row["payload_digest"]
        if self.server.query_mode == "lie_payload":
            digest = semantic_hash({"values": ["lie"]})
        if self.server.query_mode == "lie_id":
            logical_id = "other-operation"
        self._send(
            200,
            {
                "status": "confirmed",
                "idempotency_key": key,
                "logical_id": logical_id,
                "payload_digest": digest,
                "effect_id": row["effect_id"],
            },
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            request = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send(400, {"status": "unavailable", "reason": "malformed"})
            return
        if self.path == "/v1/admin/fault":
            self._admin(request)
            return
        if self.path != "/v1/effects":
            self._send(404, {"status": "unavailable", "reason": "path"})
            return
        key = str(request.get("idempotency_key", ""))
        conn = self.server.connect()
        conn.execute(
            "INSERT INTO http_log (method, path, idempotency_key) VALUES ('POST', '/v1/effects', ?)",
            (key,),
        )
        conn.close()
        self.server.wait_if("received")
        if semantic_hash(request.get("payload")) != request.get("payload_digest"):
            self._send(400, {"status": "unavailable", "reason": "digest"})
            return
        outcome = self._apply(request)
        self.server.wait_if("after_commit")
        self._send(200 if outcome["status"] != "conflict" else 409, outcome)

    def _admin(self, request: dict[str, Any]) -> None:
        if request.get("release"):
            self.server.gate.set()
        if "pause" in request:
            self.server.pause_at = request["pause"]
            self.server.gate.clear() if request["pause"] else self.server.gate.set()
        if "query_mode" in request:
            self.server.query_mode = str(request["query_mode"])
        self._send(200, {"status": "ok", "pause": self.server.pause_at, "query_mode": self.server.query_mode})

    def _apply(self, request: dict[str, Any]) -> dict[str, Any]:
        key = str(request["idempotency_key"])
        logical_id = str(request["logical_id"])
        digest = str(request["payload_digest"])
        payload = request["payload"]
        conn = self.server.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            if self.server.profile["idempotent"]:
                existing = conn.execute(
                    "SELECT * FROM effects WHERE idempotency_key=?",
                    (key,),
                ).fetchone()
                if existing is not None:
                    conn.execute("COMMIT")
                    if existing["payload_digest"] != digest or existing["logical_id"] != logical_id:
                        return {
                            "status": "conflict",
                            "idempotency_key": key,
                            "logical_id": existing["logical_id"],
                            "payload_digest": existing["payload_digest"],
                        }
                    return {
                        "status": "confirmed",
                        "idempotency_key": key,
                        "logical_id": existing["logical_id"],
                        "payload_digest": existing["payload_digest"],
                        "effect_id": existing["effect_id"],
                        "replay": True,
                    }
            effect_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO effects (
                    effect_id, idempotency_key, logical_id, payload_digest, payload_json, created_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (effect_id, key, logical_id, digest, _canon(payload), 0),
            )
            self.server.wait_if("before_commit")
            conn.execute("COMMIT")
            return {
                "status": "confirmed",
                "idempotency_key": key,
                "logical_id": logical_id,
                "payload_digest": digest,
                "effect_id": effect_id,
                "replay": False,
            }
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()


def serve(store: Path, profile: str, retention_ms: int, ready: Path | None) -> None:
    if profile not in PROFILES:
        raise SystemExit(2)
    server = _Server(store, profile, retention_ms)
    host, port = server.server_address[:2]
    if ready is not None:
        ready.write_text(
            json.dumps({"pid": __import__("os").getpid(), "base_url": f"http://{host}:{port}"}) + "\n",
            encoding="utf-8",
        )
    server.serve_forever()
