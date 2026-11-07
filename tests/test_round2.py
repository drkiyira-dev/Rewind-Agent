"""Round-2 checks. Allowed effect outcomes come from docs/round2-expectations.md."""

from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

import rewindagent.engine as engine_module
from rewindagent.cases import all_scenarios, hand_scenarios
from rewindagent.effects import choose_action, effect_cases, payload_hash, run_effect_case
from rewindagent.engine import record, rerun
from rewindagent.evaluate import run_suite
from rewindagent.model import default_tools
from rewindagent.round2 import PAIR_FIELDS, conclude, run_round2


ROOT = Path(__file__).resolve().parents[1]
SEMANTIC_KEYS = (
    "case_id",
    "method",
    "status",
    "recomputed",
    "reused",
    "prefix_skipped",
    "abandoned",
    "new_events",
    "recomputed_count",
    "reused_count",
    "simulated_cost",
    "actual_model_calls",
    "actual_token_usage",
    "external_attempts",
    "duplicate_applied",
    "safety_failures",
    "oracle_match",
    "failure_localization",
    "used_checkpoint",
    "ledger_unchanged",
    "recovery_success",
    "b1_equals_b2",
    "success_eligible",
    "pure",
    "expected_b4_status",
    "ledger_entries",
)


def _save(name: str, payload: dict) -> Path:
    path = ROOT / "tests" / "regressions" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class Round2Test(unittest.TestCase):
    def test_b3_plus_does_not_call_the_planner(self) -> None:
        case = next(item for item in hand_scenarios() if item.case_id == "diamond")
        called: list[str] = []
        real_contaminates = engine_module.contaminates
        real_verify = engine_module.verify_reuse

        def contaminates_spy(*args, **kwargs):
            called.append("contaminates")
            return real_contaminates(*args, **kwargs)

        def verify_spy(*args, **kwargs):
            called.append("verify_reuse")
            return real_verify(*args, **kwargs)

        engine_module.contaminates = contaminates_spy
        engine_module.verify_reuse = verify_spy
        try:
            recording = record(case.workflow, case.initial, case.fault, default_tools())
            rerun("B3+", case.workflow, recording, case.initial, tools=default_tools())
            self.assertEqual(called, [])
            rerun("B4", case.workflow, recording, case.initial, tools=default_tools())
        finally:
            engine_module.contaminates = real_contaminates
            engine_module.verify_reuse = real_verify
        self.assertIn("contaminates", called)
        self.assertIn("verify_reuse", called)

    def test_refusal_does_not_publish_or_leak(self) -> None:
        case = next(item for item in hand_scenarios() if item.case_id == "irreversible_changed")
        initial = dict(case.initial)
        recording = record(case.workflow, initial, case.fault, default_tools())
        values_before = dict(recording.values)
        ledger_before = [dict(entry) for entry in recording.ledger_entries]
        cache_before = copy.deepcopy(recording.cache)
        marked_id = id(recording.marked)
        recording_values_id = id(recording.values)
        first = rerun("B4", case.workflow, recording, initial, tools=default_tools())
        self.assertFalse(first.published)
        self.assertIsNone(first.published_values)
        self.assertIsNone(first.values)
        self.assertEqual(first.candidate_values.get("m"), 2)
        self.assertEqual(recording.values, values_before)
        self.assertEqual(recording.values["m"], 1002)
        self.assertEqual(id(recording.values), recording_values_id)
        self.assertEqual(recording.ledger_entries, ledger_before)
        self.assertEqual(first.ledger_entries, ledger_before)
        self.assertEqual(recording.cache, cache_before)
        self.assertEqual(id(recording.marked), marked_id)
        self.assertEqual(initial, {"seed": 1})
        first.candidate_values["m"] = 999
        second = rerun("B4", case.workflow, recording, initial, tools=default_tools())
        self.assertEqual(second.candidate_values.get("m"), 2)
        self.assertIsNone(second.published_values)
        self.assertEqual(recording.values["m"], 1002)

    def test_cache_and_checkpoint_objects_stay_with_the_caller(self) -> None:
        case = next(item for item in hand_scenarios() if item.case_id == "diamond")
        recording = record(case.workflow, case.initial, case.fault, default_tools())
        cache_before = copy.deepcopy(recording.cache)
        marked_id = id(recording.marked)
        blob_id = id(recording.marked[0])
        content_hash = recording.marked[0]["content_hash"]
        result = rerun("B4", case.workflow, recording, case.initial, tools=default_tools())
        self.assertIsNot(result.cache, recording.cache)
        self.assertEqual(recording.cache, cache_before)
        self.assertEqual(id(recording.marked), marked_id)
        self.assertEqual(id(recording.marked[0]), blob_id)
        self.assertEqual(recording.marked[0]["content_hash"], content_hash)
        next(iter(result.cache.values()))["outputs"].append("leak")
        self.assertEqual(recording.cache, cache_before)
        self.assertTrue(result.published)
        self.assertEqual(result.published_values, result.candidate_values)
        self.assertIsNot(result.published_values, result.candidate_values)

    def test_effect_outcomes_follow_the_preregistered_sets(self) -> None:
        source = (ROOT / "rewindagent" / "effects.py").read_text(encoding="utf-8")
        self.assertNotIn("contaminates", source)
        self.assertNotIn("verify_reuse", source)
        self.assertNotIn("_applied", choose_action.__code__.co_names)
        self.assertNotIn("hidden_for_evaluator", choose_action.__code__.co_names)
        for case in effect_cases():
            caller_receipts = copy.deepcopy(case["receipts"])
            for method in ("B0", "B1", "B2", "B3", "B3+", "B4"):
                row = run_effect_case(method, case)
                if row["outcome"] != case["predicted"][method]:
                    path = _save(
                        f"{case['case_id']}-{method}",
                        {"row": row, "predicted": case["predicted"][method]},
                    )
                    self.fail(path)
                self.assertEqual(case["receipts"], caller_receipts)
                self.assertFalse(row["planner_used"])
                self.assertEqual(row["actual_model_calls"], 0)

    def test_effect_identity_and_unknown_results(self) -> None:
        by_id = {item["case_id"]: item for item in effect_cases()}
        recovered = run_effect_case("B4", by_id["G_different_operation_same_payload"])
        self.assertEqual(recovered["recomputed"], ["charge.2"])
        self.assertEqual(recovered["hidden_apply_events"], 2)
        hashes = [item["payload_hash"] for item in recovered["receipts"]]
        self.assertEqual(hashes[0], hashes[1])
        self.assertEqual(
            {item["logical_id"] for item in recovered["receipts"]},
            {"charge.1", "charge.2"},
        )
        same = run_effect_case("B4", by_id["E_same_operation_same_payload"])
        self.assertEqual(same["duplicate_applied"], 0)
        self.assertEqual(same["hidden_apply_events"], 1)
        self.assertEqual(same["service_submits"], 1)
        unknown = run_effect_case("B4", by_id["H_query_unavailable"])
        self.assertEqual(unknown["outcome"], "unknown")
        self.assertEqual(unknown["service_submits"], 0)
        self.assertGreater(unknown["service_queries"], 0)
        self.assertFalse(unknown["published"])
        self.assertEqual(unknown["hidden_apply_events"], 1)
        lost = run_effect_case("B4", by_id["C_response_not_durable"])
        self.assertEqual(lost["service_submits"], 0)
        self.assertEqual(lost["service_queries"], 0)
        refused = run_effect_case("B4", by_id["F_same_operation_new_payload"])
        self.assertIsNone(refused["published_values"])
        self.assertEqual(refused["candidate_values"]["mail"], 5)
        self.assertEqual(refused["receipts"][0]["payload_hash"], payload_hash(4))
        self.assertEqual(len(refused["receipts"]), 1)
        again = run_effect_case("B4", by_id["F_same_operation_new_payload"])
        self.assertEqual(again["outcome"], "safe_refusal")
        self.assertEqual(again["receipts"][0]["payload_hash"], payload_hash(4))

    def test_round2_rule_matches_its_rows(self) -> None:
        report = run_round2()
        summary = report["summary"]
        self.assertEqual(summary["conclusion"], conclude(report["rows"])["conclusion"])
        self.assertEqual(report["actual_model_calls"], 0)
        self.assertEqual(report["actual_token_usage"], 0)
        self.assertIn(
            summary["conclusion"],
            {"engineering_packaging", "no_additional_value", "repeatable_extra_value", "difference_observed"},
        )
        paired = {}
        for row in report["rows"]:
            if row["suite"] != "slot":
                continue
            paired.setdefault(row["case_id"], {})[row["method"]] = row
            if row["method"] == "B3+":
                self.assertFalse(row["planner_used"])
        for case in all_scenarios():
            left = paired[case.case_id]["B4"]
            right = paired[case.case_id]["B3+"]
            if left["planner_used"] != (case.fault is not None):
                path = _save(f"planner-{case.case_id}", {"b4": left, "fault": case.fault is not None})
                self.fail(path)
            mismatched = [field for field in PAIR_FIELDS if left[field] != right[field]]
            if mismatched:
                path = _save(f"pair-{case.case_id}", {"fields": mismatched, "b4": left, "b3_plus": right})
                self.fail(path)


if __name__ == "__main__":
    unittest.main()
