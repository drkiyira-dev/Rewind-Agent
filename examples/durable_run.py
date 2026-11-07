"""Trusted caller for a pure durable task. Not an LLM agent.

Tools are defined in this file. The database never chooses a module to import.
The fault point, when present, is only a test synchronization hook.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from rewindagent.api import ToolSpec
from rewindagent.durable import query_run, resume_run, start_run


def _watch(name: str, fn, obs: Path | None):
    def wrapped(values: list[object]) -> tuple[object, ...]:
        if obs is not None:
            with obs.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps({"tool": name, "values": values, "pid": os.getpid()}) + "\n"
                )
        return fn(values)

    return wrapped


def _registry(obs: Path | None, *, omit: str | None, impl_suffix: str, boom: bool) -> list[ToolSpec]:
    def inc(values: list[object]) -> tuple[object, ...]:
        return (values[0] + 1,)

    def add(values: list[object]) -> tuple[object, ...]:
        return (values[0] + values[1],)

    def fail(values: list[object]) -> tuple[object, ...]:
        del values
        raise RuntimeError("persistent failure")

    tools = []
    if omit != "inc":
        tools.append(ToolSpec("inc", _watch("inc", inc, obs), impl_id=f"durable.inc.v1{impl_suffix}"))
    if omit != "add":
        tools.append(ToolSpec("add", _watch("add", add, obs), impl_id=f"durable.add.v1{impl_suffix}"))
    if boom:
        tools.append(ToolSpec("boom", _watch("boom", fail, obs), impl_id="durable.boom.v1"))
    return tools


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run or resume one pure RewindAgent task")
    parser.add_argument("command", choices=("start", "resume", "status"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--stop-at")
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--obs", type=Path)
    parser.add_argument("--omit-tool")
    parser.add_argument("--impl-suffix", default="")
    parser.add_argument("--boom", action="store_true")
    parser.add_argument("--boundary-timeout", type=float, default=45)
    args = parser.parse_args(argv)
    if args.command == "status":
        result = query_run(args.store, args.run_id)
    else:
        tools = _registry(args.obs, omit=args.omit_tool, impl_suffix=args.impl_suffix, boom=args.boom)
        workflow = json.loads(args.spec.read_text(encoding="utf-8")) if args.spec else None
        initial = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
        if args.command == "start":
            if workflow is None or initial is None:
                parser.error("start requires --spec and --snapshot")
            result = start_run(
                args.store,
                args.run_id,
                workflow,
                initial,
                tools,
                stop_at=args.stop_at,
                ready_path=args.ready,
                boundary_timeout_s=args.boundary_timeout,
            )
        else:
            result = resume_run(
                args.store,
                args.run_id,
                tools,
                workflow=workflow,
                initial=initial,
                stop_at=args.stop_at,
                ready_path=args.ready,
                boundary_timeout_s=args.boundary_timeout,
            )
    sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
