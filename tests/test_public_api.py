"""Public API checks. Expected values stay in this file and are not recovery inputs."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import rewindagent.engine as engine_module
from rewindagent.api import (
    EffectView,
    FaultSpec,
    LocalEffectSimulator,
    Step,
    ToolSpec,
    Workflow,
    execute,
    recover,
    resolve_effect,
)
from rewindagent.canonical import semantic_hash
from rewindagent.cli import eval_command, main as cli_main
from rewindagent.diagnose import render

ROOT = Path(__file__).resolve().parents[1]
COMPUTE_VALUES = {
    "left_in": 2,
    "right_in": 5,
    "left_out": 3,
    "right_out": 6,
    "total": 9,
}


def _inc(values: list[object]) -> tuple[object, ...]:
    return (values[0] + 1,)


def _add(values: list[object]) -> tuple[object, ...]:
    return (values[0] + values[1],)


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec("inc", _inc, impl_id="test.inc.v1"),
        ToolSpec("add", _add, impl_id="test.add.v1"),
    ]


def _workflow() -> Workflow:
    return Workflow(
        steps=(
            Step("left", "inc", ("left_in",), ("left_out",)),
            Step("right", "inc", ("right_in",), ("right_out",)),
            Step("join", "add", ("left_out", "right_out"), ("total",)),
        )
    )


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


class PublicApiTest(unittest.TestCase):
    def test_eval_refuses_to_overwrite_frozen_results(self) -> None:
        target = ROOT / "results" / "eval.json"
        before = target.read_bytes() if target.exists() else None
        code = eval_command(target)
        self.assertEqual(code, 3)
        self.assertEqual(target.read_bytes() if target.exists() else None, before)

    def test_safety_profile_reuses_the_independent_branch(self) -> None:
        recorded = execute(
            _workflow(),
            _tools(),
            {"left_in": 2, "right_in": 5},
            fault=FaultSpec("left", "removed"),
        )
        self.assertEqual(recorded.status, "recorded")
        self.assertEqual(recorded.values["left_out"], 1003)
        repaired = recover(recorded)
        self.assertEqual(repaired.status, "recovered")
        self.assertTrue(repaired.published)
        self.assertEqual(repaired.recomputed, ["left", "join"])
        self.assertEqual(repaired.reused, ["right"])
        self.assertEqual(repaired.values, COMPUTE_VALUES)
        self.assertEqual(repaired.evidence["profile"], "safety")
        self.assertFalse(repaired.diagnosis["planner_used"])

    def test_safety_profile_does_not_call_the_planner(self) -> None:
        def banned(*_args, **_kwargs):
            raise AssertionError("planner used on the safety profile")

        original_contaminates = engine_module.contaminates
        original_verify = engine_module.verify_reuse
        engine_module.contaminates = banned
        engine_module.verify_reuse = banned
        try:
            recorded = execute(_workflow(), _tools(), {"left_in": 2, "right_in": 5}, fault=FaultSpec("left"))
            repaired = recover(recorded, profile="safety")
        finally:
            engine_module.contaminates = original_contaminates
            engine_module.verify_reuse = original_verify
        self.assertEqual(repaired.status, "recovered")
        self.assertEqual(repaired.values, COMPUTE_VALUES)

    def test_experiment_profile_calls_the_planner_without_changing_this_case(self) -> None:
        calls: list[str] = []

        def contaminates(events, instance):
            calls.append("contaminates")
            return original_contaminates(events, instance)

        def verify_reuse(*args, **kwargs):
            calls.append("verify_reuse")
            return original_verify(*args, **kwargs)

        original_contaminates = engine_module.contaminates
        original_verify = engine_module.verify_reuse
        engine_module.contaminates = contaminates
        engine_module.verify_reuse = verify_reuse
        try:
            recorded = execute(_workflow(), _tools(), {"left_in": 2, "right_in": 5}, fault=FaultSpec("left"))
            repaired = recover(recorded, profile="experiment")
        finally:
            engine_module.contaminates = original_contaminates
            engine_module.verify_reuse = original_verify
        self.assertEqual(calls, ["contaminates", "verify_reuse"])
        self.assertEqual(repaired.status, "recovered")
        self.assertEqual(repaired.recomputed, ["left", "join"])
        self.assertTrue(repaired.diagnosis["planner_used"])

    def test_published_values_are_not_aliased_to_the_candidate_or_the_next_attempt(self) -> None:
        recorded = execute(_workflow(), _tools(), {"left_in": 2, "right_in": 5}, fault=FaultSpec("left"))
        repaired = recover(recorded)
        self.assertIsNot(repaired.values, repaired.diagnosis["candidate_values"])
        repaired.values["total"] = -1
        repaired.diagnosis["candidate_values"]["total"] = -2
        again = recover(recorded)
        self.assertEqual(again.values["total"], 9)
        self.assertEqual(again.diagnosis["candidate_values"]["total"], 9)

    def test_escape_refusal_does_not_publish_the_candidate(self) -> None:
        tools = [ToolSpec("gone", lambda values: (1,), escapes=True)]
        workflow = Workflow(steps=(Step("gone", "gone", (), ("out",)),))
        recorded = execute(workflow, tools, {}, fault=FaultSpec("gone"))
        refused = recover(recorded)
        self.assertEqual(refused.status, "safe_refusal")
        self.assertEqual(refused.reason_code, "refused_unobservable_dependency")
        self.assertFalse(refused.published)
        self.assertIsNone(refused.values)
        self.assertIsNotNone(refused.diagnosis["candidate_values"])

    def test_missing_fault_is_not_applicable(self) -> None:
        recorded = execute(_workflow(), _tools(), {"left_in": 2, "right_in": 5})
        stopped = recover(recorded)
        self.assertEqual(stopped.status, "not_applicable")
        self.assertEqual(stopped.reason_code, "fault_required")
        self.assertFalse(stopped.published)
        self.assertIsNone(stopped.values)

    def test_persistent_fault_is_an_execution_failure(self) -> None:
        recorded = execute(
            _workflow(),
            _tools(),
            {"left_in": 2, "right_in": 5},
            fault=FaultSpec("left", "persistent"),
        )
        stopped = recover(recorded)
        self.assertEqual(stopped.status, "execution_failure")
        self.assertEqual(stopped.reason_code, "persistent_fault")
        self.assertFalse(stopped.published)
        self.assertIsNone(stopped.values)

    def test_public_resolve_does_not_resend_when_the_query_is_absent(self) -> None:
        simulator = LocalEffectSimulator(idempotent_keys=True, status_query=True, query_available=True)
        missing = resolve_effect(
            EffectView("notify", 4, attempt_started=True, status_query=True, idempotent_keys=True),
            simulator,
        )
        self.assertEqual(missing.status, "unknown")
        self.assertEqual(missing.evidence["service_submits"], 0)
        self.assertEqual(simulator.visible_log()["submits"], [])
        simulator.stage_hidden("notify", 4)
        confirmed = resolve_effect(
            EffectView("notify", 4, attempt_started=True, status_query=True, idempotent_keys=True),
            simulator,
        )
        self.assertEqual(confirmed.status, "recovered")
        self.assertEqual(confirmed.reason_code, "effect_confirmed_by_query")
        self.assertEqual(simulator.visible_log()["submits"], [])
        fresh = LocalEffectSimulator(idempotent_keys=False, status_query=False)
        first = resolve_effect(EffectView("new-op", 4, attempt_started=False), fresh)
        self.assertEqual(first.reason_code, "effect_sent")
        self.assertEqual(len(fresh.visible_log()["submits"]), 1)
        unsupported = resolve_effect(
            EffectView("notify", 4, attempt_started=True, status_query=True),
            LocalEffectSimulator(idempotent_keys=False, status_query=False),
        )
        self.assertEqual(unsupported.status, "unsupported")
        self.assertEqual(unsupported.reason_code, "query_required")

    def test_unknown_effect_does_not_submit_or_publish(self) -> None:
        simulator = LocalEffectSimulator(idempotent_keys=False, status_query=True, query_available=False)
        simulator.stage_hidden("notify", 4)
        result = resolve_effect(
            EffectView("notify", 4, attempt_started=True, status_query=True),
            simulator,
        )
        self.assertEqual(result.status, "unknown")
        self.assertFalse(result.published)
        self.assertIsNone(result.values)
        self.assertEqual(result.evidence["service_submits"], 0)
        self.assertGreater(result.evidence["service_queries"], 0)
        self.assertEqual(result.evidence["query_result"]["status"], "unavailable")
        self.assertEqual(simulator.visible_log()["submits"], [])
        self.assertNotIn("_applied", result.evidence)

    def test_same_payload_does_not_collapse_two_logical_operations(self) -> None:
        digest = semantic_hash({"body": 4})
        simulator = LocalEffectSimulator(idempotent_keys=False, status_query=False, query_available=False)
        simulator.save_receipt("charge.1", digest, "charge.1#recorded")
        first = resolve_effect(EffectView("charge.1", 4, attempt_started=True, idempotent_keys=False), simulator)
        second = resolve_effect(EffectView("charge.2", 4, attempt_started=False), simulator)
        self.assertEqual(first.status, "recovered")
        self.assertEqual(first.reused, ["charge.1"])
        self.assertEqual(second.status, "recovered")
        self.assertEqual(second.recomputed, ["charge.2"])
        submits = simulator.visible_log()["submits"]
        self.assertEqual([item["logical_id"] for item in submits], ["charge.2"])
        receipts = simulator.visible_log()["receipts"]
        self.assertEqual(sorted(item["logical_id"] for item in receipts), ["charge.1", "charge.2"])
        self.assertEqual({item["payload_hash"] for item in receipts}, {digest})

    def test_changed_payload_keeps_the_old_receipt(self) -> None:
        digest = semantic_hash({"body": 4})
        simulator = LocalEffectSimulator(idempotent_keys=False, status_query=False)
        simulator.save_receipt("mail", digest, "mail#recorded")
        refused = resolve_effect(EffectView("mail", 5, attempt_started=True), simulator)
        self.assertEqual(refused.status, "safe_refusal")
        self.assertFalse(refused.published)
        self.assertIsNone(refused.values)
        self.assertEqual(refused.diagnosis["candidate_values"]["planned_body"], 5)
        self.assertEqual(simulator.receipt("mail")["payload_hash"], digest)
        self.assertEqual(simulator.visible_log()["submits"], [])

    def test_runtime_modules_do_not_import_the_evaluation_suite(self) -> None:
        banned = {"rewindagent.evaluate", "rewindagent.cases", "rewindagent.round2", "rewindagent.reference"}
        for relative in ("rewindagent/api.py", "rewindagent/diagnose.py", "examples/branch_recover.py"):
            imported = _imports(ROOT / relative)
            self.assertTrue(banned.isdisjoint(imported), relative)

    def test_inspect_reads_without_executing_or_writing(self) -> None:
        work = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="rewind-inspect-")))
        marker = work / "executed"
        payload = {
            "schema": "rewindagent.execution_record",
            "schema_version": 1,
            "status": "unknown",
            "published": False,
            "reason_code": "effect_unknown",
            "message": "visible evidence is insufficient",
            "recomputed": [],
            "reused": [],
            "abandoned": [],
            "operation_ids": ["notify"],
            "evidence": {"logical_id": "notify", "query_result": "unavailable"},
            "code": f"__import__('pathlib').Path({str(marker)!r}).write_text('no')",
        }
        record = work / "record.json"
        record.write_text(json.dumps(payload), encoding="utf-8")
        before = hashlib.sha256(record.read_bytes()).hexdigest()
        text = render(record)
        self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), before)
        self.assertIn("status: unknown", text)
        self.assertIn("unknown_operation: notify", text)
        self.assertIn("contract: effect_unknown", text)
        self.assertFalse(marker.exists())
        code = cli_main(["inspect", str(record)])
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(record.read_bytes()).hexdigest(), before)
        self.assertFalse(marker.exists())

    def test_example_script_from_another_directory(self) -> None:
        out = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="rewind-example-")))
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        snapshot = ROOT / "examples" / "data" / "snapshot.json"
        compute = subprocess.run(
            [
                sys.executable,
                str(ROOT / "examples" / "branch_recover.py"),
                "--snapshot",
                str(snapshot),
                "--out",
                str(out),
                "--mode",
                "compute",
            ],
            cwd="/tmp",
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(compute.returncode, 0, compute.stderr + compute.stdout)
        saved = json.loads((out / "compute.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "recovered")
        self.assertEqual(saved["values"], COMPUTE_VALUES)
        self.assertEqual(saved["recomputed"], ["left", "join"])
        self.assertEqual(saved["reused"], ["right"])
        unknown = subprocess.run(
            [
                sys.executable,
                str(ROOT / "examples" / "branch_recover.py"),
                "--out",
                str(out),
                "--mode",
                "unknown",
            ],
            cwd="/tmp",
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(unknown.returncode, 0, unknown.stderr + unknown.stdout)
        saved_unknown = json.loads((out / "unknown.json").read_text(encoding="utf-8"))
        self.assertEqual(saved_unknown["status"], "unknown")
        self.assertFalse(saved_unknown["published"])
        self.assertIsNone(saved_unknown["values"])


if __name__ == "__main__":
    unittest.main()
