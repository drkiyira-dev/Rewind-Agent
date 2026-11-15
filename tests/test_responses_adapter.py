"""Responses subset checks. HTTP cases use the official SDK against loopback."""

from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit
import http.client

from rewindagent.adapters.openai_responses import LOCAL_TEST_CREDENTIAL, ResponsesConfig, parse_arguments
from rewindagent.agent import start_agent

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SUMMARY_REF = "24f569712aa65958725b0ac9e1d09829554c37511f5125dfb6807c50c9dfc518"


def _sdk_ready() -> bool:
    try:
        import httpx2
        import openai
    except ImportError:
        return False
    return bool(openai) and bool(httpx2)


def _call(call_id: str, name: str, arguments: str = "{}") -> dict:
    return {
        "type": "function_call",
        "id": "fc_" + call_id,
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
        "status": "completed",
    }


def _message(text: str) -> dict:
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


class ResponseShapeTests(unittest.TestCase):
    def test_duplicate_and_extra_arguments_are_rejected(self) -> None:
        schema = {
            "type": "object",
            "properties": {"summary_ref": {"type": "string", "maxLength": 80}},
            "required": ["summary_ref"],
            "additionalProperties": False,
        }
        self.assertEqual(parse_arguments('{"summary_ref":"abc","summary_ref":"def"}', schema), "invalid_arguments")
        self.assertEqual(parse_arguments('{"summary_ref":"abc","path":"/tmp/x"}', schema), "invalid_arguments")
        self.assertEqual(parse_arguments('{"summary_ref":true}', schema), "invalid_arguments")
        parsed = parse_arguments('{"summary_ref":"abc"}', schema)
        self.assertEqual(parsed, {"summary_ref": "abc"})

    def test_live_mode_does_not_open_a_provider_socket(self) -> None:
        work = Path(self._tmp())
        socket_guard = patch("socket.socket", side_effect=AssertionError("unexpected socket"))
        socket_guard.start()
        self.addCleanup(socket_guard.stop)
        result = start_agent(
            work / "agent.sqlite",
            "live",
            "task",
            {"records": []},
            _tools(),
            ResponsesConfig(
                mode="live",
                model="gpt-4.1",
                base_url="https://api.openai.com/v1",
                live_authorized=True,
                api_key="sk-should-not-be-read",
            ),
            instructions="do not call",
            effect_slot="primary_record",
        )
        self.assertEqual(result.reason_code, "live_not_authorized")
        self.assertFalse(result.published)
        self.assertEqual(result.evidence["actual_provider_api_calls"], 0)
        self.assertFalse((work / "agent.sqlite").exists())

    def _tmp(self) -> str:
        import tempfile

        path = tempfile.mkdtemp(prefix="rewind-adapter-")
        self.addCleanup(shutil.rmtree, path, True)
        return path


class SdkProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _sdk_ready():
            self.skipTest("optional responses extra is not installed")
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="rewind-sdk-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.procs: list[subprocess.Popen] = []

    def tearDown(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout is not None and not proc.stdout.closed:
                proc.communicate(timeout=10)
            else:
                proc.wait(timeout=10)

    def test_sdk_constructor_failure_closes_http_client(self) -> None:
        from rewindagent.adapters.openai_responses import create_response

        with patch("httpx2.Client") as http_client, patch("openai.OpenAI", side_effect=ValueError("fixture")):
            result = create_response(
                ResponsesConfig("local_protocol_fixture", "fixture-responses-1", "http://127.0.0.1:1/v1"),
                instructions="local", input_items=[], tools=[],
            )
        self.assertEqual(result["error"], "transport")
        http_client.return_value.close.assert_called_once()

    def test_sdk_does_not_retry_or_follow_redirects_or_read_env_key(self) -> None:
        fixture = self.root / "fixture.json"
        _write_fixture(fixture, [[_message("unused")]])
        meta, effect = self._serve_pair(fixture, self.root)
        self._fault(meta["base_url"], {"mode": "http_500"})
        env = os.environ.copy()
        env["OPENAI_API_KEY"] = "sk-should-not-be-read"
        env["HTTP_PROXY"] = "http://127.0.0.1:9"
        env["HTTPS_PROXY"] = "http://127.0.0.1:9"
        result = self._run(meta, effect=effect, env=env, extra=["--max-model-attempts", "2"])
        self.assertEqual(result["reason_code"], "model_request_unknown")
        self.assertEqual(_protocol_count(self.root / "proto.sqlite"), 1)
        self.assertEqual(_auth_failures(self.root / "proto.sqlite"), 0)
        self._fault(meta["base_url"], {"mode": "redirect"})
        again = self._run(meta, effect=effect, env=env, command="retry-model")
        self.assertEqual(again["reason_code"], "model_request_unknown")
        self.assertEqual(_protocol_count(self.root / "proto.sqlite"), 2)

    def test_unsupported_responses_do_not_publish_or_post(self) -> None:
        cases = [
            ("unknown_tool", {"output": [_call("call_x", "shell")], "status": "completed"}, "unknown_tool"),
            ("bad_args", {"output": [_call("call_x", "read_snapshot", '{"path":"/tmp/x"}')], "status": "completed"}, "invalid_arguments"),
            ("multi", {"output": [_call("call_a", "read_snapshot"), _call("call_b", "summarize")], "status": "completed"}, "multiple_function_calls"),
            ("custom", {"output": [{"type": "custom_tool_call", "call_id": "call_x", "name": "shell", "input": "ls"}], "status": "completed"}, "unsupported_output"),
            ("incomplete", {"output": [], "status": "incomplete"}, "model_incomplete"),
            ("failed", {"output": [], "status": "failed", "error": {"code": "server_error", "message": "fixture"}}, "model_failed"),
            ("refusal", {"output": [{"type": "message", "id": "msg_r", "role": "assistant", "status": "completed", "content": [{"type": "refusal", "refusal": "no"}]}], "status": "completed"}, "model_refusal"),
            ("text_only", {"output": [_message("The summary was recorded once.")], "status": "completed"}, "business_incomplete"),
        ]
        for name, payload, reason in cases:
            with self.subTest(name=name):
                folder = self.root / name
                folder.mkdir()
                fixture = folder / "fixture.json"
                _write_fixture(fixture, [[_message("placeholder")]])
                meta, effect = self._serve_pair(fixture, folder)
                self._fault(meta["base_url"], {"mode": "replace", **payload})
                result = self._run(meta, effect=effect, store=folder / "agent.sqlite")
                self.assertEqual(result["reason_code"], reason)
                self.assertFalse(result["published"])
                self.assertEqual(_effect_score(folder / "effect.sqlite"), {"effects": 0, "posts": 0})

    def _serve(self, fixture: Path) -> dict:
        meta, _effect = self._serve_pair(fixture, self.root)
        return meta

    def _serve_pair(self, fixture: Path, folder: Path) -> tuple[dict, dict]:
        proto_ready = folder / "proto.ready.json"
        effect_ready = folder / "effect.ready.json"
        proto = self._popen(
            [
                PY, "-m", "rewindagent", "protocol-serve",
                "--fixture", str(fixture),
                "--store", str(folder / "proto.sqlite"),
                "--ready", str(proto_ready),
            ]
        )
        effect = self._popen(
            [
                PY, "-m", "rewindagent", "effect-serve",
                "--store", str(folder / "effect.sqlite"),
                "--profile", "P0",
                "--ready", str(effect_ready),
                "--retention-ms", "60000",
            ]
        )
        return _wait_ready(proto, proto_ready), _wait_ready(effect, effect_ready)

    def _popen(self, cmd: list[str]) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        return proc

    def _run(self, meta: dict, *, env: dict | None = None, extra: list[str] | None = None, command: str = "start", effect: dict | None = None, store: Path | None = None) -> dict:
        if effect is None:
            raise AssertionError("effect")
        cmd = [
            PY, str(ROOT / "examples" / "responses_agent.py"), command,
            "--store", str(store or (self.root / "agent.sqlite")),
            "--run-id", "demo",
            "--snapshot", str(ROOT / "examples" / "data" / "agent_snapshot.json"),
            "--protocol-url", meta["base_url"],
            "--effect-url", effect["base_url"],
            *(extra or []),
        ]
        run_env = os.environ.copy()
        run_env["PYTHONPATH"] = str(ROOT)
        if env:
            run_env.update(env)
        proc = subprocess.run(cmd, cwd="/tmp", env=run_env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
        self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
        return json.loads(proc.stdout)

    def _fault(self, base: str, payload: dict) -> None:
        _http(base, "POST", "/v1/admin/fault", payload)


def _tools():
    from rewindagent.agent import AgentTool
    from rewindagent.effectsvc import SERVICE_ID

    empty = {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    record = {
        "type": "object",
        "properties": {"summary_ref": {"type": "string", "maxLength": 80}},
        "required": ["summary_ref"],
        "additionalProperties": False,
    }
    return [
        AgentTool("read_snapshot", "read", "pure", "agent.read.v1", empty, lambda values: (values[0],)),
        AgentTool("summarize", "sum", "pure", "agent.summarize.v1", empty, lambda values: ({"count": 0, "total": 0},)),
        AgentTool("record_summary", "record", "effect", "agent.record.v1", record, None, SERVICE_ID),
    ]


def _write_fixture(path: Path, steps: list[list[dict]]) -> None:
    path.write_text(
        json.dumps(
            {
                "source": "synthetic_fixture_not_a_live_model",
                "model": "fixture-responses-1",
                "responses": [{"output": step} for step in steps],
            }
        ),
        encoding="utf-8",
    )


def _wait_ready(proc: subprocess.Popen, ready: Path) -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if ready.exists():
            meta = json.loads(ready.read_text(encoding="utf-8"))
            if meta.get("pid") == proc.pid:
                return meta
        if proc.poll() is not None:
            err = proc.stderr.read().decode()[:800] if proc.stderr else ""
            raise AssertionError(err)
        time.sleep(0.02)
    proc.kill()
    raise AssertionError("ready timeout")


def _http(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    parts = urlsplit(base if "://" in base else "http://" + base)
    root = base[:-3] if base.endswith("/v1") else base
    parts = urlsplit(root if root.startswith("http") else base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    body = None if payload is None else json.dumps(payload).encode()
    target = path
    conn.request(method, target, body=body, headers={"Content-Type": "application/json"})
    raw = conn.getresponse().read()
    conn.close()
    return json.loads(raw.decode())


def _protocol_count(store: Path) -> int:
    conn = sqlite3.connect(store)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM protocol_log WHERE path='/v1/responses'").fetchone()[0])
    finally:
        conn.close()


def _auth_failures(store: Path) -> int:
    conn = sqlite3.connect(store)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM protocol_log WHERE auth_ok=0").fetchone()[0])
    finally:
        conn.close()


def _effect_score(store: Path) -> dict[str, int]:
    conn = sqlite3.connect(store)
    try:
        effects = int(conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0])
        posts = int(conn.execute("SELECT COUNT(*) FROM http_log WHERE path='/v1/effects'").fetchone()[0])
    finally:
        conn.close()
    return {"effects": effects, "posts": posts}



if __name__ == "__main__":
    unittest.main()
