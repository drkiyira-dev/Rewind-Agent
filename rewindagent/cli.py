"""Offline CLI. No network and no model calls."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .cases import hand_scenarios
from .engine import record, rerun
from .evaluate import run_suite
from .model import default_tools
from .round2 import run_round2

_FROZEN = (
    "results/eval.json",
    "results/eval-rerun.json",
    "results/round2.json",
)


def _frozen_result(path: Path) -> bool:
    root = Path(__file__).resolve().parents[1]
    try:
        target = path.resolve()
    except OSError:
        return False
    return target in {(root / item).resolve() for item in _FROZEN}


def console_main() -> None:
    raise SystemExit(main())


def demo() -> int:
    case = next(item for item in hand_scenarios() if item.case_id == "diamond")
    tools = default_tools()
    recording = record(case.workflow, case.initial, case.fault, tools)
    result = rerun("B4", case.workflow, recording, case.initial, tools=tools)
    print(f"status={result.status}")
    print(f"recomputed={result.recomputed}")
    print(f"reused={result.reused}")
    print(f"values={result.values}")
    for reason in result.reasons:
        if reason["decision"] in {"reuse", "recompute", "refuse", "stop"}:
            print(f"{reason['decision']}\t{reason['id']}\t{reason['why']}")
    return 0 if result.status == "recovered" else 1


def eval_command(out: Path) -> int:
    if _frozen_result(out):
        print(f"refusing to write frozen research result {out}", file=sys.stderr)
        return 3
    report = run_suite()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(f"wrote {out}")
    print(
        "attempts={attempts} b4_eligible_success={b4_eligible_success_count}/{b4_eligible_count} "
        "suffix_benefit={benefit_vs_suffix_on_diamond} "
        "same_as_incremental={pure_b4_recompute_equals_b3_on_every_eligible_case}".format(**summary)
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print(
            "usage: python -m rewindagent demo | eval --out <new-file> | "
            "round2 --out <new-file> | inspect <record.json> | "
            "durable run|resume|status|inspect ..."
        )
        return 0
    if args[0] == "demo":
        return demo()
    if args[0] == "eval":
        out = Path("results/eval.json")
        if "--out" in args:
            out = Path(args[args.index("--out") + 1])
        return eval_command(out)
    if args[0] == "round2":
        out = Path("results/round2.json")
        if "--out" in args:
            out = Path(args[args.index("--out") + 1])
        if _frozen_result(out):
            print(f"refusing to write frozen research result {out}", file=sys.stderr)
            return 3
        report = run_round2()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        summary = report["summary"]
        print(f"wrote {out}")
        print(
            "conclusion={conclusion} paired_differences={paired_difference_count} "
            "b4_unsafe={b4_unsafe_pass} b3_plus_unsafe={b3_plus_unsafe_pass} "
            "b4_unnecessary={b4_unnecessary_refusal}".format(**summary)
        )
        return 0
    if args[0] == "inspect":
        if len(args) != 2:
            print("usage: python -m rewindagent inspect <record.json>", file=sys.stderr)
            return 2
        from .diagnose import render

        source = Path(args[1])
        if not source.is_file():
            print(f"record not found: {source}", file=sys.stderr)
            return 2
        before = source.stat().st_mtime_ns
        sys.stdout.write(render(source))
        if source.stat().st_mtime_ns != before:
            print("inspect changed the record mtime", file=sys.stderr)
            return 1
        return 0
    if args[0] == "durable":
        return _durable_command(args[1:])
    if args[0] == "effect-serve":
        return _effect_serve(args[1:])
    print(f"unknown command {args[0]}", file=sys.stderr)
    return 2


def _load_operator_registry(module_name: str, attr: str):
    """Import only an operator-typed module path. Never call this with a database value."""
    import importlib
    import re

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*", module_name):
        raise ValueError("registry module")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", attr):
        raise ValueError("registry attr")
    module = importlib.import_module(module_name)
    value = getattr(module, attr)
    if not isinstance(value, list):
        raise ValueError("registry must be a list")
    return value


def _effect_serve(args: list[str]) -> int:
    from .effectsvc import serve

    def flag(name: str) -> str | None:
        if name not in args:
            return None
        index = args.index(name)
        return args[index + 1] if index + 1 < len(args) else None

    store = flag("--store")
    profile = flag("--profile")
    ready = flag("--ready")
    retention = flag("--retention-ms")
    if not store or not profile or not ready:
        print("usage: python -m rewindagent effect-serve --store <db> --profile P0|P1|P2|P3 --ready <file> [--retention-ms N]", file=sys.stderr)
        return 2
    serve(Path(store), profile, int(retention or "86400000"), Path(ready))
    return 0


def _durable_command(args: list[str]) -> int:
    from .durable import query_run, render_store, resume_run, start_run

    if not args or args[0] in {"-h", "--help"}:
        print(
            "usage: python -m rewindagent durable "
            "run|resume --store <db> --run-id <id> --registry-module <mod> --registry-attr <name> "
            "[--spec <workflow.json>] [--snapshot <input.json>] | "
            "status|inspect --store <db> --run-id <id>"
        )
        return 0
    command = args[0]
    if command not in {"run", "resume", "status", "inspect"}:
        print(f"unknown durable command {command}", file=sys.stderr)
        return 2

    def flag(name: str) -> str | None:
        if name not in args:
            return None
        index = args.index(name)
        if index + 1 >= len(args):
            return None
        return args[index + 1]

    store = flag("--store")
    run_id = flag("--run-id")
    if not store or not run_id:
        print("durable requires --store and --run-id", file=sys.stderr)
        return 2
    path = Path(store)
    if command == "inspect":
        before = path.stat().st_mtime_ns if path.exists() else None
        sys.stdout.write(render_store(path, run_id))
        if before is not None and path.stat().st_mtime_ns != before:
            print("inspect changed the store mtime", file=sys.stderr)
            return 1
        return 0
    if command == "status":
        print(json.dumps(query_run(path, run_id).to_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    module_name = flag("--registry-module")
    attr = flag("--registry-attr")
    if not module_name or not attr:
        print("run and resume require --registry-module and --registry-attr", file=sys.stderr)
        return 2
    try:
        tools = _load_operator_registry(module_name, attr)
    except Exception as exc:
        print(f"registry refused: {exc}", file=sys.stderr)
        return 2
    workflow = None
    initial = None
    spec = flag("--spec")
    snapshot = flag("--snapshot")
    if spec:
        workflow = json.loads(Path(spec).read_text(encoding="utf-8"))
    if snapshot:
        initial = json.loads(Path(snapshot).read_text(encoding="utf-8"))
    if command == "run":
        if workflow is None or initial is None:
            print("run requires --spec and --snapshot", file=sys.stderr)
            return 2
        result = start_run(path, run_id, workflow, initial, tools)
    else:
        result = resume_run(path, run_id, tools, workflow=workflow, initial=initial)
    print(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if result.status in {"published", "open"} else 1
