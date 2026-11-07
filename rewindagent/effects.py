"""Simulated external effects. choose_action sees only receipts, attempts, and query views."""

from __future__ import annotations

from time import perf_counter_ns
from typing import Any, Callable

from .canonical import semantic_hash


def payload_hash(body: int) -> str:
    return semantic_hash({"body": body})


def choose_action(
    logical_id: str,
    planned_hash: str,
    receipt: dict[str, str] | None,
    attempt_started: bool,
    *,
    idempotent_keys: bool,
    status_query: bool,
    query: Callable[[str], dict[str, Any]],
) -> dict[str, Any]:
    """Research baseline. An absent query may return send.

    The public ``resolve_effect`` path does not follow that branch. This
    function remains for the pre-registered in-process comparison only.
    """
    if receipt is not None:
        if receipt["payload_hash"] == planned_hash:
            return {"action": "reuse", "reason": "durable receipt matches the planned payload"}
        return {"action": "refuse_payload", "reason": "durable receipt has a different payload"}
    if not attempt_started:
        return {"action": "send", "use_key": False, "reason": "intact log has no attempt for this logical operation"}
    if status_query:
        view = query(logical_id)
        if view["status"] == "unavailable":
            return {"action": "unknown", "reason": "status query is unavailable"}
        if view["status"] == "applied" and view["payload_hash"] == planned_hash:
            return {
                "action": "persist_from_query",
                "payload_hash": view["payload_hash"],
                "reason": "query reports this payload already applied",
            }
        if view["status"] == "applied":
            return {"action": "refuse_payload", "reason": "query reports a different applied payload"}
        if view["status"] == "absent":
            return {"action": "send", "use_key": False, "reason": "query reports the logical operation is absent"}
        return {"action": "unknown", "reason": "query result is not a known status"}
    if idempotent_keys:
        return {
            "action": "send",
            "use_key": True,
            "reason": "idempotency key allows one retry of the same logical operation",
        }
    return {
        "action": "unknown",
        "reason": "an attempt started and neither a receipt, a query, nor an idempotency key is available",
    }


class ReceiptStore:
    def __init__(self, entries: list[dict[str, str]] | None = None) -> None:
        self.entries = [dict(entry) for entry in (entries or [])]

    def find(self, logical_id: str) -> dict[str, str] | None:
        for entry in self.entries:
            if entry["logical_id"] == logical_id:
                return entry
        return None

    def persist(self, logical_id: str, digest: str, attempt_id: str) -> None:
        if self.find(logical_id) is not None:
            return
        self.entries.append(
            {"logical_id": logical_id, "payload_hash": digest, "attempt_id": attempt_id}
        )


class SimulatedService:
    def __init__(self, *, idempotent_keys: bool, status_query: bool, query_available: bool) -> None:
        self.idempotent_keys = idempotent_keys
        self.status_query = status_query
        self.query_available = query_available
        self._applied: dict[str, str] = {}
        self.submits: list[dict[str, Any]] = []
        self.queries = 0
        self.apply_events = 0
        self.duplicate_applies = 0
        self.conflict_submits = 0

    def seed(self, logical_id: str, digest: str) -> None:
        self._applied[logical_id] = digest
        self.apply_events += 1

    def query(self, logical_id: str) -> dict[str, Any]:
        self.queries += 1
        if not self.status_query or not self.query_available:
            return {"status": "unavailable", "payload_hash": None}
        digest = self._applied.get(logical_id)
        if digest is None:
            return {"status": "absent", "payload_hash": None}
        return {"status": "applied", "payload_hash": digest}

    def submit(
        self,
        logical_id: str,
        digest: str,
        *,
        idempotency_key: str | None,
        attempt_id: str,
    ) -> str:
        self.submits.append(
            {
                "logical_id": logical_id,
                "payload_hash": digest,
                "attempt_id": attempt_id,
                "idempotency_key": idempotency_key,
            }
        )
        existing = self._applied.get(logical_id)
        if existing is None:
            self._applied[logical_id] = digest
            self.apply_events += 1
            return "applied"
        if existing == digest and idempotency_key and self.idempotent_keys:
            return "deduped"
        if existing == digest:
            self.apply_events += 1
            self.duplicate_applies += 1
            return "duplicate"
        self.conflict_submits += 1
        return "conflict"

    def hidden_for_evaluator(self) -> dict[str, Any]:
        return {
            "applied": dict(self._applied),
            "apply_events": self.apply_events,
            "duplicate_applies": self.duplicate_applies,
            "conflict_submits": self.conflict_submits,
        }


def _replay_action() -> dict[str, Any]:
    return {"action": "send", "use_key": False, "reason": "replay submits without reading receipts or queries"}


def run_effect_case(method: str, case: dict[str, Any]) -> dict[str, Any]:
    predicted = case["predicted"][method]
    if method in {"B1", "B2"}:
        reason = "no_checkpoint_before_fault" if method == "B1" else "fault_instance_required"
        return _row(
            case,
            method,
            applicable=False,
            reason=reason,
            attempted=False,
            outcome="not_applicable",
            status=reason,
            submits=0,
            queries=0,
            duplicate_applied=0,
            conflict_submits=0,
            published=False,
            candidate={item["logical_id"]: item["body"] for item in case["planned"]},
            receipts=list(case["receipts"]),
            sent=[],
            plan_ns=0,
            planner_used=False,
            safety=[],
            hidden_apply_events=len(case["applied"]),
        )
    service = SimulatedService(
        idempotent_keys=case["idempotent_keys"],
        status_query=case["status_query"],
        query_available=case["query_available"],
    )
    for item in case["applied"]:
        service.seed(item["logical_id"], payload_hash(item["body"]))
    receipts = ReceiptStore(
        [
            {
                "logical_id": item["logical_id"],
                "payload_hash": payload_hash(item["body"]),
                "attempt_id": f"{item['logical_id']}#recorded",
            }
            for item in case["receipts"]
        ]
    )
    attempts = set(case["attempts"])
    guarded = method in {"B3+", "B4"}
    sent: list[str] = []
    flags: list[str] = []
    plan_ns = 0
    for item in case["planned"]:
        logical_id = item["logical_id"]
        digest = payload_hash(item["body"])
        started = perf_counter_ns()
        if guarded:
            action = choose_action(
                logical_id,
                digest,
                receipts.find(logical_id),
                logical_id in attempts,
                idempotent_keys=case["idempotent_keys"],
                status_query=case["status_query"],
                query=service.query,
            )
        else:
            action = _replay_action()
        plan_ns += perf_counter_ns() - started
        kind = action["action"]
        if kind == "reuse":
            flags.append("reuse")
            continue
        if kind == "refuse_payload":
            flags.append("refuse")
            continue
        if kind == "unknown":
            flags.append("unknown")
            continue
        if kind == "persist_from_query":
            receipts.persist(logical_id, action["payload_hash"], f"{logical_id}#query")
            flags.append("persist")
            continue
        key = logical_id if action.get("use_key") else None
        result = service.submit(
            logical_id,
            digest,
            idempotency_key=key,
            attempt_id=f"{logical_id}#recovery",
        )
        sent.append(logical_id)
        if result in {"applied", "deduped"}:
            receipts.persist(logical_id, digest, f"{logical_id}#recovery")
            flags.append("sent")
        elif result == "duplicate":
            flags.append("duplicate")
        else:
            flags.append("conflict")
    safety: list[str] = []
    if "duplicate" in flags:
        safety.append("duplicate_effect")
    if "conflict" in flags:
        safety.append("conflicting_submit")
    if safety:
        outcome = "unsafe_pass"
        status = "unsafe_pass"
    elif "unknown" in flags:
        outcome = "unknown"
        status = "unknown"
    elif "refuse" in flags:
        outcome = "safe_refusal"
        status = "refused_external_payload_changed"
    else:
        outcome = "recovered"
        status = "recovered"
    candidate = {item["logical_id"]: item["body"] for item in case["planned"]}
    return _row(
        case,
        method,
        applicable=True,
        reason="effect_case",
        attempted=True,
        outcome=outcome,
        status=status,
        submits=len(service.submits),
        queries=service.queries,
        duplicate_applied=service.duplicate_applies,
        conflict_submits=service.conflict_submits,
        published=outcome == "recovered",
        candidate=candidate,
        receipts=receipts.entries,
        sent=sent,
        plan_ns=plan_ns,
        planner_used=False,
        safety=safety,
        hidden_apply_events=service.hidden_for_evaluator()["apply_events"],
        predicted=predicted,
    )


def _row(
    case: dict[str, Any],
    method: str,
    *,
    applicable: bool,
    reason: str,
    attempted: bool,
    outcome: str,
    status: str,
    submits: int,
    queries: int,
    duplicate_applied: int,
    conflict_submits: int,
    published: bool,
    candidate: dict[str, int],
    receipts: list[dict[str, str]],
    sent: list[str],
    plan_ns: int,
    planner_used: bool,
    safety: list[str],
    hidden_apply_events: int,
    predicted: str | None = None,
) -> dict[str, Any]:
    allowed = case["predicted"][method]
    evidence = case["evidence_sufficient"]
    unnecessary = (
        applicable
        and evidence
        and case["predicted"]["B4"] == "recovered"
        and outcome in {"safe_refusal", "unknown"}
        and method in {"B3+", "B4"}
    )
    reasonable = (
        applicable
        and not evidence
        and outcome in {"unknown", "safe_refusal"}
        and not safety
    )
    return {
        "suite": "effect",
        "case_id": case["case_id"],
        "method": method,
        "applicable": applicable,
        "applicability_reason": reason,
        "attempted": attempted,
        "outcome": outcome,
        "status": status,
        "safe_recovery": outcome == "recovered" and not safety,
        "unnecessary_refusal": unnecessary,
        "reasonable_stop": reasonable,
        "recomputed": list(sent),
        "recomputed_count": len(sent),
        "service_submits": submits,
        "service_queries": queries,
        "external_requests": submits + queries,
        "duplicate_applied": duplicate_applied,
        "conflict_submits": conflict_submits,
        "published": published,
        "published_values": dict(candidate) if published else None,
        "candidate_values": dict(candidate),
        "receipts": receipts,
        "safety_failures": safety,
        "planner_used": planner_used,
        "plan_ns": plan_ns,
        "evidence_sufficient": evidence,
        "predicted_outcome": predicted if predicted is not None else allowed,
        "hidden_apply_events": hidden_apply_events,
        "actual_model_calls": 0,
        "actual_token_usage": 0,
    }


def effect_cases() -> list[dict[str, Any]]:
    def case(
        case_id: str,
        *,
        idempotent_keys: bool,
        status_query: bool,
        query_available: bool,
        attempts: list[str],
        receipts: list[dict[str, Any]],
        applied: list[dict[str, Any]],
        planned: list[dict[str, Any]],
        evidence_sufficient: bool,
        predicted: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "case_id": case_id,
            "idempotent_keys": idempotent_keys,
            "status_query": status_query,
            "query_available": query_available,
            "attempts": attempts,
            "receipts": receipts,
            "applied": applied,
            "planned": planned,
            "evidence_sufficient": evidence_sufficient,
            "predicted": predicted,
        }

    guarded_same = {
        "B0": "unsafe_pass",
        "B1": "not_applicable",
        "B2": "not_applicable",
        "B3": "unsafe_pass",
        "B3+": "recovered",
        "B4": "recovered",
    }
    return [
        case(
            "A_not_started",
            idempotent_keys=False,
            status_query=False,
            query_available=True,
            attempts=[],
            receipts=[],
            applied=[],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=True,
            predicted={**guarded_same, "B0": "recovered", "B3": "recovered"},
        ),
        case(
            "B_response_lost",
            idempotent_keys=False,
            status_query=True,
            query_available=True,
            attempts=["mail"],
            receipts=[],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=True,
            predicted=guarded_same,
        ),
        case(
            "C_response_not_durable",
            idempotent_keys=False,
            status_query=False,
            query_available=True,
            attempts=["mail"],
            receipts=[],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=False,
            predicted={
                "B0": "unsafe_pass",
                "B1": "not_applicable",
                "B2": "not_applicable",
                "B3": "unsafe_pass",
                "B3+": "unknown",
                "B4": "unknown",
            },
        ),
        case(
            "D_receipt_persisted",
            idempotent_keys=False,
            status_query=False,
            query_available=True,
            attempts=["mail"],
            receipts=[{"logical_id": "mail", "body": 4}],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=True,
            predicted=guarded_same,
        ),
        case(
            "E_same_operation_same_payload",
            idempotent_keys=True,
            status_query=False,
            query_available=True,
            attempts=["mail"],
            receipts=[],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=True,
            predicted=guarded_same,
        ),
        case(
            "F_same_operation_new_payload",
            idempotent_keys=False,
            status_query=False,
            query_available=True,
            attempts=["mail"],
            receipts=[{"logical_id": "mail", "body": 4}],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 5}],
            evidence_sufficient=True,
            predicted={
                "B0": "unsafe_pass",
                "B1": "not_applicable",
                "B2": "not_applicable",
                "B3": "unsafe_pass",
                "B3+": "safe_refusal",
                "B4": "safe_refusal",
            },
        ),
        case(
            "G_different_operation_same_payload",
            idempotent_keys=False,
            status_query=False,
            query_available=True,
            attempts=["charge.1"],
            receipts=[{"logical_id": "charge.1", "body": 4}],
            applied=[{"logical_id": "charge.1", "body": 4}],
            planned=[
                {"logical_id": "charge.1", "body": 4},
                {"logical_id": "charge.2", "body": 4},
            ],
            evidence_sufficient=True,
            predicted=guarded_same,
        ),
        case(
            "H_query_unavailable",
            idempotent_keys=False,
            status_query=True,
            query_available=False,
            attempts=["mail"],
            receipts=[],
            applied=[{"logical_id": "mail", "body": 4}],
            planned=[{"logical_id": "mail", "body": 4}],
            evidence_sufficient=False,
            predicted={
                "B0": "unsafe_pass",
                "B1": "not_applicable",
                "B2": "not_applicable",
                "B3": "unsafe_pass",
                "B3+": "unknown",
                "B4": "unknown",
            },
        ),
    ]
