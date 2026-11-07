"""Cross-process effect tests. The runtime never receives the service database path."""

from __future__ import annotations

import json
import os
import platform
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from tests.process_support import owned_popen, stop_process, evidence_path

import http.client

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "examples" / "effect_run.py"
SPEC = ROOT / "examples" / "data" / "effect_workflow.json"
SNAP = ROOT / "examples" / "data" / "effect_snapshot.json"
CASES: list[dict] = []


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    return env


def _http(base: str, method: str, path: str, payload: dict | None = None) -> dict:
    from urllib.parse import urlsplit

    parts = urlsplit(base)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=5)
    body = None if payload is None else json.dumps(payload).encode()
    conn.request(method, path, body=body, headers={"Content-Type": "application/json"})
    raw = conn.getresponse().read()
    conn.close()
    return json.loads(raw.decode())


def _score(store: Path) -> dict[str, int]:
    conn = sqlite3.connect(store)
    effects = conn.execute("SELECT COUNT(*) FROM effects").fetchone()[0]
    posts = conn.execute("SELECT COUNT(*) FROM http_log WHERE path='/v1/effects'").fetchone()[0]
    conn.close()
    return {"effects": effects, "posts": posts}


def _start_service(owner, tmp: Path, profile: str, retention_ms: int = 60000) -> tuple[subprocess.Popen, str]:
    ready = tmp / f"svc-{profile}.json"
    if ready.exists():
        ready.unlink()
    proc = owned_popen(owner,
        [
            sys.executable,
            "-m",
            "rewindagent",
            "effect-serve",
            "--store",
            str(tmp / f"svc-{profile}.sqlite"),
            "--profile",
            profile,
            "--ready",
            str(ready),
            "--retention-ms",
            str(retention_ms),
        ],
        cwd=str(ROOT),
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.exists():
                meta = json.loads(ready.read_text(encoding="utf-8"))
                if meta.get("pid") == proc.pid:
                    return proc, meta["base_url"]
            if proc.poll() is not None:
                raise AssertionError(proc.stderr.read().decode()[:800])
            time.sleep(0.02)
        stop_process(proc)
        raise AssertionError("service timeout")
    except BaseException:
        stop_process(proc)
        raise

def _spawn(owner, args: list[str]) -> subprocess.Popen:
    return owned_popen(owner,
        [sys.executable, str(WORKER), *args],
        cwd="/tmp",
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )


def _finish(owner, args: list[str], timeout: float = 20) -> tuple[dict, int]:
    proc = _spawn(owner, args)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            proc.wait(timeout=5)
            raise AssertionError(f"timeout {args}")
        if proc.returncode != 0:
            raise AssertionError(f"{proc.returncode} {err.decode()[:800]} {out.decode()[:400]}")
        return json.loads(out.decode()), proc.pid
    finally:
        stop_process(proc)

def _crash(owner, args: list[str], ready: Path, point: str) -> dict:
    if ready.exists():
        ready.unlink()
    proc = _spawn(owner, [*args, "--stop-at", point, "--ready", str(ready)])
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if ready.exists():
                seen = json.loads(ready.read_text(encoding="utf-8"))
                if seen.get("point") == point and seen.get("pid") == proc.pid:
                    break
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(f"early {point} {proc.returncode} {err!r} {out!r}")
            time.sleep(0.02)
        else:
            stop_process(proc)
            proc.wait(timeout=5)
            raise AssertionError(f"timeout {point}")
        stop_process(proc)
        out, _err = proc.communicate(timeout=5)
        return {"pid": proc.pid, "returncode": proc.returncode, "stdout": out.decode()}
    finally:
        stop_process(proc)

def _wait_phase(base: str, phase: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        view = _http(base, "GET", "/v1/admin/state")
        if view.get("phase") == phase:
            return
        time.sleep(0.02)
    raise AssertionError(f"phase {phase}")


def _base(store: Path, url: str, spec: Path = SPEC) -> list[str]:
    return [
        "start",
        "--store",
        str(store),
        "--run-id",
        "mix",
        "--service-url",
        url,
        "--spec",
        str(spec),
        "--snapshot",
        str(SNAP),
    ]


def _resume(store: Path, url: str) -> list[str]:
    return ["resume", "--store", str(store), "--run-id", "mix", "--service-url", url]


class EffectCrashTest(unittest.TestCase):
    def test_runtime_source_does_not_read_service_truth_or_admin(self) -> None:
        flow = (ROOT / "rewindagent" / "effectflow.py").read_text(encoding="utf-8")
        client = (ROOT / "examples" / "effect_run.py").read_text(encoding="utf-8")
        self.assertNotIn("choose_action", flow)
        self.assertNotIn("/v1/admin", flow)
        self.assertNotIn("/v1/admin", client)
        self.assertNotIn("http_log", flow)
        self.assertNotIn("http_log", client)

    def test_service_honesty_and_normal_completion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            non, url3 = _start_service(self, root, "P3")
            try:
                from rewindagent.effectsvc import AppendClient

                client = AppendClient(url3, timeout_s=5)
                body = {
                    "idempotency_key": "same",
                    "logical_id": "rec-1",
                    "payload_digest": "abc",
                    "payload": {"values": [3]},
                    "contract_version": "append.v1",
                }
                # digest will not match; use the real digest
                from rewindagent.canonical import semantic_hash

                payload = {"values": [3]}
                body["payload"] = payload
                body["payload_digest"] = semantic_hash(payload)
                client.submit(body)
                client.submit(body)
                self.assertEqual(_score(root / "svc-P3.sqlite")["effects"], 2)
            finally:
                stop_process(non)
            idem, url0 = _start_service(self, root, "P0")
            try:
                from rewindagent.canonical import semantic_hash
                from rewindagent.effectsvc import AppendClient

                client = AppendClient(url0, timeout_s=5)
                payload = {"values": [3]}
                same = {
                    "idempotency_key": "same",
                    "logical_id": "rec-1",
                    "payload": payload,
                    "payload_digest": semantic_hash(payload),
                    "contract_version": "append.v1",
                }
                client.submit(same)
                client.submit(same)
                other = dict(same)
                other["payload"] = {"values": [9]}
                other["payload_digest"] = semantic_hash(other["payload"])
                conflict = client.submit(other)
                self.assertEqual(conflict["status"], "conflict")
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                result, _pid = _finish(self, _base(root / "run.sqlite", url0))
                self.assertEqual(result["values"]["n2"], 3)
                self.assertEqual(result["values"]["report"], 4)
                self.assertEqual(result["values"]["ack"], semantic_hash({"values": [3]}))
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 2)
            finally:
                stop_process(idem)
            CASES.append({"name": "honesty_and_normal", "timeout": False, "hidden_truth_used_by_runtime": False})

    def test_e0_and_e1_keep_the_logical_identity(self) -> None:
        from rewindagent.effectflow import effect_key
        from rewindagent.effectsvc import CONTRACT_VERSION, SERVICE_ID

        expected = effect_key(SERVICE_ID, CONTRACT_VERSION, "mix", "rec-1")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                first = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E0")
                self.assertEqual(first["returncode"], -signal.SIGKILL)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 0)
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertTrue(resumed["published"])
                conn = sqlite3.connect(root / "run.sqlite")
                key = conn.execute("SELECT idempotency_key FROM effect_ops").fetchone()[0]
                conn.close()
                self.assertEqual(key, expected)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E1")
                conn = sqlite3.connect(root / "run.sqlite")
                state, key = conn.execute("SELECT state, idempotency_key FROM effect_ops").fetchone()
                conn.close()
                self.assertEqual(state, "prepared")
                self.assertEqual(key, expected)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 0)
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertTrue(resumed["published"])
                conn = sqlite3.connect(root / "run.sqlite")
                key2 = conn.execute("SELECT idempotency_key FROM effect_ops").fetchone()[0]
                conn.close()
                self.assertEqual(key2, expected)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                CASES.append({
                    "name": "E0_E1",
                    "e0_pid": first["pid"],
                    "e1_pid": killed["pid"],
                    "hidden_truth_used_by_runtime": False,
                })
            finally:
                stop_process(svc)

    def test_e2_without_idempotency_or_query_stays_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P3")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E2")
                self.assertEqual(_score(root / "svc-P3.sqlite")["posts"], 0)
                first, _pid = _finish(self, _resume(root / "run.sqlite", url))
                second, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(first["status"], "unknown")
                self.assertEqual(second["status"], "unknown")
                self.assertFalse(first["published"])
                self.assertEqual(_score(root / "svc-P3.sqlite")["posts"], 0)
                self.assertEqual(_score(root / "svc-P3.sqlite")["effects"], 0)
                CASES.append({"name": "E2_P3", "killed_pid": killed["pid"], "status": "unknown"})
            finally:
                stop_process(svc)

    def test_e3_not_found_does_not_submit_and_later_reconcile_confirms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "before_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "before_commit")
                stop_process(proc)
                proc.wait(timeout=5)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 0)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 1)
                unknown, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(unknown["reason_code"], "effect_not_found")
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 1)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and _score(root / "svc-P0.sqlite")["effects"] != 1:
                    time.sleep(0.02)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                confirmed, _pid = _finish(self, ["reconcile", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url])
                self.assertTrue(confirmed["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 1)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                CASES.append({"name": "E3_then_reconcile", "killed_pid": proc.pid, "service_pid": svc.pid})
            finally:
                stop_process(svc)

    def test_e4_profiles_and_e5_saved_only_from_public_evidence(self) -> None:
        from rewindagent.canonical import semantic_hash

        ack = semantic_hash({"values": [3]})
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "after_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "after_commit")
                stop_process(proc)
                proc.wait(timeout=5)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(resumed["values"]["ack"], ack)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P1")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "after_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "after_commit")
                stop_process(proc)
                proc.wait(timeout=5)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertTrue(resumed["published"])
                self.assertEqual(_score(root / "svc-P1.sqlite")["effects"], 1)
                self.assertEqual(_score(root / "svc-P1.sqlite")["posts"], 2)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P3")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "after_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "after_commit")
                stop_process(proc)
                proc.wait(timeout=5)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
                unknown, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(unknown["status"], "unknown")
                self.assertEqual(_score(root / "svc-P3.sqlite")["effects"], 1)
                self.assertEqual(_score(root / "svc-P3.sqlite")["posts"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E5")
                self.assertEqual(killed["stdout"], "")
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(resumed["values"]["ack"], ack)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], 1)
                CASES.append({"name": "E4_E5", "hidden_truth_used_by_runtime": False})
            finally:
                stop_process(svc)

    def test_e6_e7_e8_and_repeat_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E6")
                conn = sqlite3.connect(root / "run.sqlite")
                state = conn.execute("SELECT state FROM effect_ops").fetchone()[0]
                steps = conn.execute("SELECT instance_id FROM step_commits").fetchall()
                conn.close()
                self.assertNotEqual(state, "confirmed")
                self.assertNotIn("append", [row[0] for row in steps])
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertTrue(resumed["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E7")
                conn = sqlite3.connect(root / "run.sqlite")
                steps = [row[0] for row in conn.execute("SELECT instance_id FROM step_commits ORDER BY position")]
                conn.close()
                self.assertEqual(steps, ["prep", "append"])
                before = _score(root / "svc-P0.sqlite")["posts"]
                resumed, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(resumed["values"]["report"], 4)
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], before)
                self.assertEqual(resumed["recomputed"], ["tail"])
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E8")
                self.assertEqual(killed["stdout"], "")
                self.assertEqual(killed["returncode"], -signal.SIGKILL)
                status, _pid = _finish(self, ["status", "--store", str(root / "run.sqlite"), "--run-id", "mix"])
                publication = status["publication_id"]
                before = _score(root / "svc-P0.sqlite")["effects"]
                again, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(again["publication_id"], publication)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], before)
                CASES.append({"name": "E6_E7_E8", "e8_pid": killed["pid"], "publication_id": publication})
            finally:
                stop_process(svc)

    def test_identity_conflict_and_distinct_operations(self) -> None:
        conflict = {
            "schema_version": 1,
            "steps": [
                {"step_id": "a", "kind": "effect", "logical_id": "rec-1", "service_id": "local-append", "reads": ["n"], "writes": ["ack1"], "config": {}},
                {"step_id": "b", "kind": "effect", "logical_id": "rec-1", "service_id": "local-append", "reads": ["m"], "writes": ["ack2"], "config": {}},
            ],
        }
        distinct = {
            "schema_version": 1,
            "steps": [
                {"step_id": "a", "kind": "effect", "logical_id": "rec-1", "service_id": "local-append", "reads": ["n"], "writes": ["ack1"], "config": {}},
                {"step_id": "b", "kind": "effect", "logical_id": "rec-2", "service_id": "local-append", "reads": ["n"], "writes": ["ack2"], "config": {}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "conflict.json").write_text(json.dumps(conflict), encoding="utf-8")
            (root / "distinct.json").write_text(json.dumps(distinct), encoding="utf-8")
            (root / "snap.json").write_text(json.dumps({"n": 4, "m": 8}), encoding="utf-8")
            svc, url = _start_service(self, root, "P0")
            try:
                refused, _pid = _finish(self,
                    ["start", "--store", str(root / "c.sqlite"), "--run-id", "mix", "--service-url", url,
                     "--spec", str(root / "conflict.json"), "--snapshot", str(root / "snap.json")]
                )
                self.assertEqual(refused["reason_code"], "effect_conflict")
                self.assertFalse(refused["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                done, _pid = _finish(self,
                    ["start", "--store", str(root / "d.sqlite"), "--run-id", "other", "--service-url", url,
                     "--spec", str(root / "distinct.json"), "--snapshot", str(root / "snap.json")]
                )
                self.assertTrue(done["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 3)
                CASES.append({"name": "conflict_and_distinct", "effects": 3})
            finally:
                stop_process(svc)

    def test_query_outage_then_reconcile_and_retention(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "after_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "after_commit")
                stop_process(proc)
                _http(url, "POST", "/v1/admin/fault", {"query_mode": "unavailable", "release": True, "pause": None})
                unknown, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(unknown["status"], "unknown")
                posts = _score(root / "svc-P0.sqlite")["posts"]
                self.assertEqual(posts, 1)
                again, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(again["status"], "unknown")
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], posts)
                _http(url, "POST", "/v1/admin/fault", {"query_mode": "normal"})
                confirmed, _pid = _finish(self, ["reconcile", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url])
                self.assertTrue(confirmed["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], posts)
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P1", retention_ms=1000)
            try:
                killed = _crash(self, _base(root / "run.sqlite", url) + ["--now-ms", "1000"], root / "ready.json", "E5")
                self.assertEqual(_score(root / "svc-P1.sqlite")["effects"], 1)
                stopped, _pid = _finish(self, _resume(root / "run.sqlite", url) + ["--now-ms", "999999"])
                self.assertEqual(stopped["reason_code"], "retention_expired")
                self.assertEqual(_score(root / "svc-P1.sqlite")["posts"], 1)
                CASES.append({"name": "query_then_retention", "e5_pid": killed["pid"]})
            finally:
                stop_process(svc)

    def test_busy_liar_pure_failure_and_service_restart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "before_commit"})
                holder = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "before_commit")
                before = _score(root / "svc-P0.sqlite")["posts"]
                busy, busy_pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(busy["reason_code"], "executor_busy")
                self.assertEqual(_score(root / "svc-P0.sqlite")["posts"], before)
                stop_process(holder)
                holder.wait(timeout=5)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                killed = _crash(self, _base(root / "run.sqlite", url), root / "ready.json", "E5")
                _http(url, "POST", "/v1/admin/fault", {"query_mode": "lie_payload"})
                refused, _pid = _finish(self, _resume(root / "run.sqlite", url))
                self.assertEqual(refused["reason_code"], "receipt_mismatch")
                self.assertFalse(refused["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                CASES.append({"name": "busy_and_liar", "busy_pid": busy_pid, "killed_pid": killed["pid"]})
            finally:
                stop_process(svc)
        boom = {
            "schema_version": 1,
            "steps": [
                {"step_id": "prep", "tool": "inc", "reads": ["n"], "writes": ["n2"], "config": {}},
                {"step_id": "append", "kind": "effect", "logical_id": "rec-1", "service_id": "local-append", "reads": ["n2"], "writes": ["ack"], "config": {}},
                {"step_id": "tail", "tool": "boom", "reads": ["n2"], "writes": ["report"], "config": {}},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "boom.json").write_text(json.dumps(boom), encoding="utf-8")
            svc, url = _start_service(self, root, "P0")
            try:
                failed, _pid = _finish(self, _base(root / "run.sqlite", url, root / "boom.json") + ["--boom-tail"])
                self.assertEqual(failed["reason_code"], "persistent_tool_failure")
                self.assertFalse(failed["published"])
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                again, _pid = _finish(self, _resume(root / "run.sqlite", url) + ["--boom-tail"])
                self.assertEqual(again["reason_code"], "persistent_tool_failure")
                self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
            finally:
                stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "before_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "before_commit")
                stop_process(svc)
                svc.wait(timeout=5)
                out, err = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, err.decode()[:400])
                self.assertEqual(json.loads(out.decode())["status"], "unknown")
                svc2, url2 = _start_service(self, root, "P0")
                try:
                    self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 0)
                    unknown, _pid = _finish(self, _resume(root / "run.sqlite", url2))
                    self.assertEqual(unknown["status"], "unknown")
                    self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 0)
                finally:
                    stop_process(svc2)
            finally:
                if svc.poll() is None:
                    stop_process(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url = _start_service(self, root, "P0")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "after_commit"})
                proc = _spawn(self, _base(root / "run.sqlite", url))
                _wait_phase(url, "after_commit")
                stop_process(svc)
                svc.wait(timeout=5)
                proc.wait(timeout=10)
                svc2, url2 = _start_service(self, root, "P0")
                try:
                    self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                    done, _pid = _finish(self, ["reconcile", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url2])
                    self.assertTrue(done["published"])
                    self.assertEqual(_score(root / "svc-P0.sqlite")["effects"], 1)
                    CASES.append({"name": "service_restart", "hidden_truth_used_by_runtime": False, "timeout": False})
                finally:
                    stop_process(svc2)
            finally:
                if svc.poll() is None:
                    stop_process(svc)


def tearDownModule() -> None:
    path = evidence_path("phase2b-crash-results.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": "rewindagent.phase2b.crash_results",
                "python": sys.version,
                "platform": platform.platform(),
                "sqlite_version": sqlite3.sqlite_version,
                "command": "python3 -m unittest tests.test_effect_crash",
                "actual_model_calls": 0,
                "actual_token_usage": 0,
                "hidden_truth_used_by_runtime": False,
                "cases": CASES,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
