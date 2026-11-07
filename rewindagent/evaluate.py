"""Run the pre-registered suite. Oracle values are not passed into the planner."""

from __future__ import annotations

from time import perf_counter_ns
from typing import Any

from .cases import Scenario, all_scenarios
from .engine import record, rerun
from .model import Tool, default_tools, tool_inc
from .reference import evaluate_graph

METHODS = ("B0", "B1", "B2", "B3", "B4")


def _tools_for(case: Scenario) -> dict[str, Tool]:
    tools = default_tools()
    if case.change_impl:
        tools["inc"] = Tool("inc", "inc.v2", tool_inc)
    return tools


def _oracle_values(case: Scenario) -> dict[str, Any] | None:
    if case.expected_values is not None:
        return dict(case.expected_values)
    if case.graph is not None:
        return evaluate_graph(case.graph)
    return None


def _safety(case: Scenario, recording_events: list[dict[str, Any]], row: dict[str, Any]) -> list[str]:
    failures = list(row["safety_failures"])
    by_id = {event["instance_id"]: event for event in recording_events}
    for reused in row["reused"]:
        event = by_id.get(reused)
        if event is None:
            continue
        if event.get("violation"):
            failures.append("unsafe_reuse_undeclared")
        if event.get("escapes"):
            failures.append("unsafe_reuse_escape")
    if case.family == "effect" and row["method"] == "B4":
        # Filled by the caller after comparing ledgers. Placeholder kept out.
        pass
    # Preserve order while dropping duplicates.
    unique: list[str] = []
    for item in failures:
        if item not in unique:
            unique.append(item)
    return unique


def run_case(case: Scenario, methods: tuple[str, ...] = METHODS) -> list[dict[str, Any]]:
    base_tools = default_tools()
    recording = record(case.workflow, case.initial, case.fault, base_tools)
    tools = _tools_for(case)
    oracle = _oracle_values(case)
    original_ledger = [dict(entry) for entry in recording.ledger_entries]
    rows = []
    for method in methods:
        result = rerun(
            method,
            case.workflow,
            recording,
            case.initial,
            tools=tools,
            mutate=case.mutate,
        )
        oracle_match = None if oracle is None else result.candidate_values == oracle
        row = {
            "case_id": case.case_id,
            "family": case.family,
            "method": method,
            "status": result.status,
            "recomputed": result.recomputed,
            "reused": result.reused,
            "prefix_skipped": result.prefix_skipped,
            "abandoned": result.abandoned,
            "new_events": result.new_events,
            "recomputed_count": len(result.recomputed),
            "reused_count": len(result.reused),
            "simulated_cost": result.simulated_cost,
            "actual_model_calls": 0,
            "actual_token_usage": 0,
            "external_attempts": result.external_attempts,
            "duplicate_applied": result.duplicate_applied,
            "safety_failures": list(result.safety_failures),
            "oracle_match": oracle_match,
            "failure_localization": "given",
            "plan_ns": result.plan_ns,
            "total_ns": result.total_ns,
            "used_checkpoint": result.used_checkpoint,
            "published": result.published,
            "planner_used": result.planner_used,
            "reasons": result.reasons,
            "ledger_entries": result.ledger_entries,
            "closure": result.closure,
            "success_eligible": case.success_eligible,
            "pure": case.pure,
            "expected_b4_status": case.expected_b4,
        }
        row["safety_failures"] = _safety(case, recording.events, row)
        ledger_same = result.ledger_entries == original_ledger
        row["ledger_unchanged"] = ledger_same
        if case.family == "effect" and not ledger_same:
            row["safety_failures"] = row["safety_failures"] + ["ledger_mutated"]
        row["recovery_success"] = (
            result.status == "recovered"
            and oracle_match is True
            and row["duplicate_applied"] == 0
            and not row["safety_failures"]
            and (case.family != "effect" or ledger_same)
        )
        rows.append(row)
    b1 = next(row for row in rows if row["method"] == "B1")
    b2 = next(row for row in rows if row["method"] == "B2")
    same_suffix = b1["recomputed"] == b2["recomputed"] and b1["status"] not in {
        "no_checkpoint",
        "checkpoint_corrupt",
        "not_applicable",
    }
    for row in rows:
        row["b1_equals_b2"] = same_suffix
    return rows


def _median(samples: list[int]) -> int:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def run_suite() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in all_scenarios():
        rows.extend(run_case(case))
    diamond = next(case for case in all_scenarios() if case.case_id == "diamond")
    recording = record(diamond.workflow, diamond.initial, diamond.fault, default_tools())
    samples = []
    for _ in range(5):
        started = perf_counter_ns()
        rerun("B4", diamond.workflow, recording, diamond.initial, tools=default_tools())
        samples.append(perf_counter_ns() - started)
    summary = _summarize(rows)
    summary["diamond_b4_timing_ns_median"] = _median(samples)
    summary["timing_repeats"] = 5
    summary["timing_is_not_a_new_sample"] = True
    return {
        "schema_version": 1,
        "failure_localization": "given",
        "actual_model_calls": 0,
        "actual_token_usage": 0,
        "confidence_interval": None,
        "confidence_interval_reason": "deterministic scenarios; repeats are timings only",
        "rows": rows,
        "summary": summary,
    }


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = len(rows)
    successes = [row for row in rows if row["recovery_success"]]
    b4 = [row for row in rows if row["method"] == "B4"]
    b4_success_eligible = [row for row in b4 if row["success_eligible"]]
    b4_eligible_success = [row for row in b4_success_eligible if row["recovery_success"]]
    pure_pairs = []
    for row in rows:
        if row["method"] != "B4" or not row["pure"] or not row["success_eligible"]:
            continue
        b3 = next(
            item
            for item in rows
            if item["case_id"] == row["case_id"] and item["method"] == "B3"
        )
        pure_pairs.append(row["recomputed"] == b3["recomputed"])
    diamond_b4 = next(row for row in b4 if row["case_id"] == "diamond")
    diamond_b2 = next(row for row in rows if row["case_id"] == "diamond" and row["method"] == "B2")
    linear_b4 = next(row for row in b4 if row["case_id"] == "linear")
    linear_b2 = next(row for row in rows if row["case_id"] == "linear" and row["method"] == "B2")
    b4_safety = [row["case_id"] for row in b4 if row["safety_failures"] or row["duplicate_applied"]]
    duplicate_rows = [f"{row['case_id']}:{row['method']}" for row in rows if row["duplicate_applied"]]
    return {
        "attempts": attempts,
        "scenario_count": len({row["case_id"] for row in rows}),
        "method_count": len(METHODS),
        "recovery_success_count": len(successes),
        "recovery_success_rate_all_attempts": len(successes) / attempts,
        "b4_eligible_count": len(b4_success_eligible),
        "b4_eligible_success_count": len(b4_eligible_success),
        "b4_eligible_success_rate": len(b4_eligible_success) / len(b4_success_eligible),
        "benefit_vs_suffix_on_diamond": diamond_b4["recomputed_count"] < diamond_b2["recomputed_count"],
        "linear_b4_recompute_equals_b2": linear_b4["recomputed"] == linear_b2["recomputed"],
        "pure_b4_recompute_equals_b3_on_every_eligible_case": all(pure_pairs),
        "b4_cases_with_safety_failures": b4_safety,
        "duplicate_applied_rows": duplicate_rows,
        "b1_equals_b2_cases": sorted({row["case_id"] for row in rows if row["b1_equals_b2"]}),
        "refusal_or_stop_rows": [
            {"case_id": row["case_id"], "method": row["method"], "status": row["status"]}
            for row in rows
            if row["status"] != "recovered"
        ],
    }
