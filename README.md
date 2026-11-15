# RewindAgent 0.1.0

English | [简体中文](readme-CH.md)

An experimental Python recovery component. The research conclusion remains `engineering_packaging`. Phase 3A adds one bounded Responses tool loop. It is not a new agent framework, and later phases have not started.

The project provides four distinct capabilities:

- **In-process API:** `execute`, `recover`, and `resolve_effect`. Recovery records exist only within the current process.
- **Durable recovery for pure computation:** a single local SQLite task store lets a new process continue from committed steps after the previous process is killed with SIGKILL.
- **Bounded loopback effect recovery:** the task store and simulated append-service store are separate. The service listens only on `127.0.0.1`; receipts and queries determine whether recovery can continue, must refuse, or remains `unknown`.
- **Responses tool loop:** one non-streaming OpenAI Responses function call at a time. The model response is stored before the tool runs. The checked path uses a local protocol fixture, not a live model.

Check `status` and `published` to determine the task outcome. Exit code 0 only means the command returned normally. `unknown` is not success and does not establish that the external effect never occurred. Unpublished candidate values are not final results.

## Scope and research conclusion

The verified environment is macOS 26.6 arm64, Python 3.14.3, SQLite 3.53.4, local disk, and one active executor per store file. The core package uses only the Python standard library. The optional `responses` extra installs the official OpenAI SDK; the tested SDK is 3.46.0. Linux, Windows, other Python versions, power loss, distributed execution, live models, and real external services have not been verified. The `py3-none-any` wheel tag is not evidence of cross-platform validation.

Across the existing 42 controlled offline scenarios, B4 and B3+ (the baseline with conventional safety checks added) showed no differences in the prespecified paired behavior fields. Safety comes from their shared checks, not from the recovery planner. The default `recover(..., profile="safety")` uses the conventional safety path; `profile="experiment"` retains the historical planner. This project does not claim a new recovery-planning algorithm, proven global optimality, production exactly-once guarantees, real model-token savings, or safe automatic recovery for arbitrary third-party APIs. `actual_model_calls = 0`.

Tools must come from a trusted registry and obey the pure-function and controlled-access contracts. This is not a Python sandbox: undeclared disk, network, or environment-variable access is not automatically detected. The caller identifies the fault location. Public workflows consist of ordered steps; the historical interpreter limits controlled loops to four iterations.

A pure function killed after returning but before its result is committed may execute again. Persistent storage uses `journal_mode=DELETE`, `synchronous=FULL`, explicit transactions, and `fcntl.flock`. Recovery refuses to reuse a task when its input, workflow, or tool identity does not match.

Effect recovery applies only to the simulated `append.v1` service contract. There is no deduplication across runs: a new `run_id` defines a new caller scope. The client uses the persisted preparation time and retention period to stop unsafe retries; server-side TTL cleanup is not implemented. After an attempted send, a `not_found` query does not authorize another POST. The original request may still complete and can later be checked with `reconcile`. P2 supports queries but is not idempotent: two direct POSTs with the same identity and payload produce two effects. An identity key stored with an effect supports lookup; counting rows with keys does not establish deduplication.

See the [API documentation](docs/api.md), [phase 2A contract](docs/phase2a-contract.md), [phase 2B contract](docs/phase2b-contract.md), and [phase 3A contract](docs/phase3a-contract.md). The original research definitions are in the [evaluation plan](docs/evaluation-plan.md) and [round 2 expectations](docs/round2-expectations.md).

## Recovery boundary maintenance

Recovery now rejects receipts for another logical operation, even when the payload is identical. SQLite filenames containing `#`, `?`, or `%` refer to the exact requested file. The append client accepts confirmations only from HTTP 200; HTTP 409 remains a submission conflict, and malformed or failed responses remain unavailable. Idempotent retries recheck the current time after network waits and immediately before sending. At the retention deadline, or when the clock is earlier than preparation, recovery remains `unknown`. An explicit `now_ms` is a deterministic test override, not a production clock.

These fixes preserve the public API and storage format. The original research interpreter and frozen evidence are unchanged.

## Tests and offline experiments

Run from the repository root:

```bash
python3 -W always::ResourceWarning -m unittest discover -s tests -v
python3 -m rewindagent demo
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/rewind-demo.XXXXXX")"
python3 -m rewindagent eval --out "$run_dir/eval.json"
python3 -m rewindagent round2 --out "$run_dir/round2.json"
```

The current suite contains 148 tests. With the optional Responses dependencies installed, all 148 pass with no skips. Without them, 23 SDK integration tests are explicitly skipped; the core and dependency-free boundary tests still run. The historical handoff contained 82 tests. Tests require permission to listen on loopback. Test outputs use separate temporary directories by default. To retain them under a chosen parent directory, set `REWIND_TEST_OUTPUT`; each run still creates a fresh subdirectory. The old `PHASE2A_WRITE` and `PHASE2B_WRITE` switches that overwrote historical evidence have been removed.

The owner chose to keep all seven frozen original evidence files local. They are absent from the public repository and distribution packages. A local workspace containing those originals can additionally run:

```bash
python3 -m unittest tests.history_audit -v
```

This module contains three historical audits and fails if the original evidence is missing. It is separate from ordinary functional tests and does not substitute synthetic data for historical evidence. Compared with the earliest frozen eval output, the current output differs in timing, adds `planner_used` and `published` fields, and has non-B4 `closure` diagnostic differences. The historical audit still checks B4's closure and the prespecified semantic fields individually. The differences from the earliest output must not be described as timing-only.

`eval` and `round2` refuse to overwrite the three frozen research-output paths in the source tree. Use a separate output directory for new runs.

## Build and local installation

The following commands reuse the `run_dir` created above. Run them in the same terminal, from the repository root. Installing build tools may access PyPI; installing the existing wheel explicitly disables package-index access and dependency resolution.

```bash
python3 -m venv "$run_dir/build-env"
"$run_dir/build-env/bin/python" -m pip install \
  build==1.6.1 setuptools==84.2.1 packaging==26.3 pyproject_hooks==1.3.3
"$run_dir/build-env/bin/python" -m build --no-isolation --outdir "$run_dir/dist" .
python3 -m venv "$run_dir/install-env"
"$run_dir/install-env/bin/python" -m pip install \
  --no-index --no-deps --disable-pip-version-check \
  "$run_dir/dist/rewindagent-0.1.0-py3-none-any.whl"
py="$run_dir/install-env/bin/python"
(cd "$run_dir" && env -u PYTHONPATH "$py" -c 'import rewindagent; print(rewindagent.__file__)')
(cd "$run_dir" && env -u PYTHONPATH "$py" -m rewindagent demo)
```

The verified handoff build used the versions above. Its tool-installation logs showed cached wheels; the build used `--no-isolation` with already installed tools. Offline builds on a fresh machine without a cache have not been verified. The declared minimum setuptools version is 77 to support SPDX license metadata. The core runtime has no third-party dependencies; the optional Responses loop uses the extra described below.

The wheel contains the package and standard metadata. The sdist also contains tests, examples, and documentation, including both README languages. Databases, logs, original results, virtual environments, and local build copies are not distributed. Do not set `PYTHONPATH` to the original source directory when verifying an independent installation.

## In-process and pure-computation examples

Continue using the installed interpreter and temporary directory above. Example and input paths are relative to the repository root:

```bash
"$py" examples/branch_recover.py \
  --snapshot examples/data/snapshot.json --out "$run_dir/example" --mode compute
"$py" examples/branch_recover.py --out "$run_dir/example" --mode unknown
"$py" -m rewindagent inspect "$run_dir/example/unknown.json"
"$py" examples/durable_run.py start \
  --store "$run_dir/task.sqlite" --run-id demo \
  --spec examples/data/durable_workflow.json --snapshot examples/data/snapshot.json
"$py" examples/durable_run.py resume --store "$run_dir/task.sqlite" --run-id demo
"$py" -m rewindagent durable inspect --store "$run_dir/task.sqlite" --run-id demo
```

The in-process compute example recomputes `left` and `join`, reuses `right`, and publishes `total=9`. The unknown example makes zero submissions and does not publish. The durable pure-computation example publishes `total=9`; repeated resume calls do not invoke the tools again. `inspect` parses records without executing text stored in them.

## Loopback effect example

Run in the same terminal, from the repository root. The service starts in the background; the commands below stop only the PID created by this terminal:

```bash
"$py" -m rewindagent effect-serve \
  --store "$run_dir/append.sqlite" --profile P0 \
  --ready "$run_dir/append-ready.json" --retention-ms 60000 \
  >"$run_dir/service.stdout" 2>"$run_dir/service.stderr" &
service_pid=$!
trap 'kill "$service_pid" 2>/dev/null; wait "$service_pid" 2>/dev/null' EXIT
service_url="$("$py" - "$run_dir/append-ready.json" <<'PY'
import json, pathlib, sys, time
ready = pathlib.Path(sys.argv[1])
for _ in range(100):
    if ready.exists():
        print(json.loads(ready.read_text())["base_url"])
        break
    time.sleep(0.1)
else:
    raise SystemExit("service did not become ready")
PY
)"
"$py" examples/effect_run.py start \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url" \
  --spec examples/data/effect_workflow.json --snapshot examples/data/effect_snapshot.json
"$py" examples/effect_run.py resume \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url"
"$py" examples/effect_run.py reconcile \
  --store "$run_dir/effect-run.sqlite" --run-id demo --service-url "$service_url"
"$py" examples/effect_run.py status --store "$run_dir/effect-run.sqlite" --run-id demo
kill "$service_pid"
wait "$service_pid" 2>/dev/null || true
trap - EXIT
```

P0 and P1 support idempotent submission; P0 and P2 support queries. The normal example publishes `n2=3` and `report=4`. `reconcile` only queries and adopts evidence; it does not issue a new business POST.

After inspecting the temporary results, remove only the directory created for this run. This also removes the example virtual environments and local packages:

```bash
rm -rf -- "$run_dir"
unset run_dir py service_pid service_url
```

## Responses tool loop

This loop accepts one ordinary Responses `function_call`, stores that call, runs it through the existing pure-computation or loopback effect kernel, stores the tool result, and only then requests the next model turn. `unknown` stops the loop. `reconcile` checks an existing effect and does not call the model or send a new business POST. `status` only reads the session.

Recovery also checks the saved call's arguments and child identity before executing or adopting it. Inconsistent records return `record_corrupt` without changing the evidence. If a crash leaves a SQLite rollback journal that requires writing, `status` and `inspect` return `unknown / store_recovery_required`; use `resume` to recover under the executor lock. SIGKILL tests cover uncommitted request intents, accepted calls, effect outputs, final responses and publications, including journal rollback and lock contention. See the [boundary coverage and limits](docs/phase3a-contract.md#additional-recovery-boundaries).

The local fixture is synthetic. It is not a live model transcript. `store=False` selects local continuation; it is not a claim that a provider retains nothing. Live mode is implemented only as a refusal: this phase does not call a paid model.

From the repository root, install the local checkout and its optional extra in a fresh environment. This dependency installation may access PyPI; the example itself only connects to loopback:

```bash
run_dir="$(mktemp -d "${TMPDIR:-/tmp}/rewind-agent.XXXXXX")"
python3 -m venv "$run_dir/env"
py="$run_dir/env/bin/python"
"$py" -m pip install '.[responses]'
"$py" -m rewindagent effect-serve \
  --store "$run_dir/effect.sqlite" --profile P0 \
  --ready "$run_dir/effect-ready.json" --retention-ms 60000 &
effect_pid=$!
"$py" -m rewindagent protocol-serve \
  --fixture examples/data/agent_fixture.json \
  --store "$run_dir/protocol.sqlite" \
  --ready "$run_dir/protocol-ready.json" &
protocol_pid=$!
cleanup_3a() {
  kill "$effect_pid" "$protocol_pid" 2>/dev/null || true
  wait "$effect_pid" "$protocol_pid" 2>/dev/null || true
}
trap cleanup_3a EXIT
"$py" - "$run_dir/effect-ready.json" "$run_dir/protocol-ready.json" <<'PY_READY'
import json, pathlib, sys, time
paths = [pathlib.Path(name) for name in sys.argv[1:]]
for _ in range(200):
    try:
        [json.loads(path.read_text()) for path in paths]
        break
    except (FileNotFoundError, json.JSONDecodeError):
        time.sleep(0.05)
else:
    raise SystemExit("local services did not become ready within 10 seconds")
PY_READY
effect_url="$( "$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"])' "$run_dir/effect-ready.json" )"
protocol_url="$( "$py" -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"])' "$run_dir/protocol-ready.json" )"
"$py" examples/responses_agent.py start \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --snapshot examples/data/agent_snapshot.json \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
"$py" examples/responses_agent.py status --store "$run_dir/agent.sqlite" --run-id demo
"$py" examples/responses_agent.py resume \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
"$py" examples/responses_agent.py reconcile \
  --store "$run_dir/agent.sqlite" --run-id demo \
  --protocol-url "$protocol_url" --effect-url "$effect_url"
cleanup_3a
trap - EXIT
```

The normal local run publishes `summary.count=2`, `summary.total=7`, and one append effect. Budgets are `--max-model-turns`, `--max-tool-calls`, and `--max-model-attempts`. They are stored and are not reset by a restart. The extra pins the verified `openai==3.46.0` and `httpx2==2.16.0` versions. No package-index release is required: installation above uses this checkout. Budget flags apply only to `start`; resume uses the persisted budgets and rejects budget overrides. Without the extra, core commands still run and the tool loop reports the missing optional adapter.

The session coordinator lock remains held while the durable kernel runs a child. An existing child result is checked against the accepted workflow, input and tool identity before adoption. Repeated `call_id` values in a new response are refused; an effect can be reused through a distinct call ID only when the authorized slot and payload agree. Pure tools in this subset take no model arguments and operate on the imported snapshot. Opaque reasoning is requested for local continuation and is hidden by `inspect`.

After examining the results, remove only this example's temporary directory with `rm -rf -- "$run_dir"`. No real model calls, provider token measurements, package releases or deployments were performed.

## Delivery and license

Licensed under the [GNU General Public License, version 3](LICENSE), with SPDX identifier `GPL-3.0-only`. Third-party reading records remain in [NOTICE.md](NOTICE.md). The handoff did not publish a PyPI package, create a GitHub Release, or deploy a service.

The handoff and verification scope is documented in [docs/CODEX_HANDOFF.md](docs/CODEX_HANDOFF.md). Repository files and retained local materials are classified in `artifacts/handoff-manifest.json`. There is no CI configuration or passing-CI badge. No MCP, email, payment, or production-database integration has been added. Phase 3A is the bounded Responses loop above. Live model validation was not run.
