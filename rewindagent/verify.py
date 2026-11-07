"""Runtime checks. This module does not know expected business answers."""

from __future__ import annotations

from typing import Any


def contaminates(events: list[dict[str, Any]], fault_id: str) -> set[str]:
    """Reverse closure over every recorded read, not the first reader only."""
    readers: dict[str, list[str]] = {}
    for event in events:
        for read in list(event.get("reads") or []) + list(event.get("control_reads") or []):
            readers.setdefault(read["producer"], []).append(event["instance_id"])
    seen: set[str] = set()
    stack = [fault_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(readers.get(current, []))
    return seen


def verify_reuse(
    events: list[dict[str, Any]],
    fault_id: str,
    reuse_ids: set[str],
    recomputed_hashes: dict[str, str],
) -> list[str]:
    """Reject a reuse set that still depends on a changed contaminated output.

    ``recomputed_hashes`` maps a recomputed producer to its new output hash.
    Absence of a producer from that map means the recorded output stands.
    """
    problems: list[str] = []
    known = {"genesis", "environment"}
    producers = set(known)
    for event in events:
        producers.add(event["instance_id"])
    closure = contaminates(events, fault_id) if fault_id else set()
    by_id = {event["instance_id"]: event for event in events}
    for event in events:
        for read in event.get("reads") or []:
            if read["producer"] not in producers and read["producer"] not in known:
                problems.append(f"dangling:{event['instance_id']}:{read['producer']}")
        if event["instance_id"] not in reuse_ids:
            continue
        if event.get("violation"):
            problems.append(f"reuse_violation:{event['instance_id']}")
        if event.get("escapes"):
            problems.append(f"reuse_escape:{event['instance_id']}")
        for read in list(event.get("reads") or []) + list(event.get("control_reads") or []):
            producer = read["producer"]
            if producer not in closure:
                continue
            new_hash = recomputed_hashes.get(producer)
            if new_hash is None:
                problems.append(f"reuse_of_unrecomputed_fault_reader:{event['instance_id']}")
                continue
            old = by_id.get(producer)
            old_hash = None if old is None else old.get("output_hash")
            if new_hash != old_hash or new_hash != read["value_hash"]:
                problems.append(f"reuse_stale_input:{event['instance_id']}")
    return problems
