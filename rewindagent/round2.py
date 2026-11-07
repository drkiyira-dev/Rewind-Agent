"""Second-round comparison. The conclusion rule is fixed in docs/round2-expectations.md."""

from __future__ import annotations

import hashlib
import platform
import sys
from pathlib import Path
from typing import Any

from .cases import all_scenarios
from .effects import effect_cases, run_effect_case
from .evaluate import run_case

SLOT_METHODS = ("B0", "B1", "B2", "B3", "B3+", "B4")
EFFECT_METHODS = SLOT_METHODS
PAIR_FIELDS = (
    "outcome",
    "service_submits",
    "service_queries",
    "duplicate_applied",
    "published",
    "safety_failures",
    "recomputed",
)


def source_sha256() -> str:
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    paths = sorted(root.glob("rewindagent/*.py"))
    paths.append(root / "docs" / "round2-expectations.md")
    for path in paths:
        digest.update(str(path.name).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _guarded_expectation(case_id: str, expected_b4: str) -> tuple[bool, str]:
    if expected_b4 == "recovered":
        return True, "recovered"
    if case_id in {"escape", "unbounded"}:
        return False, "safe_refusal"
    if case_id == "irreversible_changed":
        return True, "safe_refusal"
    if case_id == "persistent":
        return True, "execution_failure"
    raise ValueError(case_id)


def classify_slot(case: Any, row: dict[str, Any]) -> dict[str, Any]:
    evidence, guarded = _guarded_expectation(case.case_id, case.expected_b4)
    status = row["status"]
    method = row["method"]
    if method == "B1" and status == "no_checkpoint":
        applicable, attempted, outcome, reason = False, False, "not_applicable", "no_checkpoint_before_fault"
    elif method == "B2" and status == "not_applicable":
        applicable, attempted, outcome, reason = False, False, "not_applicable", "fault_instance_required"
    elif status == "checkpoint_corrupt":
        applicable, attempted, outcome, reason = True, True, "execution_failure", "checkpoint_corrupt"
    else:
        applicable, attempted, reason = True, True, "ran"
        unsafe = bool(row["safety_failures"]) or row["duplicate_applied"] > 0
        if unsafe:
            outcome = "unsafe_pass"
        elif status == "recovered" and row["recovery_success"]:
            outcome = "recovered"
        elif status == "recovered":
            outcome = "unknown"
        elif status == "persistent_fault":
            outcome = "execution_failure"
        elif status == "effect_replay_suppressed" or status == "external_blocked" or status.startswith("refused_") or status in {"verifier_rejected", "write_conflict"}:
            outcome = "safe_refusal"
        else:
            outcome = "execution_failure"
    unnecessary = (
        applicable
        and evidence
        and guarded == "recovered"
        and method in {"B3+", "B4"}
        and outcome in {"safe_refusal", "unknown"}
    )
    reasonable = applicable and (not evidence) and outcome in {"safe_refusal", "unknown"} and not row["safety_failures"]
    return {
        "suite": "slot",
        "case_id": case.case_id,
        "family": case.family,
        "method": method,
        "applicable": applicable,
        "applicability_reason": reason,
        "attempted": attempted,
        "outcome": outcome,
        "status": status,
        "safe_recovery": outcome == "recovered",
        "unnecessary_refusal": unnecessary,
        "reasonable_stop": reasonable,
        "recomputed": list(row["recomputed"]),
        "recomputed_count": row["recomputed_count"],
        "service_submits": row["external_attempts"],
        "service_queries": 0,
        "external_requests": row["external_attempts"],
        "duplicate_applied": row["duplicate_applied"],
        "conflict_submits": 0,
        "published": row["published"],
        "safety_failures": list(row["safety_failures"]),
        "planner_used": row["planner_used"],
        "plan_ns": row["plan_ns"],
        "evidence_sufficient": evidence,
        "guarded_expectation": guarded,
        "actual_model_calls": 0,
        "actual_token_usage": 0,
        "oracle_match": row["oracle_match"],
        "ledger_unchanged": row["ledger_unchanged"],
    }


def conclude(rows: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["suite"], row["case_id"]), {})[row["method"]] = row
    differences = []
    for key, methods in sorted(grouped.items()):
        left = methods.get("B4")
        right = methods.get("B3+")
        if left is None or right is None or not left["applicable"] or not right["applicable"]:
            continue
        mismatched = [field for field in PAIR_FIELDS if left[field] != right[field]]
        if mismatched:
            differences.append({"suite": key[0], "case_id": key[1], "fields": mismatched})

    def count(method: str, field: str, value: Any = True) -> int:
        return sum(1 for row in rows if row["method"] == method and row[field] == value)

    b4_unsafe = count("B4", "outcome", "unsafe_pass")
    b3p_unsafe = count("B3+", "outcome", "unsafe_pass")
    b3_unsafe = count("B3", "outcome", "unsafe_pass")
    b0_unsafe = count("B0", "outcome", "unsafe_pass")
    b4_unnecessary = count("B4", "unnecessary_refusal", True)
    if not differences and b4_unsafe == 0 and b3p_unsafe == 0 and (b3_unsafe or b0_unsafe):
        conclusion = "engineering_packaging"
    elif not differences and b4_unsafe == 0 and b3p_unsafe == 0 and b3_unsafe == 0 and b0_unsafe == 0:
        conclusion = "no_additional_value"
    elif differences and b4_unsafe < b3p_unsafe and b4_unnecessary == 0:
        conclusion = "repeatable_extra_value"
    else:
        conclusion = "difference_observed"
    return {
        "conclusion": conclusion,
        "paired_difference_count": len(differences),
        "paired_differences": differences,
        "b4_unsafe_pass": b4_unsafe,
        "b3_plus_unsafe_pass": b3p_unsafe,
        "b3_unsafe_pass": b3_unsafe,
        "b0_unsafe_pass": b0_unsafe,
        "b4_unnecessary_refusal": b4_unnecessary,
        "b3_plus_unnecessary_refusal": count("B3+", "unnecessary_refusal", True),
        "b4_reasonable_stop": count("B4", "reasonable_stop", True),
        "b4_safe_recovery": count("B4", "safe_recovery", True),
        "b3_plus_safe_recovery": count("B3+", "safe_recovery", True),
        "rows": len(rows),
        "applicable_rows": sum(1 for row in rows if row["applicable"]),
        "attempted_rows": sum(1 for row in rows if row["attempted"]),
        "not_applicable_rows": count_status(rows, "not_applicable"),
        "outcome_counts": outcome_counts(rows),
    }


def count_status(rows: list[dict[str, Any]], outcome: str) -> int:
    return sum(1 for row in rows if row["outcome"] == outcome)


def outcome_counts(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = found.setdefault(row["method"], {})
        bucket[row["outcome"]] = bucket.get(row["outcome"], 0) + 1
    return found


def run_round2() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for case in all_scenarios():
        for row in run_case(case, SLOT_METHODS):
            rows.append(classify_slot(case, row))
    for case in effect_cases():
        for method in EFFECT_METHODS:
            rows.append(run_effect_case(method, case))
    summary = conclude(rows)
    return {
        "schema_version": 2,
        "python": sys.version,
        "platform": platform.platform(),
        "command": "python3 -m rewindagent round2 --out results/round2.json",
        "source_sha256": source_sha256(),
        "expectations": "docs/round2-expectations.md",
        "actual_model_calls": 0,
        "actual_token_usage": 0,
        "failure_localization": "given",
        "confidence_interval": None,
        "rows": rows,
        "summary": summary,
    }
