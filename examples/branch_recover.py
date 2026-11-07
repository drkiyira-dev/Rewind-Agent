"""Scripted two-branch recovery. Not an LLM agent.

The snapshot is read here, at the import boundary, and passed in as slot values.
Tools do not open files. The fault id is chosen by this script, not discovered.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

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


def _inc(values: list[object]) -> tuple[object, ...]:
    return (values[0] + 1,)


def _add(values: list[object]) -> tuple[object, ...]:
    return (values[0] + values[1],)


def _tools() -> list[ToolSpec]:
    return [
        ToolSpec("inc", _inc, impl_id="example.inc.v1"),
        ToolSpec("add", _add, impl_id="example.add.v1"),
    ]


def _workflow() -> Workflow:
    return Workflow(
        steps=(
            Step("left", "inc", ("left_in",), ("left_out",)),
            Step("right", "inc", ("right_in",), ("right_out",)),
            Step("join", "add", ("left_out", "right_out"), ("total",)),
        )
    )


def _load_snapshot(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise SystemExit("snapshot must be a JSON object")
    return loaded


def _write(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_compute(snapshot: Path, out: Path) -> int:
    initial = _load_snapshot(snapshot)
    recorded = execute(_workflow(), _tools(), initial, fault=FaultSpec("left", "removed"))
    repaired = recover(recorded)
    _write(out / "compute.json", repaired.to_dict())
    print(f"recorded_status={recorded.status} published={recorded.published}")
    print(f"status={repaired.status} published={repaired.published} reason={repaired.reason_code}")
    print(f"recomputed={repaired.recomputed}")
    print(f"reused={repaired.reused}")
    print(f"values={repaired.values}")
    if repaired.status != "recovered" or not repaired.published:
        return 1
    if repaired.recomputed != ["left", "join"] or repaired.reused != ["right"]:
        return 1
    if repaired.values != {
        "left_in": 2,
        "right_in": 5,
        "left_out": 3,
        "right_out": 6,
        "total": 9,
    }:
        return 1
    return 0


def run_unknown(out: Path) -> int:
    simulator = LocalEffectSimulator(
        idempotent_keys=False,
        status_query=True,
        query_available=False,
    )
    result = resolve_effect(
        EffectView(
            "notify",
            4,
            attempt_started=True,
            receipt=None,
            idempotent_keys=False,
            status_query=True,
        ),
        simulator,
    )
    _write(out / "unknown.json", result.to_dict())
    print(f"status={result.status} published={result.published} reason={result.reason_code}")
    print(f"message={result.message}")
    print(f"evidence={result.evidence}")
    if result.status != "unknown" or result.published or result.values is not None:
        return 1
    if result.evidence.get("service_submits") != 0:
        return 1
    if result.evidence.get("query_result") != {"status": "unavailable", "payload_hash": None}:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RewindAgent scripted branch example")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("compute", "unknown"), default="compute")
    args = parser.parse_args(argv)
    if args.mode == "unknown":
        return run_unknown(args.out)
    if args.snapshot is None:
        parser.error("--snapshot is required for compute")
    return run_compute(args.snapshot, args.out)


if __name__ == "__main__":
    raise SystemExit(main())
