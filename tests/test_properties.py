"""Random pure graphs and one independently enumerated diamond.

The clean-value function below is handwritten. It does not call the planner.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from rewindagent.cases import random_scenarios
from rewindagent.engine import record, rerun
from rewindagent.evaluate import run_suite
from rewindagent.model import default_tools
from rewindagent.reference import evaluate_graph
from rewindagent.verify import contaminates


ROOT = Path(__file__).resolve().parents[1]


def _local_clean(graph: dict) -> dict[str, int]:
    values: dict[str, int] = {}
    for node in graph["nodes"]:
        kind = node["kind"]
        if kind == "set":
            values[node["write"]] = int(node["const"])
        elif kind == "inc":
            values[node["write"]] = values[node["reads"][0]] + 1
        elif kind == "add":
            values[node["write"]] = values[node["reads"][0]] + values[node["reads"][1]]
        else:
            raise AssertionError(kind)
    return values


def _diamond_minimum_recompute() -> set[frozenset[str]]:
    """Brute force on the fixed 5-node diamond. Does not call contaminates()."""
    nodes = ["P", "S", "L", "R", "J"]
    clean = {"p": 2, "s": 5, "l": 3, "r": 6, "j": 9}

    def simulate(recompute: set[str]) -> dict[str, int]:
        slots: dict[str, int] = {}
        slots["p"] = 2 if "P" in recompute else 2
        slots["s"] = 5 if "S" in recompute else 5
        slots["l"] = slots["p"] + 1 if "L" in recompute else 1003
        slots["r"] = slots["s"] + 1 if "R" in recompute else 6
        slots["j"] = slots["l"] + slots["r"] if "J" in recompute else 1009
        return slots

    feasible: list[set[str]] = []
    for mask in range(1, 1 << len(nodes)):
        chosen = {nodes[index] for index in range(len(nodes)) if mask & (1 << index)}
        if "L" not in chosen:
            continue
        if simulate(chosen) == clean:
            feasible.append(chosen)
    best = min(len(item) for item in feasible)
    return {frozenset(item) for item in feasible if len(item) == best}


def _save(name: str, payload: dict) -> Path:
    path = ROOT / "tests" / "regressions" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


class PropertyTest(unittest.TestCase):
    def test_random_graphs_match_the_local_formula(self) -> None:
        cases = random_scenarios()
        self.assertEqual(len(cases), 20)
        seeds = [case.graph["seed"] for case in cases]
        self.assertEqual(seeds, list(range(20261106, 20261126)))
        for case in cases:
            with self.subTest(case=case.case_id):
                expected = _local_clean(case.graph)
                self.assertEqual(evaluate_graph(case.graph), expected)
                recording = record(case.workflow, case.initial, case.fault, default_tools())
                result = rerun("B4", case.workflow, recording, case.initial, tools=default_tools())
                closure = contaminates(recording.events, case.fault.instance_id)
                if result.values != expected or result.status != "recovered" or set(result.reused) & closure:
                    path = _save(
                        case.case_id,
                        {
                            "values": result.values,
                            "expected": expected,
                            "status": result.status,
                            "reused": result.reused,
                            "recomputed": result.recomputed,
                            "closure": sorted(closure),
                        },
                    )
                    self.fail(path)
                known = {"genesis", "environment"} | {event["instance_id"] for event in result.events}
                for event in result.events:
                    for read in event["reads"]:
                        self.assertIn(read["producer"], known)
                self.assertEqual(result.duplicate_applied, 0)

    def test_diamond_enumeration_matches_b4_on_this_graph_only(self) -> None:
        minima = _diamond_minimum_recompute()
        self.assertEqual(minima, {frozenset({"L", "J"})})
        from rewindagent.cases import hand_scenarios
        from rewindagent.evaluate import run_case

        rows = {row["method"]: row for row in run_case(next(item for item in hand_scenarios() if item.case_id == "diamond"))}
        self.assertEqual(set(rows["B4"]["recomputed"]), {"L", "J"})
        self.assertLess(rows["B4"]["recomputed_count"], rows["B2"]["recomputed_count"])

    def test_suite_safety_gate_and_pre_registered_benefit_rules(self) -> None:
        report = run_suite()
        summary = report["summary"]
        self.assertEqual(report["actual_model_calls"], 0)
        self.assertEqual(report["actual_token_usage"], 0)
        self.assertEqual(report["failure_localization"], "given")
        self.assertIsNone(report["confidence_interval"])
        self.assertEqual(summary["scenario_count"], 34)
        self.assertEqual(summary["attempts"], 170)
        self.assertEqual(summary["timing_repeats"], 5)
        self.assertTrue(summary["timing_is_not_a_new_sample"])
        self.assertEqual(summary["duplicate_applied_rows"], [])
        self.assertEqual(summary["b4_cases_with_safety_failures"], [])
        self.assertTrue(summary["benefit_vs_suffix_on_diamond"])
        self.assertTrue(summary["linear_b4_recompute_equals_b2"])
        self.assertTrue(summary["pure_b4_recompute_equals_b3_on_every_eligible_case"])
        self.assertIn("linear", summary["b1_equals_b2_cases"])
        self.assertNotIn("diamond", summary["b1_equals_b2_cases"])
        self.assertEqual(summary["b4_eligible_count"], 30)
        self.assertEqual(summary["b4_eligible_success_count"], 30)
        for row in report["rows"]:
            self.assertEqual(row["actual_model_calls"], 0)
            if row["duplicate_applied"]:
                path = _save(f"duplicate-{row['case_id']}-{row['method']}", row)
                self.fail(path)
            if row["method"] == "B4" and row["safety_failures"]:
                path = _save(f"b4-safety-{row['case_id']}", row)
                self.fail(path)
            if row["status"] != "recovered":
                self.assertFalse(row["recovery_success"])
            if row["method"] == "B4" and row["pure"] and row["success_eligible"]:
                b3 = next(item for item in report["rows"] if item["case_id"] == row["case_id"] and item["method"] == "B3")
                self.assertEqual(row["recomputed"], b3["recomputed"])


if __name__ == "__main__":
    unittest.main()
