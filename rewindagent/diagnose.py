"""Read-only view of an execution record. Does not run tools or load pickle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCHEMA = "rewindagent.execution_record"

_FIELDS = (
    "status",
    "published",
    "reason_code",
    "message",
    "recomputed",
    "reused",
    "abandoned",
    "operation_ids",
)


def render(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return f"status: unavailable\nmessage: invalid json ({exc.lineno}:{exc.colno})\n"
    if not isinstance(data, dict):
        return "status: unavailable\nschema: unavailable\nmessage: json value is not an object\n"
    lines = [_line("schema", data.get("schema", "unavailable"))]
    if data.get("schema") != SCHEMA:
        lines.append("message: file is not a rewindagent execution record; missing fields stay unavailable")
    for name in _FIELDS:
        lines.append(_line(name, data.get(name, "unavailable")))
    evidence = data.get("evidence")
    lines.append(_line("evidence", evidence if isinstance(evidence, dict) else "unavailable"))
    lines.append(_contract(data))
    lines.append(_unknown(data))
    return "\n".join(lines) + "\n"


def _line(name: str, value: Any) -> str:
    if value == "unavailable":
        shown = "unavailable"
    elif isinstance(value, list):
        shown = ", ".join(str(item) for item in value) if value else "(none)"
    elif isinstance(value, dict):
        shown = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        shown = str(value)
    return f"{name}: {shown}"


def _contract(data: dict[str, Any]) -> str:
    status = data.get("status")
    if status in {"safe_refusal", "unknown", "execution_failure", "not_applicable", "unsupported"}:
        code = data.get("reason_code", "unavailable")
        message = data.get("message", "unavailable")
        return f"contract: {code}; {message}"
    return "contract: unavailable"


def _unknown(data: dict[str, Any]) -> str:
    if data.get("status") != "unknown":
        return "unknown_operation: unavailable"
    evidence = data.get("evidence")
    if isinstance(evidence, dict) and evidence.get("logical_id"):
        return f"unknown_operation: {evidence['logical_id']}"
    operations = data.get("operation_ids")
    if isinstance(operations, list) and operations:
        return f"unknown_operation: {operations[0]}"
    return "unknown_operation: unavailable"
