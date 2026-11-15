"""One local Responses tool loop. Not a live model and not a general agent framework.

The snapshot is read once at this boundary and passed in as data.
Tools do not open the snapshot file again. Tool order comes from the provider
response, which in the local example is a synthetic fixture.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rewindagent.agent import AgentBudgets, AgentTool, ResponsesConfig, resume_agent, start_agent
from rewindagent.agent import inspect_agent, query_agent, reconcile_agent
from rewindagent.effectsvc import SERVICE_ID, AppendClient


def _read(values: list[object]) -> tuple[object, ...]:
    return (values[0],)


def _summarize(values: list[object]) -> tuple[object, ...]:
    snapshot = values[0]
    records = snapshot["records"]
    summary = {"count": len(records), "total": sum(int(item["n"]) for item in records)}
    return (summary,)


def tools() -> list[AgentTool]:
    empty = {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }
    record = {
        "type": "object",
        "properties": {"summary_ref": {"type": "string", "maxLength": 80}},
        "required": ["summary_ref"],
        "additionalProperties": False,
    }
    return [
        AgentTool("read_snapshot", "Read the imported snapshot.", "pure", "agent.read.v1", empty, _read),
        AgentTool("summarize", "Sum the imported snapshot.", "pure", "agent.summarize.v1", empty, _summarize),
        AgentTool(
            "record_summary",
            "Record the summary once in the authorized slot.",
            "effect",
            "agent.record.v1",
            record,
            None,
            SERVICE_ID,
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local Responses tool loop")
    parser.add_argument("command", choices=("start", "resume", "reconcile", "status", "inspect", "retry-model"))
    parser.add_argument("--store", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--protocol-url")
    parser.add_argument("--effect-url")
    parser.add_argument("--model", default="fixture-responses-1")
    parser.add_argument("--stop-at")
    parser.add_argument("--ready", type=Path)
    parser.add_argument("--max-model-turns", type=int)
    parser.add_argument("--max-tool-calls", type=int)
    parser.add_argument("--max-model-attempts", type=int)
    args = parser.parse_args(argv)
    if args.command != "start" and any(value is not None for value in
            (args.max_model_turns, args.max_tool_calls, args.max_model_attempts)):
        parser.error("budgets are fixed at start; omit budget flags when resuming or inspecting")
    if args.command in {"status", "inspect"}:
        viewed = inspect_agent(args.store, args.run_id) if args.command == "inspect" else query_agent(args.store, args.run_id).to_dict()
        sys.stdout.write(json.dumps(viewed, ensure_ascii=False, sort_keys=True) + "\n")
        return 0
    if not args.protocol_url or not args.effect_url:
        parser.error("this command requires --protocol-url and --effect-url")
    provider = ResponsesConfig(mode="local_protocol_fixture", model=args.model, base_url=args.protocol_url, timeout_s=5)
    defaults = AgentBudgets()
    budgets = AgentBudgets(
        defaults.max_model_turns if args.max_model_turns is None else args.max_model_turns,
        defaults.max_tool_calls if args.max_tool_calls is None else args.max_tool_calls,
        defaults.max_model_attempts if args.max_model_attempts is None else args.max_model_attempts,
    )
    services = {SERVICE_ID: AppendClient(args.effect_url)}
    registry = tools()
    if args.command == "start":
        if args.snapshot is None:
            parser.error("start requires --snapshot")
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8"))
        result = start_agent(
            args.store,
            args.run_id,
            "Summarize the imported records and record that summary once.",
            snapshot,
            registry,
            provider,
            instructions="Use only the registered tools. A sentence is not a record.",
            effect_slot="primary_record",
            services=services,
            budgets=budgets,
            stop_at=args.stop_at,
            ready_path=args.ready,
        )
    elif args.command == "resume":
        snapshot = json.loads(args.snapshot.read_text(encoding="utf-8")) if args.snapshot else None
        result = resume_agent(
            args.store,
            args.run_id,
            registry,
            provider,
            services=services,
            snapshot=snapshot,
            stop_at=args.stop_at,
            ready_path=args.ready,
        )
    elif args.command == "retry-model":
        result = resume_agent(
            args.store,
            args.run_id,
            registry,
            provider,
            services=services,
            authorize_model_retry=True,
            stop_at=args.stop_at,
            ready_path=args.ready,
        )
    else:
        result = reconcile_agent(args.store, args.run_id, registry, provider, services=services)
    sys.stdout.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
