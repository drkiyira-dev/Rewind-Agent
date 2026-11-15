"""Transport errors are not effect confirmations, even with matching JSON fields."""

import gc
import json
import sqlite3
import tempfile
import unittest
import warnings
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from rewindagent.durable import start_run
from rewindagent.effectsvc import AppendClient, SERVICE_ID, _Server, serve


class Response:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    def read(self):
        return self.body


class Connection:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.requests = []

    def request(self, method, path, body=None, headers=None):
        self.requests.append((method, path, body))

    def getresponse(self):
        return self.response

    def close(self):
        self.closed = True


class AppendClientTest(unittest.TestCase):
    def call(self, status, body, method="GET", path="/v1/effects/key"):
        conn = Connection(Response(status, body))
        with patch("http.client.HTTPConnection", return_value=conn):
            result = AppendClient("http://127.0.0.1:1")._call(method, path)
        self.assertTrue(conn.closed)
        self.assertEqual(len(conn.requests), 1)
        return result

    def test_http_errors_cannot_confirm_an_effect(self):
        body = b'{"status":"confirmed"}'
        for status in (400, 401, 404, 409, 429, 500, 503):
            for method in ("GET", "POST"):
                with self.subTest(status=status, method=method):
                    result = self.call(status, body, method, "/v1/effects")
                    self.assertEqual(result, {"status": "unavailable", "reason": f"http_{status}"})

    def test_success_and_submit_conflict_are_preserved(self):
        self.assertEqual(self.call(200, b'{"status":"confirmed"}'), {"status": "confirmed"})
        conflict = b'{"status":"conflict","logical_id":"record"}'
        self.assertEqual(
            self.call(409, conflict, "POST", "/v1/effects"),
            {"status": "conflict", "logical_id": "record"},
        )
        self.assertEqual(self.call(409, conflict)["status"], "unavailable")
        self.assertEqual(self.call(409, conflict, "GET", "/v1/capabilities")["status"], "unavailable")

    def test_malformed_response_is_unavailable_and_connection_is_closed(self):
        for body in (b"\xff", b"{", b"[]", b"null"):
            with self.subTest(body=body):
                self.assertEqual(self.call(200, body), {"status": "unavailable", "reason": "malformed"})

    def test_redirect_is_unavailable(self):
        self.assertEqual(
            self.call(302, b'{"status":"confirmed"}'),
            {"status": "unavailable", "reason": "redirect"},
        )

    def test_transport_exception_closes_connection(self):
        conn = Connection(None)
        with patch("http.client.HTTPConnection", return_value=conn), patch.object(
            conn, "getresponse", side_effect=TimeoutError
        ):
            result = AppendClient("http://127.0.0.1:1").capabilities()
        self.assertEqual(result, {"status": "unavailable", "reason": "transport"})
        self.assertTrue(conn.closed)

    def test_error_receipt_cannot_publish_durable_run(self):
        workflow = {
            "schema_version": 1,
            "steps": [{"step_id": "append", "kind": "effect", "logical_id": "record",
                       "service_id": SERVICE_ID, "reads": ["n"], "writes": ["ack"], "config": {}}],
        }
        caps = {"service_id": SERVICE_ID, "contract_version": "append.v1",
                "idempotent": False, "query": False, "retention_ms": 1000}
        for status, response_kind, expected in (
            (500, "confirmed", "unknown"),
            (409, "confirmed", "unknown"),
            (200, "invalid_utf8", "unknown"),
            (409, "conflict", "safe_refusal"),
            (200, "confirmed", "published"),
        ):
            with self.subTest(status=status, response_kind=response_kind), tempfile.TemporaryDirectory() as tmp:
                connections = []
                posts = []

                class WorkflowConnection(Connection):
                    def __init__(self, *args, **kwargs):
                        super().__init__(None)
                        connections.append(self)

                    def getresponse(self):
                        method, path, raw = self.requests[-1]
                        if path == "/v1/capabilities":
                            return Response(200, json.dumps(caps).encode())
                        self_test.assertEqual((method, path), ("POST", "/v1/effects"))
                        request = json.loads(raw)
                        posts.append(request)
                        body = {"status": response_kind, **{key: request[key] for key in
                                ("idempotency_key", "logical_id", "payload_digest")}}
                        return Response(status, b"\xff" if response_kind == "invalid_utf8" else json.dumps(body).encode())

                self_test = self
                with patch("http.client.HTTPConnection", WorkflowConnection):
                    result = start_run(
                        Path(tmp) / "task.sqlite", "run", workflow, {"n": 2}, [],
                        services={SERVICE_ID: AppendClient("http://127.0.0.1:1")},
                    )
                self.assertEqual(result.status, expected)
                self.assertEqual(result.published, expected == "published")
                self.assertEqual(len(posts), 1)
                self.assertTrue(all(conn.closed for conn in connections))
                with closing(sqlite3.connect(Path(tmp) / "task.sqlite")) as conn:
                    effect_state, attempts = conn.execute("SELECT state, attempt_count FROM effect_ops").fetchone()
                self.assertEqual(attempts, 1)
                self.assertEqual(effect_state, {"unknown": "unknown", "safe_refusal": "rejected",
                                                "published": "confirmed"}[expected])


class ServiceCleanupTest(unittest.TestCase):
    def assert_initialization_failure_closes_resources(self, store):
        before = store.read_bytes()
        connect = sqlite3.connect
        server_close = _Server.server_close
        opened = []
        closed_servers = []

        def capture_connection(*args, **kwargs):
            conn = connect(*args, **kwargs)
            opened.append(conn)
            return conn

        def capture_close(server):
            closed_servers.append(server)
            server_close(server)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", ResourceWarning)
            with patch("rewindagent.effectsvc.sqlite3.connect", capture_connection), patch.object(
                _Server, "server_close", capture_close
            ):
                with self.assertRaises(sqlite3.DatabaseError):
                    _Server(store, "P0", 1000)
            try:
                self.assertEqual(len(closed_servers), 1)
                self.assertEqual(closed_servers[0].socket.fileno(), -1)
                self.assertEqual(len(opened), 1)
                with self.assertRaises(sqlite3.ProgrammingError):
                    opened[0].execute("SELECT 1")
            finally:
                for conn in opened:
                    conn.close()
                for server in closed_servers:
                    server_close(server)
                opened.clear()
                closed_servers.clear()
            gc.collect()
            self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])
        self.assertEqual(store.read_bytes(), before)

    def test_corrupt_store_closes_bound_socket_and_failed_connection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "corrupt.sqlite"
            store.write_bytes(b"not a sqlite database")
            self.assert_initialization_failure_closes_resources(store)

    def test_invalid_schema_closes_bound_socket_and_rolls_back_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / "invalid-schema.sqlite"
            with closing(sqlite3.connect(store)) as conn:
                conn.execute("CREATE TABLE effects (wrong_column TEXT)")
                conn.commit()
            self.assert_initialization_failure_closes_resources(store)
            with closing(sqlite3.connect(store)) as conn:
                self.assertEqual(
                    conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(),
                    [("effects",)],
                )

    def test_serve_closes_socket_on_ready_failure_return_and_interrupt(self):
        server_close = _Server.server_close
        for mode in ("ready_failure", "return", "interrupt"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                closed_servers = []

                def capture_close(server):
                    closed_servers.append(server)
                    server_close(server)

                ready = root / "missing" / "ready.json" if mode == "ready_failure" else None
                with warnings.catch_warnings(record=True) as caught:
                    warnings.simplefilter("always", ResourceWarning)
                    with patch.object(_Server, "server_close", capture_close), patch.object(
                        _Server, "serve_forever", side_effect=KeyboardInterrupt if mode == "interrupt" else None
                    ) as loop:
                        if mode == "ready_failure":
                            with self.assertRaises(FileNotFoundError):
                                serve(root / "service.sqlite", "P0", 1000, ready)
                            loop.assert_not_called()
                        elif mode == "interrupt":
                            with self.assertRaises(KeyboardInterrupt):
                                serve(root / "service.sqlite", "P0", 1000, ready)
                        else:
                            serve(root / "service.sqlite", "P0", 1000, ready)
                    try:
                        self.assertEqual(len(closed_servers), 1)
                        self.assertEqual(closed_servers[0].socket.fileno(), -1)
                    finally:
                        for server in closed_servers:
                            server_close(server)
                        closed_servers.clear()
                    gc.collect()
                    self.assertFalse([w for w in caught if issubclass(w.category, ResourceWarning)])
