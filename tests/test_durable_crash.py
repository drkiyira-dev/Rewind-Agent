"""Cross-process crash tests. Expected values stay in this file."""

from __future__ import annotations

import ast
import hashlib
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

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "examples" / "durable_run.py"
SPEC = ROOT / "examples" / "data" / "durable_workflow.json"
SPEC_ALT = ROOT / "examples" / "data" / "durable_workflow_alt.json"
BOOM = ROOT / "examples" / "data" / "durable_boom.json"
EFFECT = ROOT / "examples" / "data" / "durable_effect.json"
SNAPSHOT = ROOT / "examples" / "data" / "snapshot.json"
SNAPSHOT_ALT = ROOT / "examples" / "data" / "durable_snapshot_alt.json"
EXPECTED = {"left_in": 2, "left_out": 3, "right_in": 5, "right_out": 6, "total": 9}
CASES: list[dict] = []


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    env["REWIND_STOP_AT"] = "K0"
    return env


def _obs(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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


def _finish(owner, args: list[str], timeout: float = 30) -> tuple[dict, int]:
    proc = _spawn(owner, args)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            stop_process(proc)
            proc.wait(timeout=10)
            raise AssertionError(f"timeout {args}")
        if proc.returncode != 0:
            raise AssertionError(f"exit {proc.returncode} {err.decode()} {out.decode()}")
        return json.loads(out.decode()), proc.pid
    finally:
        stop_process(proc)

def _crash(owner, args: list[str], ready: Path, point: str, before_kill=None, timeout: float = 20):
    if ready.exists():
        ready.unlink()
    proc = _spawn(owner, [*args, "--stop-at", point, "--ready", str(ready)])
    try:
        deadline = time.monotonic() + timeout
        seen = None
        while time.monotonic() < deadline:
            if ready.exists():
                seen = json.loads(ready.read_text(encoding="utf-8"))
                if seen.get("point") == point and seen.get("pid") == proc.pid:
                    break
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise AssertionError(f"child exited before {point}: {proc.returncode} {out!r} {err!r}")
            time.sleep(0.02)
        else:
            stop_process(proc)
            proc.wait(timeout=10)
            raise AssertionError(f"timeout waiting for {point}")
        if before_kill is not None:
            before_kill()
        stop_process(proc)
        out, err = proc.communicate(timeout=10)
        return {
            "pid": proc.pid,
            "returncode": proc.returncode,
            "stdout": out.decode(),
            "stderr": err.decode(),
            "ready": seen,
        }
    finally:
        stop_process(proc)

def _start_args(store: Path, run_id: str, obs: Path, spec: Path = SPEC, snapshot: Path = SNAPSHOT) -> list[str]:
    return [
        "start",
        "--store",
        str(store),
        "--run-id",
        run_id,
        "--spec",
        str(spec),
        "--snapshot",
        str(snapshot),
        "--obs",
        str(obs),
    ]


def _resume_args(store: Path, run_id: str, obs: Path) -> list[str]:
    return ["resume", "--store", str(store), "--run-id", run_id, "--obs", str(obs)]


def _record(name: str, **payload) -> None:
    CASES.append({"name": name, "timeout": False, **payload})


class DurableCrashTest(unittest.TestCase):
    def test_library_does_not_import_modules_from_data(self) -> None:
        tree = ast.parse((ROOT / "rewindagent" / "durable.py").read_text(encoding="utf-8"))
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
        self.assertNotIn("importlib", modules)
        self.assertTrue(
            {"rewindagent.evaluate", "rewindagent.cases", "rewindagent.round2"}.isdisjoint(modules)
        )

    def test_k0_registration_committed_before_any_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            killed = _crash(self, _start_args(root / "t.sqlite", "run", root / "obs.jsonl"), root / "ready.json", "K0")
            self.assertEqual(killed["returncode"], -signal.SIGKILL)
            self.assertEqual(killed["stdout"], "")
            status, _ = _finish(self, ["status", "--store", str(root / "t.sqlite"), "--run-id", "run"])
            self.assertEqual(status["status"], "open")
            self.assertEqual(status["committed_steps"], [])
            resumed, pid = _finish(self, _resume_args(root / "t.sqlite", "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            self.assertTrue(resumed["published"])
            calls = _obs(root / "obs.jsonl")
            self.assertEqual(calls, [
                {"tool": "inc", "values": [2], "pid": pid},
                {"tool": "inc", "values": [5], "pid": pid},
                {"tool": "add", "values": [3, 6], "pid": pid},
            ])
            _record("K0", killed=killed, resume_pid=pid, status=resumed["status"], published=True)

    def test_k1_discards_uncommitted_computation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            killed = _crash(self, _start_args(root / "t.sqlite", "run", root / "obs.jsonl"), root / "ready.json", "K1")
            self.assertEqual(killed["returncode"], -signal.SIGKILL)
            status, _ = _finish(self, ["status", "--store", str(root / "t.sqlite"), "--run-id", "run"])
            self.assertEqual(status["committed_steps"], [])
            resumed, pid = _finish(self, _resume_args(root / "t.sqlite", "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            calls = _obs(root / "obs.jsonl")
            left = [item for item in calls if item["values"] == [2]]
            self.assertEqual(len(left), 2)
            self.assertNotEqual(left[0]["pid"], left[1]["pid"])
            self.assertEqual(resumed["recomputed"], ["left", "right", "join"])
            _record("K1", killed=killed, resume_pid=pid, left_calls=2, published=resumed["published"])

    def test_k2_rolls_back_the_open_step_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            journal = Path(str(store) + "-journal")
            seen = {}

            def before_kill() -> None:
                seen["journal"] = journal.exists()
                status, _ = _finish(self, ["status", "--store", str(store), "--run-id", "run"])
                seen["status"] = status

            killed = _crash(self,
                _start_args(store, "run", root / "obs.jsonl"),
                root / "ready.json",
                "K2",
                before_kill=before_kill,
            )
            self.assertEqual(killed["returncode"], -signal.SIGKILL)
            self.assertTrue(seen["journal"])
            self.assertEqual(seen["status"]["committed_steps"], [])
            self.assertFalse(seen["status"]["published"])
            resumed, pid = _finish(self, _resume_args(store, "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            left = [item for item in _obs(root / "obs.jsonl") if item["values"] == [2]]
            self.assertEqual(len(left), 2)
            status, _ = _finish(self, ["status", "--store", str(store), "--run-id", "run"])
            self.assertEqual(status["evidence"]["published_event_count"], 1)
            _record(
                "K2",
                killed=killed,
                journal_present_before_kill=True,
                committed_during_transaction=seen["status"]["committed_steps"],
                resume_pid=pid,
            )

    def test_k3_reuses_the_committed_step(self) -> None:
        self._k3_once("K3")

    def test_k3_repeat_is_only_a_reproducibility_check(self) -> None:
        self._k3_once("K3-repeat")

    def _k3_once(self, name: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            killed = _crash(self, _start_args(root / "t.sqlite", "run", root / "obs.jsonl"), root / "ready.json", "K3")
            self.assertEqual(killed["returncode"], -signal.SIGKILL)
            status, _ = _finish(self, ["status", "--store", str(root / "t.sqlite"), "--run-id", "run"])
            self.assertEqual(status["committed_steps"], ["left"])
            resumed, pid = _finish(self, _resume_args(root / "t.sqlite", "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            self.assertEqual(resumed["reused"], ["left"])
            self.assertEqual(resumed["recomputed"], ["right", "join"])
            calls = _obs(root / "obs.jsonl")
            self.assertEqual([item["values"] for item in calls], [[2], [5], [3, 6]])
            self.assertEqual(calls[0]["pid"], killed["pid"])
            self.assertEqual(calls[1]["pid"], pid)
            _record(name, killed_pid=killed["pid"], resume_pid=pid, reused=["left"])

    def test_k4_does_not_publish_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            journal = Path(str(store) + "-journal")
            seen = {}

            def before_kill() -> None:
                seen["journal"] = journal.exists()
                status, _ = _finish(self, ["status", "--store", str(store), "--run-id", "run"])
                seen["status"] = status

            killed = _crash(self,
                _start_args(store, "run", root / "obs.jsonl"),
                root / "ready.json",
                "K4",
                before_kill=before_kill,
            )
            self.assertTrue(seen["journal"])
            self.assertEqual(seen["status"]["status"], "open")
            self.assertEqual(seen["status"]["committed_steps"], ["left", "right", "join"])
            self.assertFalse(seen["status"]["published"])
            before = len(_obs(root / "obs.jsonl"))
            resumed, pid = _finish(self, _resume_args(store, "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            self.assertEqual(resumed["recomputed"], [])
            self.assertEqual(len(_obs(root / "obs.jsonl")), before)
            self.assertNotEqual(pid, killed["pid"])
            _record("K4", killed_pid=killed["pid"], resume_pid=pid, recomputed=[])

    def test_k5_keeps_the_publication_when_stdout_is_lost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            seen = {}

            def before_kill() -> None:
                status, _ = _finish(self, ["status", "--store", str(store), "--run-id", "run"])
                seen["status"] = status
                busy, busy_pid = _finish(self, _resume_args(store, "run", root / "obs-b.jsonl"))
                seen["busy"] = busy
                seen["busy_pid"] = busy_pid

            killed = _crash(self,
                _start_args(store, "run", root / "obs.jsonl"),
                root / "ready.json",
                "K5",
                before_kill=before_kill,
            )
            self.assertEqual(killed["stdout"], "")
            self.assertEqual(killed["returncode"], -signal.SIGKILL)
            self.assertTrue(seen["status"]["published"])
            self.assertEqual(seen["busy"]["reason_code"], "executor_busy")
            self.assertEqual(_obs(root / "obs-b.jsonl"), [])
            publication = seen["status"]["publication_id"]
            resumed, _pid = _finish(self, _resume_args(store, "run", root / "obs.jsonl"))
            self.assertEqual(resumed["publication_id"], publication)
            self.assertEqual(resumed["values"], EXPECTED)
            self.assertEqual(resumed["recomputed"], [])
            self.assertEqual(resumed["evidence"]["published_event_count"], 1)
            again, _pid = _finish(self, _resume_args(store, "run", root / "obs.jsonl"))
            self.assertEqual(again["publication_id"], publication)
            self.assertEqual(again["evidence"]["published_event_count"], 1)
            calls = _obs(root / "obs.jsonl")
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(item["pid"] == killed["pid"] for item in calls))
            _record("K5", killed_pid=killed["pid"], publication_id=publication, stdout_empty=True)

    def test_third_process_continues_after_the_resume_is_killed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            obs = root / "obs.jsonl"
            first = _crash(self, _start_args(store, "run", obs), root / "ready-a.json", "K3")
            second = _crash(self, _resume_args(store, "run", obs), root / "ready-b.json", "K3")
            self.assertEqual(first["returncode"], -signal.SIGKILL)
            self.assertEqual(second["returncode"], -signal.SIGKILL)
            third, pid = _finish(self, _resume_args(store, "run", obs))
            self.assertEqual(third["values"], EXPECTED)
            self.assertEqual(third["reused"], ["left", "right"])
            self.assertEqual(third["recomputed"], ["join"])
            calls = _obs(obs)
            self.assertEqual([item["pid"] for item in calls], [first["pid"], second["pid"], pid])
            self.assertEqual([item["values"] for item in calls], [[2], [5], [3, 6]])
            _record("third_process", pids=[first["pid"], second["pid"], pid])

    def test_lock_blocks_a_second_executor_until_the_first_exits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            killed = None
            ready = root / "ready.json"

            def before_kill() -> None:
                nonlocal killed
                busy, pid = _finish(self, _resume_args(store, "run", root / "obs-b.jsonl"))
                self.assertEqual(busy["reason_code"], "executor_busy")
                self.assertEqual(_obs(root / "obs-b.jsonl"), [])
                killed = pid

            dead = _crash(self, _start_args(store, "run", root / "obs.jsonl"), ready, "K0", before_kill=before_kill)
            self.assertEqual(dead["returncode"], -signal.SIGKILL)
            resumed, pid = _finish(self, _resume_args(store, "run", root / "obs.jsonl"))
            self.assertEqual(resumed["values"], EXPECTED)
            self.assertTrue(all(item["pid"] == pid for item in _obs(root / "obs.jsonl")))
            _record("busy", holder_pid=dead["pid"], competitor_pid=killed, resume_pid=pid)

    def test_changed_identity_does_not_reuse_the_old_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            obs = root / "obs.jsonl"
            started, _ = _finish(self, _start_args(store, "run", obs))
            self.assertEqual(started["values"], EXPECTED)
            before = _obs(obs)
            missing, _ = _finish(self, _resume_args(store, "run", obs) + ["--omit-tool", "add"])
            self.assertEqual(missing["reason_code"], "tool_missing")
            self.assertIsNone(missing["values"])
            version, _ = _finish(self, _resume_args(store, "run", obs) + ["--impl-suffix", ".other"])
            self.assertEqual(version["reason_code"], "tool_version_mismatch")
            self.assertIsNone(version["values"])
            changed_input, _ = _finish(self, _resume_args(store, "run", obs) + ["--snapshot", str(SNAPSHOT_ALT)])
            self.assertEqual(changed_input["reason_code"], "input_mismatch")
            changed_flow, _ = _finish(self, _resume_args(store, "run", obs) + ["--spec", str(SPEC_ALT)])
            self.assertEqual(changed_flow["reason_code"], "workflow_mismatch")
            self.assertEqual(_obs(obs), before)
            status, _ = _finish(self, ["status", "--store", str(store), "--run-id", "run"])
            self.assertEqual(status["values"], EXPECTED)
            self.assertEqual(status["evidence"]["published_event_count"], 1)
            _record("identity_refusal", codes=[
                missing["reason_code"],
                version["reason_code"],
                changed_input["reason_code"],
                changed_flow["reason_code"],
            ])

    def test_corrupt_or_unknown_store_is_not_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            garbage = root / "bad.sqlite"
            garbage.write_bytes(b"this is not sqlite")
            before = garbage.read_bytes()
            refused, _ = _finish(self, _resume_args(garbage, "run", root / "obs.jsonl"))
            self.assertEqual(refused["reason_code"], "store_corrupt")
            self.assertEqual(garbage.read_bytes(), before)
            unknown = root / "schema.sqlite"
            conn = sqlite3.connect(unknown)
            conn.execute("PRAGMA user_version=99")
            conn.commit()
            conn.close()
            before_unknown = unknown.read_bytes()
            refused, _ = _finish(self, _resume_args(unknown, "run", root / "obs.jsonl"))
            self.assertEqual(refused["reason_code"], "unknown_schema_version")
            self.assertEqual(unknown.read_bytes(), before_unknown)
            missing = root / "absent.sqlite"
            refused, _ = _finish(self, _resume_args(missing, "run", root / "obs.jsonl"))
            self.assertEqual(refused["reason_code"], "store_not_found")
            self.assertFalse(missing.exists())
            _record("corrupt_store", codes=["store_corrupt", "unknown_schema_version", "store_not_found"])

    def test_tampered_step_is_not_rebuilt_into_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            started, _ = _finish(self, _start_args(store, "run", root / "obs.jsonl"))
            self.assertTrue(started["published"])
            self.assertEqual(started["values"], EXPECTED)
            conn = sqlite3.connect(store)
            conn.execute("UPDATE step_commits SET output_json='[999]' WHERE instance_id='left'")
            conn.commit()
            conn.close()
            before = store.read_bytes()
            refused, _ = _finish(self, _resume_args(store, "run", root / "obs2.jsonl"))
            self.assertEqual(refused["reason_code"], "record_corrupt")
            self.assertFalse(refused["published"])
            self.assertIsNone(refused["values"])
            conn = sqlite3.connect(store)
            output = conn.execute("SELECT output_json FROM step_commits WHERE instance_id='left'").fetchone()[0]
            conn.close()
            self.assertEqual(output, "[999]")
            self.assertEqual(_obs(root / "obs2.jsonl"), [])
            self.assertGreater(store.stat().st_size, 0)
            self.assertNotEqual(before, b"")
            _record("record_corrupt", reason=refused["reason_code"])

    def test_persistent_tool_failure_is_not_retried_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            obs = root / "obs.jsonl"
            first, pid = _finish(self, _start_args(root / "t.sqlite", "run", obs, spec=BOOM) + ["--boom"])
            self.assertEqual(first["reason_code"], "persistent_tool_failure")
            self.assertFalse(first["published"])
            second, _ = _finish(self, _resume_args(root / "t.sqlite", "run", obs) + ["--boom"])
            self.assertEqual(second["reason_code"], "persistent_tool_failure")
            self.assertFalse(second["published"])
            calls = _obs(obs)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["pid"], pid)
            _record("persistent_tool_failure", calls=1)

    def test_effect_step_is_refused_before_a_run_is_created(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            refused, _ = _finish(self, _start_args(store, "run", root / "obs.jsonl", spec=EFFECT))
            self.assertEqual(refused["reason_code"], "unsupported_effect")
            self.assertFalse(store.exists())
            self.assertEqual(_obs(root / "obs.jsonl"), [])
            _record("unsupported_effect", store_created=False)

    def test_storage_settings_match_the_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = root / "t.sqlite"
            _finish(self, _start_args(store, "run", root / "obs.jsonl"))
            from rewindagent.durable import read_storage_settings

            settings = read_storage_settings(store)
            self.assertEqual(str(settings["journal_mode"]).lower(), "delete")
            self.assertEqual(settings["synchronous"], "FULL")
            self.assertEqual(settings["foreign_keys"], 1)
            self.assertEqual(settings["user_version"], 1)
            _record("storage_settings", settings=settings)


def tearDownModule() -> None:
    path = evidence_path("phase2a-crash-results.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    for source in sorted((ROOT / "rewindagent").glob("*.py")):
        digest.update(source.name.encode())
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    contract = ROOT / "docs" / "phase2a-contract.md"
    digest.update(contract.name.encode())
    digest.update(b"\0")
    digest.update(contract.read_bytes())
    report = {
        "schema": "rewindagent.phase2a.crash_results",
        "python": sys.version,
        "platform": platform.platform(),
        "sqlite_version": sqlite3.sqlite_version,
        "command": "python3 -m unittest tests.test_durable_crash",
        "package": str((ROOT / "rewindagent" / "__init__.py").resolve()),
        "source_sha256": digest.hexdigest(),
        "actual_model_calls": 0,
        "actual_token_usage": 0,
        "repeats_are_not_independent_samples": True,
        "power_loss_tested": False,
        "exactly_once_tools": False,
        "historical_audit": "run separately: python3 -m unittest tests.history_audit",
        "cases": CASES,
    }
    path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
