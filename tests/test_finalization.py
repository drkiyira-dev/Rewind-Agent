"""Acceptance gaps for phase B. Evidence always uses a fresh output directory."""

from __future__ import annotations

import hashlib
import json
import os
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
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "examples" / "effect_run.py"
PURE = ROOT / "examples" / "durable_run.py"
SPEC = ROOT / "examples" / "data" / "effect_workflow.json"
SNAP = ROOT / "examples" / "data" / "effect_snapshot.json"
PURE_SPEC = ROOT / "examples" / "data" / "durable_workflow.json"
PURE_SNAP = ROOT / "examples" / "data" / "snapshot.json"
EVIDENCE: list[dict] = []


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env.pop("PHASE2A_WRITE", None)
    env.pop("PHASE2B_WRITE", None)
    return env


_stop = stop_process

def _http(base: str, method: str, path: str, payload: dict | None = None) -> dict:
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
    keys = conn.execute("SELECT COUNT(*) FROM effects WHERE idempotency_key IS NOT NULL").fetchone()[0]
    posts = conn.execute("SELECT COUNT(*) FROM http_log WHERE path='/v1/effects'").fetchone()[0]
    conn.close()
    return {"effects": effects, "keyed_effect_rows": keys, "posts": posts}


def _serve(owner, tmp: Path, profile: str, retention_ms: int = 60000) -> tuple[subprocess.Popen, str, Path]:
    store = tmp / "service.sqlite"
    ready = tmp / "ready.json"
    if ready.exists():
        ready.unlink()
    proc = owned_popen(owner,
        [
            sys.executable,
            "-m",
            "rewindagent",
            "effect-serve",
            "--store",
            str(store),
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
                    return proc, meta["base_url"], store
            if proc.poll() is not None:
                raise AssertionError(proc.stderr.read().decode()[:600])
            time.sleep(0.02)
        _stop(proc)
        raise AssertionError("service timeout")
    except BaseException:
        stop_process(proc)
        raise

def _run(owner, args: list[str]) -> tuple[dict, int]:
    proc = owned_popen(owner,
        [sys.executable, str(WORKER), *args],
        cwd="/tmp",
        env=_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=20)
        if proc.returncode != 0:
            raise AssertionError(err.decode()[:600] + out.decode()[:300])
        return json.loads(out.decode()), proc.pid
    finally:
        stop_process(proc)

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


def _wait_phase(base: str, phase: str) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if _http(base, "GET", "/v1/admin/state").get("phase") == phase:
            return
        time.sleep(0.02)
    raise AssertionError(phase)


def _start_args(store: Path, url: str) -> list[str]:
    return [
        "start",
        "--store",
        str(store),
        "--run-id",
        "mix",
        "--service-url",
        url,
        "--spec",
        str(SPEC),
        "--snapshot",
        str(SNAP),
    ]


class FinalizationTest(unittest.TestCase):
    def test_p2_direct_posts_are_not_deduplicated(self) -> None:
        from rewindagent.canonical import semantic_hash

        with tempfile.TemporaryDirectory() as tmp:
            svc, url, store = _serve(self, Path(tmp), "P2")
            try:
                caps = _http(url, "GET", "/v1/capabilities")
                self.assertFalse(caps["idempotent"])
                self.assertTrue(caps["query"])
                payload = {"values": [3]}
                request = {
                    "idempotency_key": "same-key",
                    "logical_id": "same-operation",
                    "payload": payload,
                    "payload_digest": semantic_hash(payload),
                    "contract_version": "append.v1",
                }
                # Direct HTTP bypasses all RewindAgent recovery decisions.
                replies = [_http(url, "POST", "/v1/effects", request) for _ in range(2)]
                self.assertTrue(all(r["status"] == "confirmed" and not r["replay"] for r in replies))
                self.assertNotEqual(replies[0]["effect_id"], replies[1]["effect_id"])
                score = _score(store)
                self.assertEqual(score, {"effects": 2, "keyed_effect_rows": 2, "posts": 2})
                query = _http(url, "GET", "/v1/effects/same-key")
                self.assertEqual(query["status"], "confirmed")
                self.assertIn(query["effect_id"], [r["effect_id"] for r in replies])
                conn = sqlite3.connect(store)
                try:
                    indexes = conn.execute("PRAGMA index_list(effects)").fetchall()
                    self.assertNotIn("effects_key", [r[1] for r in indexes])
                finally:
                    conn.close()
                EVIDENCE.append({"id": "P2_direct_duplicate", "capabilities": caps,
                                 "score": score, "query": query, "replies": replies,
                                 "indexes": indexes, "hidden_truth_used_by_runtime": False})
            finally:
                _stop(svc)

    def test_j_k_and_p2(self) -> None:
        self._window("J", "before_commit", expect_effects_after_kill=0)
        self._window("K", "after_commit", expect_effects_after_kill=1)
        self._p2()

    def _window(self, name: str, pause: str, expect_effects_after_kill: int) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old, url, store = _serve(self, root, "P0")
            client = None
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": pause})
                client = _spawn(self, _start_args(root / "run.sqlite", url))
                _wait_phase(url, pause)
                old_rc = _stop(old)
                client_out, client_err = client.communicate(timeout=10)
                client_status = json.loads(client_out.decode())
                restarted, new_url, same = _serve(self, root, "P0")
                try:
                    self.assertEqual(same, store)
                    after_kill = _score(store)
                    self.assertEqual(after_kill["effects"], expect_effects_after_kill)
                    self.assertEqual(after_kill["keyed_effect_rows"], expect_effects_after_kill)
                    if expect_effects_after_kill == 0:
                        resumed, resume_pid = _run(self,
                            ["resume", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", new_url]
                        )
                        self.assertEqual(resumed["status"], "unknown")
                        self.assertFalse(resumed["published"])
                        self.assertEqual(_score(store)["effects"], 0)
                        basis = resumed["reason_code"]
                    else:
                        resumed, resume_pid = _run(self,
                            ["reconcile", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", new_url]
                        )
                        self.assertTrue(resumed["published"])
                        self.assertEqual(_score(store)["effects"], 1)
                        self.assertEqual(_score(store)["posts"], after_kill["posts"])
                        basis = "query_confirmed"
                    EVIDENCE.append(
                        {
                            "id": name,
                            "old_service_pid": old.pid,
                            "old_service_returncode": old_rc,
                            "new_service_pid": restarted.pid,
                            "client_pid": client.pid,
                            "client_returncode": client.returncode,
                            "client_status": client_status["status"],
                            "resume_pid": resume_pid,
                            "service_store": store.name,
                            "same_store": True,
                            "score_after_restart": after_kill,
                            "score_after_resume": _score(store),
                            "local_status": resumed["status"],
                            "published": resumed["published"],
                            "basis": basis,
                            "hidden_truth_used_by_runtime": False,
                        }
                    )
                finally:
                    _stop(restarted)
            finally:
                _stop(old)
                if client is not None:
                    _stop(client)

    def _p2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, store = _serve(self, root, "P2")
            try:
                _http(url, "POST", "/v1/admin/fault", {"pause": "before_commit"})
                client = _spawn(self, _start_args(root / "run.sqlite", url))
                _wait_phase(url, "before_commit")
                _stop(client)
                during = _score(store)
                self.assertEqual(during["effects"], 0)
                self.assertEqual(during["posts"], 1)
                unknown, pid = _run(self,
                    ["resume", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url]
                )
                self.assertEqual(unknown["reason_code"], "effect_not_found")
                self.assertEqual(_score(store)["posts"], 1)
                _http(url, "POST", "/v1/admin/fault", {"release": True, "pause": None})
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and _score(store)["effects"] != 1:
                    time.sleep(0.02)
                self.assertEqual(_score(store)["effects"], 1)
                confirmed, reconcile_pid = _run(self,
                    ["reconcile", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url]
                )
                self.assertTrue(confirmed["published"])
                final = _score(store)
                self.assertEqual(final["posts"], 1)
                self.assertEqual(final["effects"], 1)
                EVIDENCE.append(
                    {
                        "id": "P2",
                        "service_pid": svc.pid,
                        "client_pid": client.pid,
                        "resume_pid": pid,
                        "reconcile_pid": reconcile_pid,
                        "profile": "P2",
                        "score_during_inflight": during,
                        "score_final": final,
                        "local_status": confirmed["status"],
                        "published": True,
                        "basis": "query_confirmed_without_repost",
                        "hidden_truth_used_by_runtime": False,
                    }
                )
            finally:
                _stop(svc)

    def test_effect_store_is_not_silently_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, _store = _serve(self, root, "P0")
            try:
                ready = root / "boundary.json"
                proc = _spawn(self, _start_args(root / "run.sqlite", url) + ["--stop-at", "E1", "--ready", str(ready)])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not ready.exists():
                    time.sleep(0.02)
                _stop(proc)
                db = root / "run.sqlite"
                conn = sqlite3.connect(db)
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
                conn.execute("DROP TABLE effect_meta")
                conn.commit()
                conn.close()
                refused, _pid = _run(self,
                    ["resume", "--store", str(db), "--run-id", "mix", "--service-url", url]
                )
                self.assertEqual(refused["reason_code"], "effect_schema_incomplete")
                self.assertFalse(refused["published"])
                conn = sqlite3.connect(db)
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                conn.close()
                self.assertNotIn("effect_meta", names)
                self.assertEqual(_score(_store)["posts"], 0)
            finally:
                _stop(svc)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, store = _serve(self, root, "P0")
            try:
                ready = root / "boundary.json"
                proc = _spawn(self, _start_args(root / "ok.sqlite", url) + ["--stop-at", "E1", "--ready", str(ready)])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not ready.exists():
                    time.sleep(0.02)
                _stop(proc)
                db = root / "ok.sqlite"
                conn = sqlite3.connect(db)
                conn.execute("UPDATE effect_meta SET format_version=99")
                conn.commit()
                conn.close()
                refused, _pid = _run(self, ["resume", "--store", str(db), "--run-id", "mix", "--service-url", url])
                self.assertEqual(refused["status"], "unsupported")
                self.assertEqual(refused["reason_code"], "unknown_effect_format")
                self.assertFalse(refused["published"])
                posts = _score(store)["posts"]
                refused2, _pid = _run(self, ["resume", "--store", str(db), "--run-id", "mix", "--service-url", url])
                self.assertEqual(refused2["reason_code"], "unknown_effect_format")
                self.assertEqual(_score(store)["posts"], posts)
            finally:
                _stop(svc)

    def test_pure_store_and_effect_store_without_adapter(self) -> None:
        from rewindagent.api import ToolSpec
        from rewindagent.durable import resume_run, start_run

        def inc(values: list[object]) -> tuple[object, ...]:
            return (values[0] + 1,)

        def add(values: list[object]) -> tuple[object, ...]:
            return (values[0] + values[1],)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "pure.sqlite"
            workflow = json.loads(PURE_SPEC.read_text(encoding="utf-8"))
            initial = json.loads(PURE_SNAP.read_text(encoding="utf-8"))
            tools = [
                ToolSpec("inc", inc, impl_id="durable.inc.v1"),
                ToolSpec("add", add, impl_id="durable.add.v1"),
            ]
            started = start_run(store, "pure", workflow, initial, tools)
            self.assertTrue(started.published)
            conn = sqlite3.connect(store)
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 1)
            names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertNotIn("effect_meta", names)
            again = resume_run(store, "pure", tools)
            self.assertTrue(again.published)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, store = _serve(self, root, "P0")
            try:
                ready = root / "boundary.json"
                proc = _spawn(self, _start_args(root / "run.sqlite", url) + ["--stop-at", "E1", "--ready", str(ready)])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not ready.exists():
                    time.sleep(0.02)
                _stop(proc)
                result = resume_run(
                    root / "run.sqlite",
                    "mix",
                    [ToolSpec("inc", inc, impl_id="effect.inc.v1")],
                )
                self.assertEqual(result.reason_code, "service_unregistered")
                self.assertFalse(result.published)
                body = result.to_dict()
                self.assertEqual(_score(store)["posts"], 0)
                EVIDENCE.append(
                    {
                        "id": "effect_db_without_adapter",
                        "reason_code": body["reason_code"],
                        "published": False,
                        "posts": 0,
                    }
                )
            finally:
                _stop(svc)

    def test_client_retention_does_not_claim_server_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, store = _serve(self, root, "P1", retention_ms=1000)
            try:
                ready = root / "boundary.json"
                proc = _spawn(self, _start_args(root / "run.sqlite", url) + ["--now-ms", "1000", "--stop-at", "E5", "--ready", str(ready)])
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not ready.exists():
                    time.sleep(0.02)
                _stop(proc)
                stopped, pid = _run(self,
                    ["resume", "--store", str(root / "run.sqlite"), "--run-id", "mix", "--service-url", url, "--now-ms", "999999"]
                )
                self.assertEqual(stopped["reason_code"], "retention_expired")
                self.assertFalse(stopped["published"])
                score = _score(store)
                self.assertEqual(score["effects"], 1)
                self.assertEqual(score["keyed_effect_rows"], 1)
                self.assertEqual(score["posts"], 1)
                EVIDENCE.append(
                    {
                        "id": "client_retention_only",
                        "resume_pid": pid,
                        "reason_code": "retention_expired",
                        "server_cleanup_verified": False,
                        "score": score,
                        "hidden_truth_used_by_runtime": False,
                    }
                )
            finally:
                _stop(svc)

    def test_status_and_inspect_do_not_post(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            svc, url, store = _serve(self, root, "P0")
            try:
                _run(self, _start_args(root / "run.sqlite", url))
                before = _score(store)
                _run(self, ["status", "--store", str(root / "run.sqlite"), "--run-id", "mix"])
                subprocess.run(
                    [sys.executable, "-m", "rewindagent", "durable", "inspect", "--store", str(root / "run.sqlite"), "--run-id", "mix"],
                    cwd=str(ROOT),
                    env=_env(),
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(_score(store), before)
            finally:
                _stop(svc)


def tearDownModule() -> None:
    digest = hashlib.sha256()
    for rel in (
        "rewindagent/api.py",
        "rewindagent/effects.py",
        "rewindagent/effectflow.py",
        "rewindagent/effectsvc.py",
        "rewindagent/durable.py",
    ):
        digest.update(rel.encode())
        digest.update((ROOT / rel).read_bytes())
    report = {
        "schema": "rewindagent.finalization.acceptance",
        "python": sys.version,
        "source_sha256": digest.hexdigest(),
        "actual_model_calls": 0,
        "actual_token_usage": 0,
        "server_expiry_cleanup_implemented": False,
        "cases": EVIDENCE,
    }
    evidence_path("acceptance.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
