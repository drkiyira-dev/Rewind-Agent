"""Cross-process agent recovery. The official SDK talks only to the local fixture."""

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
from pathlib import Path
from urllib.parse import urlsplit
import http.client

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SUMMARY_REF = "24f569712aa65958725b0ac9e1d09829554c37511f5125dfb6807c50c9dfc518"
SNAPSHOT = ROOT / "examples" / "data" / "agent_snapshot.json"
STANDARD = ROOT / "examples" / "data" / "agent_fixture.json"


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
        "id": "msg_final",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


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


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        if not _sdk_ready():
            self.skipTest("optional responses extra is not installed")
        import tempfile

        self.root = Path(tempfile.mkdtemp(prefix="rewind-agent-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._reap)

    def test_normal_multiturn_publishes_one_effect(self) -> None:
        meta, effect = self._servers(STANDARD)
        result = self._finish(self._cmd("start", meta, effect))
        self.assertTrue(result["published"])
        self.assertEqual(result["values"]["summary"], {"count": 2, "total": 7})
        self.assertEqual(result["values"]["summary_ref"], SUMMARY_REF)
        self.assertEqual(_score(self.root / "effect.sqlite"), {"effects": 1, "posts": 1})
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(result["evidence"]["actual_provider_api_calls"], 0)
        self.assertEqual(result["evidence"]["actual_token_usage"], 0)
        self.assertEqual(result["evidence"]["provider_mode"], "local_protocol_fixture")
        self.assertEqual(result["evidence"]["live_model_validation"], "NOT_RUN_NOT_AUTHORIZED")
        again = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(again["publication_id"], result["publication_id"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 1)
        inspected = self._finish(self._cmd("inspect", meta, effect))
        self.assertNotIn("fixture-opaque-not-a-secret", json.dumps(inspected))

    def test_uncommitted_response_is_not_executed_or_retried(self) -> None:
        meta, effect = self._servers(STANDARD)
        killed = self._crash(self._cmd("start", meta, effect, "--stop-at", "response_uncommitted", "--ready", str(self.root / "ready.json")), self.root / "ready.json", "response_uncommitted")
        self.assertEqual(killed["returncode"], -signal.SIGKILL)
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 0)
        self.assertEqual(_call_count(self.root / "agent.sqlite"), 0)
        unknown = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(unknown["reason_code"], "model_request_unknown")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        retried = self._crash(
            self._cmd("retry-model", meta, effect, "--stop-at", "response_uncommitted", "--ready", str(self.root / "ready2.json")),
            self.root / "ready2.json",
            "response_uncommitted",
        )
        self.assertEqual(retried["returncode"], -signal.SIGKILL)
        self.assertEqual(_attempts(self.root / "agent.sqlite"), 2)
        self.assertEqual(_call_count(self.root / "agent.sqlite"), 0)
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 2)

    def test_accepted_call_resumes_without_a_new_model_request(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        killed = self._crash(self._cmd("start", meta, effect, "--stop-at", "call_accepted", "--ready", str(ready)), ready, "call_accepted")
        self.assertEqual(killed["returncode"], -signal.SIGKILL)
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        call = _calls(self.root / "agent.sqlite")[0]
        self.assertEqual(call["call_id"], "call_read")
        child = call["child_run_id"]
        self._crash(
            self._cmd("resume", meta, effect, "--stop-at", "output_committed", "--ready", str(self.root / "ready-b.json")),
            self.root / "ready-b.json",
            "output_committed",
        )
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        again = _calls(self.root / "agent.sqlite")[0]
        self.assertEqual(again["call_id"], "call_read")
        self.assertEqual(again["child_run_id"], child)
        self.assertEqual(again["adopted"], 1)

    def test_effect_receipt_loss_blocks_the_next_model_turn(self) -> None:
        meta, effect = self._servers(STANDARD)
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"pause": "after_commit"})
        proc = self._popen(self._cmd("start", meta, effect), cwd="/tmp")
        self._wait_phase(effect["base_url"], "after_commit")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 3)
        self.assertEqual(_score(self.root / "effect.sqlite"), {"effects": 1, "posts": 1})
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"query_mode": "unavailable"})
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"release": True, "pause": None})
        unknown = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(unknown["reason_code"], "effect_unknown")
        self.assertFalse(unknown["published"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 3)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 1)

    def test_child_result_is_adopted_once(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        self._crash(self._cmd("start", meta, effect, "--stop-at", "child_done", "--ready", str(ready)), ready, "child_done")
        self.assertEqual(_calls(self.root / "agent.sqlite")[0]["adopted"], 0)
        self._crash(
            self._cmd("resume", meta, effect, "--stop-at", "output_committed", "--ready", str(self.root / "ready-b.json")),
            self.root / "ready-b.json",
            "output_committed",
        )
        rows = _items(self.root / "agent.sqlite", "function_call_output")
        self.assertEqual(len(rows), 1)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 0)

    def test_saved_tool_output_resumes_at_the_next_model_request(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        self._crash(self._cmd("start", meta, effect, "--stop-at", "output_committed", "--ready", str(ready)), ready, "output_committed")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        done = self._finish(self._cmd("resume", meta, effect))
        self.assertTrue(done["published"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(_score(self.root / "effect.sqlite"), {"effects": 1, "posts": 1})
        self.assertEqual(len(_calls(self.root / "agent.sqlite")), 3)

    def test_saved_final_response_publishes_without_another_model_call(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        killed = self._crash(self._cmd("start", meta, effect, "--stop-at", "final_saved", "--ready", str(ready)), ready, "final_saved")
        self.assertEqual(killed["stdout"], "")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(_score(self.root / "effect.sqlite")["effects"], 1)
        published = self._finish(self._cmd("resume", meta, effect))
        self.assertTrue(published["published"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 1)

    def test_published_resume_keeps_the_publication_id(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        killed = self._crash(self._cmd("start", meta, effect, "--stop-at", "published", "--ready", str(ready)), ready, "published")
        self.assertEqual(killed["stdout"], "")
        status = self._finish(self._cmd("status", meta, effect))
        publication = status["publication_id"]
        self.assertTrue(status["published"])
        again = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(again["publication_id"], publication)
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 4)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 1)

    def test_same_slot_is_not_recorded_twice(self) -> None:
        repeat = self.root / "repeat.json"
        _write_fixture(
            repeat,
            [
                [_call("call_read", "read_snapshot")],
                [_call("call_sum", "summarize")],
                [_call("call_record", "record_summary", json.dumps({"summary_ref": SUMMARY_REF}))],
                [_call("call_record_2", "record_summary", json.dumps({"summary_ref": SUMMARY_REF}))],
                [_message("recorded once")],
            ],
        )
        meta, effect = self._servers(repeat)
        done = self._finish(self._cmd("start", meta, effect, "--max-tool-calls", "6", "--max-model-turns", "8"))
        self.assertTrue(done["published"])
        self.assertEqual(_score(self.root / "effect.sqlite"), {"effects": 1, "posts": 1})
        conflict_root = self.root / "conflict"
        conflict_root.mkdir()
        fixture = conflict_root / "fixture.json"
        _write_fixture(
            fixture,
            [
                [_call("call_read", "read_snapshot")],
                [_call("call_sum", "summarize")],
                [_call("call_record", "record_summary", json.dumps({"summary_ref": SUMMARY_REF}))],
                [_call("call_other", "record_summary", json.dumps({"summary_ref": "f" * 64}))],
            ],
        )
        meta2, effect2 = self._servers(fixture, folder=conflict_root)
        refused = self._finish(self._cmd("start", meta2, effect2, "--store", str(conflict_root / "agent.sqlite"), "--max-tool-calls", "6"))
        self.assertEqual(refused["reason_code"], "effect_conflict")
        self.assertFalse(refused["published"])
        self.assertEqual(_score(conflict_root / "effect.sqlite"), {"effects": 1, "posts": 1})

    def test_reconcile_confirms_without_a_model_call_or_post(self) -> None:
        meta, effect = self._servers(STANDARD)
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"pause": "after_commit"})
        proc = self._popen(self._cmd("start", meta, effect), cwd="/tmp")
        self._wait_phase(effect["base_url"], "after_commit")
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"query_mode": "unavailable"})
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"release": True, "pause": None})
        held = _protocol(self.root / "proto.sqlite")
        unknown = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(unknown["reason_code"], "effect_unknown")
        _http(effect["base_url"], "POST", "/v1/admin/fault", {"query_mode": "normal"})
        confirmed = self._finish(self._cmd("reconcile", meta, effect))
        self.assertEqual(confirmed["phase"], "awaiting_model")
        self.assertFalse(confirmed["published"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), held)
        self.assertEqual(_score(self.root / "effect.sqlite"), {"effects": 1, "posts": 1})
        done = self._finish(self._cmd("resume", meta, effect))
        self.assertTrue(done["published"])
        self.assertEqual(_protocol(self.root / "proto.sqlite"), held + 1)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 1)

    def test_budgets_and_identity_survive_restart(self) -> None:
        meta, effect = self._servers(STANDARD)
        stopped = self._finish(self._cmd("start", meta, effect, "--max-model-turns", "1"))
        self.assertEqual(stopped["reason_code"], "budget_exhausted")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 0)
        mismatch = self._finish(self._cmd("resume", meta, effect, "--model", "other-model"))
        self.assertEqual(mismatch["reason_code"], "session_mismatch")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)

    def test_a_second_process_does_not_call_the_model_or_effect(self) -> None:
        meta, effect = self._servers(STANDARD)
        ready = self.root / "ready.json"
        holder = self._popen(self._cmd("start", meta, effect, "--stop-at", "call_accepted", "--ready", str(ready)), cwd="/tmp")
        self._wait_ready_file(holder, ready, "call_accepted")
        before = _file_hash(self.root / "agent.sqlite")
        busy = self._finish(self._cmd("resume", meta, effect))
        self.assertEqual(busy["reason_code"], "executor_busy")
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        self.assertEqual(_score(self.root / "effect.sqlite")["posts"], 0)
        status = self._finish(self._cmd("status", meta, effect))
        self.assertEqual(status["phase"], "tool_pending")
        self.assertEqual(_file_hash(self.root / "agent.sqlite"), before)
        self.assertEqual(_protocol(self.root / "proto.sqlite"), 1)
        os.kill(holder.pid, signal.SIGKILL)
        holder.wait(timeout=5)

    def _servers(self, fixture: Path, folder: Path | None = None) -> tuple[dict, dict]:
        folder = folder or self.root
        proto_ready = folder / "proto.ready.json"
        effect_ready = folder / "effect.ready.json"
        proto = self._popen(
            [PY, "-m", "rewindagent", "protocol-serve", "--fixture", str(fixture), "--store", str(folder / "proto.sqlite"), "--ready", str(proto_ready)]
        )
        effect = self._popen(
            [PY, "-m", "rewindagent", "effect-serve", "--store", str(folder / "effect.sqlite"), "--profile", "P0", "--ready", str(effect_ready), "--retention-ms", "60000"]
        )
        return _wait_ready(proto, proto_ready), _wait_ready(effect, effect_ready)

    def _cmd(self, command: str, meta: dict, effect: dict, *extra: str) -> list[str]:
        store = None
        args = list(extra)
        if "--store" in args:
            store = args[args.index("--store") + 1]
        return [
            PY, str(ROOT / "examples" / "responses_agent.py"), command,
            "--store", store or str(self.root / "agent.sqlite"),
            "--run-id", "demo",
            "--snapshot", str(SNAPSHOT),
            "--protocol-url", meta["base_url"],
            "--effect-url", effect["base_url"],
            *extra,
        ]

    def _popen(self, cmd: list[str], cwd: str = str(ROOT)) -> subprocess.Popen:
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        env.pop("OPENAI_API_KEY", None)
        proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.procs.append(proc)
        return proc

    def _finish(self, cmd: list[str]) -> dict:
        proc = self._popen(cmd, cwd="/tmp")
        out, err = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 0, err.decode()[:800])
        return json.loads(out.decode())

    def _crash(self, cmd: list[str], ready: Path, point: str) -> dict:
        if ready.exists():
            ready.unlink()
        proc = self._popen(cmd, cwd="/tmp")
        self._wait_ready_file(proc, ready, point)
        os.kill(proc.pid, signal.SIGKILL)
        out, _err = proc.communicate(timeout=5)
        return {"returncode": proc.returncode, "stdout": out.decode()}

    def _wait_ready_file(self, proc: subprocess.Popen, ready: Path, point: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if ready.exists():
                seen = json.loads(ready.read_text(encoding="utf-8"))
                if seen.get("point") == point and seen.get("pid") == proc.pid:
                    return
            if proc.poll() is not None:
                err = proc.stderr.read().decode()[:800] if proc.stderr else ""
                raise AssertionError(f"early {point} {proc.returncode} {err}")
            time.sleep(0.02)
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
        raise AssertionError(f"timeout {point}")

    def _wait_phase(self, base: str, phase: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if _http(base, "GET", "/v1/admin/state").get("phase") == phase:
                return
            time.sleep(0.02)
        raise AssertionError(phase)

    def _reap(self) -> None:
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            if proc.stdout is not None and not proc.stdout.closed:
                try:
                    proc.communicate(timeout=5)
                except Exception:
                    proc.wait(timeout=5)
            else:
                proc.wait(timeout=5)


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
    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    body = None if payload is None else json.dumps(payload).encode()
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
    raw = conn.getresponse().read()
    conn.close()
    return json.loads(raw.decode())


def _protocol(store: Path) -> int:
    conn = sqlite3.connect(store)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM protocol_log WHERE path='/v1/responses'").fetchone()[0])
    finally:
        conn.close()


def _score(store: Path) -> dict[str, int]:
    conn = sqlite3.connect(store)
    try:
        return {
            "effects": int(conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]),
            "posts": int(conn.execute("SELECT COUNT(*) FROM http_log WHERE path='/v1/effects'").fetchone()[0]),
        }
    finally:
        conn.close()


def _calls(store: Path) -> list[dict]:
    conn = sqlite3.connect(store)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT call_id, child_run_id, adopted, state FROM agent_calls ORDER BY rowid").fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _call_count(store: Path) -> int:
    return len(_calls(store))


def _attempts(store: Path) -> int:
    conn = sqlite3.connect(store)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM agent_attempts").fetchone()[0])
    finally:
        conn.close()


def _items(store: Path, kind: str) -> list[str]:
    conn = sqlite3.connect(store)
    try:
        return [row[0] for row in conn.execute("SELECT body_json FROM agent_items WHERE kind=?", (kind,))]
    finally:
        conn.close()


def _file_hash(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


if __name__ == "__main__":
    unittest.main()
