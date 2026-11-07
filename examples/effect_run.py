"""Mixed pure-compute and one append effect. Not an LLM agent.

The service URL is an operator argument. The database does not choose it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rewindagent.api import ToolSpec
from rewindagent.durable import query_run, reconcile_run, resume_run, start_run
from rewindagent.effectsvc import SERVICE_ID, AppendClient


def _inc(values: list[object]) -> tuple[object, ...]:
    return (values[0] + 1,)


def _boom(values: list[object]) -> tuple[object, ...]:
    del values
    raise RuntimeError("persistent failure")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Durable task with one local append effect")
    parser.add_argument("command", choices=("start", "resume", "reconcile", "status"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--service-url")
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--stop-at")
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--now-ms", type=int)
    parser.add_argument("--boom-tail", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        result = query_run(args.store, args.run_id)
    else:
        if not args.service_url:
            parser.error("this command requires --service-url")
        tools = [ToolSpec("inc", _inc, impl_id="effect.inc.v1")]
        if args.boom_tail:
            tools.append(ToolSpec("boom", _boom, impl_id="effect.boom.v1"))
        services = {SERVICE_ID: AppendClient(args.service_url)}
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
                services=services,
                now_ms=args.now_ms,
                stop_at=args.stop_at,
                ready_path=args.ready,
            )
        elif args.command == "reconcile":
            result = reconcile_run(
                args.store,
                args.run_id,
                tools,
                services,
                now_ms=args.now_ms,
            )
        else:
            result = resume_run(
                args.store,
                args.run_id,
                tools,
                services=services,
                workflow=workflow,
                initial=initial,
                now_ms=args.now_ms,
                stop_at=args.stop_at,
                ready_path=args.ready,
            )
    sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
