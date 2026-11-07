"""Interpreter. B4 rebuilds from genesis; cache hits are the only reuse path."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter_ns
from typing import Any, Callable

from .canonical import semantic_hash
from .model import (
    MAX_ITERS,
    Conflict,
    Ledger,
    Log,
    State,
    Tool,
    corrupt_value,
    default_tools,
    load_snapshot,
    snapshot,
)
from .verify import contaminates, verify_reuse


def _instance(step_id: str, iteration: int | None) -> str:
    if iteration is None:
        return step_id
    return f"{step_id}#{iteration}"


def _invoke(tool: Tool, values: list[Any], config: dict[str, Any]) -> tuple[Any, ...]:
    if tool.name == "set":
        return (config["value"],)
    return tool.fn(values)


@dataclass
class Fault:
    instance_id: str
    mode: str
    bad_hash: str | None = None

    def copy(self) -> "Fault":
        return Fault(self.instance_id, self.mode, self.bad_hash)


@dataclass
class Run:
    status: str
    values: dict[str, Any]
    events: list[dict[str, Any]]
    reused: list[str]
    recomputed: list[str]
    prefix_skipped: list[str]
    abandoned: list[str]
    new_events: list[str]
    reasons: list[dict[str, str]]
    ledger_entries: list[dict[str, str]]
    external_attempts: int
    duplicate_applied: int
    safety_failures: list[str]
    simulated_cost: int
    plan_ns: int
    total_ns: int
    used_checkpoint: bool
    closure: list[str]
    order: list[str] = field(default_factory=list)
    cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    marked: list[dict[str, Any]] = field(default_factory=list)
    snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    genesis_blob: dict[str, Any] | None = None
    fault: Fault | None = None
    log_text: str = ""
    candidate_values: dict[str, Any] = field(default_factory=dict)
    published_values: dict[str, Any] | None = None
    published: bool = False
    planner_used: bool = False


class Engine:
    def __init__(
        self,
        *,
        mode: str,
        tools: dict[str, Tool] | None = None,
        cache: dict[str, dict[str, Any]] | None = None,
        ledger: Ledger | None = None,
        fault: Fault | None = None,
        honor_contract: bool = True,
        skip: set[str] | None = None,
        restore: dict[str, Any] | None = None,
    ) -> None:
        self.mode = mode
        self.tools = tools or default_tools()
        self.cache = {} if cache is None else cache
        self.ledger = Ledger() if ledger is None else ledger
        self.fault = fault
        self.honor_contract = honor_contract
        self.effect_guard = False
        self.refuse_escapes = False
        self.skip = set() if skip is None else set(skip)
        self.state = State()
        self.log = Log()
        self.events: list[dict[str, Any]] = []
        self.reused: list[str] = []
        self.recomputed: list[str] = []
        self.prefix_skipped: list[str] = []
        self.reasons: list[dict[str, str]] = []
        self.external_attempts = 0
        self.duplicate_applied = 0
        self.safety_failures: list[str] = []
        self.simulated_cost = 0
        self.status = "ok"
        self.ctrl_i = 0
        self.marked: list[dict[str, Any]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self.order: list[str] = []
        self.used_checkpoint = restore is not None
        self.closure: set[str] = set()
        self.plan_ns = 0
        self.planner_used = False
        self.genesis_blob: dict[str, Any] | None = None
        self._initial: dict[str, Any] = {}
        if restore is not None:
            self.state = load_snapshot(restore)

    def begin(self, initial: dict[str, Any]) -> None:
        self._initial = dict(initial)
        if not self.used_checkpoint:
            for name, value in initial.items():
                self.state.init(name, value)
        self.genesis_blob = snapshot(self.state, "genesis")

    def mutate(self, updates: dict[str, Any]) -> None:
        for name, value in updates.items():
            self.state.write(name, value, "environment")

    def refuse(self, status: str, why: str) -> None:
        if self.status == "ok":
            self.status = status
            self.reasons.append({"id": "", "decision": "refuse", "why": why})

    def checkpoint(self) -> None:
        after = self.order[-1] if self.order else "genesis"
        self.marked.append(snapshot(self.state, after))

    def control_bool(self, slot: str) -> bool:
        return bool(self._control(slot, "bool"))

    def control_int(self, slot: str) -> int:
        value = self._control(slot, "int")
        if isinstance(value, bool) or not isinstance(value, int):
            self.refuse("refused_unbounded_control", "loop count is not an integer slot")
            return 0
        if value > MAX_ITERS:
            self.refuse(
                "refused_unbounded_control",
                f"loop count {value} exceeds bound {MAX_ITERS}",
            )
            return 0
        if value < 0:
            self.refuse("refused_unbounded_control", "negative loop count")
            return 0
        return value

    def _control(self, slot: str, kind: str) -> Any:
        inst = f"control:{slot}:{self.ctrl_i}"
        self.ctrl_i += 1
        if self.status != "ok":
            return 0
        if inst in self.skip:
            self.prefix_skipped.append(inst)
            return self.state.slots[slot].value
        full = self.state.observe(slot, inst)
        read = self._public_read(full)
        event = self._event(inst, "control", reads=[read], writes=[], outputs=())
        event["control_kind"] = kind
        self._commit_event(event, write=False)
        return full["value"]

    def call(
        self,
        step_id: str,
        tool_name: str,
        reads: list[str],
        writes: list[str],
        *,
        iteration: int | None = None,
        config: dict[str, Any] | None = None,
    ) -> None:
        if self.status != "ok":
            return
        if iteration is not None and iteration >= MAX_ITERS:
            self.refuse("refused_unbounded_control", "iteration index past bound")
            return
        inst = _instance(step_id, iteration)
        if inst in self.skip:
            self.prefix_skipped.append(inst)
            return
        tool = self.tools[tool_name]
        config = {} if config is None else dict(config)
        observed = [self.state.observe(slot, inst) for slot in reads]
        violation = False
        if tool.undeclared_slot is not None:
            observed.append(self.state.observe(tool.undeclared_slot, inst))
            violation = True
        input_hashes = [item["value_hash"] for item in observed[: len(reads)]]
        key = semantic_hash(
            {
                "inst": inst,
                "impl": tool.impl_id,
                "config": config,
                "inputs": input_hashes,
            }
        )
        use_fault = self.fault is not None and inst == self.fault.instance_id and (
            self.mode == "original" or self.fault.mode == "persistent"
        )
        poisoned = (
            self.fault is not None
            and inst == self.fault.instance_id
            and self.mode != "original"
            and self.fault.mode == "removed"
        )
        entry = self.cache.get(key)
        if entry is not None and (entry.get("violation") or entry.get("escapes")) and self.honor_contract:
            entry = None
        if use_fault or poisoned or tool.effect != "none" or tool.escapes:
            entry = None
        if entry is not None:
            try:
                self._apply_outputs(inst, reads, writes, entry["outputs"], observed)
            except Conflict:
                self.status = "write_conflict"
                self.safety_failures.append("write_conflict")
                self.reasons.append(
                    {
                        "id": inst,
                        "decision": "refuse",
                        "why": "cached write base did not match current slot",
                    }
                )
                return
            self.reused.append(inst)
            self.reasons.append(
                {
                    "id": inst,
                    "decision": "reuse",
                    "why": "instance, implementation, config, and input hashes match a pure cache entry",
                }
            )
            event = self._event(
                inst,
                "tool",
                reads=[self._public_read(item) for item in observed],
                writes=writes,
                outputs=tuple(entry["outputs"]),
                tool=tool,
                config=config,
                violation=bool(entry.get("violation")),
                decision="reuse",
            )
            self._commit_event(event, write=False)
            return
        if tool.escapes and self.mode != "original" and self.honor_contract:
            self.refuse(
                "refused_unobservable_dependency",
                "tool may leave the controlled state API; selective recovery stops",
            )
            return
        values = [item["value"] for item in observed]
        if tool.undeclared_slot is not None:
            outputs = _invoke(tool, [values[-1]], config)
        else:
            outputs = _invoke(tool, values[: len(reads)], config)
        if use_fault:
            outputs = tuple(corrupt_value(item) for item in outputs)
            bad = semantic_hash(list(outputs))
            if self.mode == "original" and self.fault is not None:
                self.fault.bad_hash = bad
            if (
                self.mode != "original"
                and self.fault is not None
                and self.fault.mode == "persistent"
                and bad == self.fault.bad_hash
            ):
                self.recomputed.append(inst)
                self.simulated_cost += tool.cost
                self.status = "persistent_fault"
                self.reasons.append(
                    {
                        "id": inst,
                        "decision": "stop",
                        "why": "one recompute reproduced the recorded bad output; not retrying",
                    }
                )
                self._commit_event(
                    self._event(
                        inst,
                        "tool",
                        reads=[self._public_read(item) for item in observed],
                        writes=[],
                        outputs=outputs,
                        tool=tool,
                        config=config,
                        violation=violation,
                        decision="stop",
                    ),
                    write=False,
                )
                return
        self._apply_outputs(inst, reads, writes, outputs, observed)
        self.cache[key] = {
            "instance_id": inst,
            "outputs": list(outputs),
            "violation": violation,
            "escapes": tool.escapes,
            "impl_id": tool.impl_id,
        }
        self.recomputed.append(inst)
        self.simulated_cost += tool.cost
        self.reasons.append(
            {
                "id": inst,
                "decision": "recompute",
                "why": "cache miss, fault instance, contract violation, or implementation change",
            }
        )
        event = self._event(
            inst,
            "tool",
            reads=[self._public_read(item) for item in observed],
            writes=writes,
            outputs=outputs,
            tool=tool,
            config=config,
            violation=violation,
            decision="recompute",
        )
        self._commit_event(event, write=False)

    def send(self, step_id: str, reads: list[str]) -> None:
        if self.status != "ok":
            return
        inst = step_id
        if inst in self.skip:
            self.prefix_skipped.append(inst)
            return
        observed = [self.state.observe(slot, inst) for slot in reads]
        payload = semantic_hash([item["value"] for item in observed])
        existing = self.ledger.find(inst)
        if self.mode != "original" and self.effect_guard:
            if existing is None:
                self.refuse(
                    "refused_new_external_effect",
                    "recovery would append a new external delivery",
                )
                self._commit_event(
                    self._event(
                        inst,
                        "effect",
                        reads=[self._public_read(item) for item in observed],
                        writes=[],
                        outputs=(payload,),
                        decision="refuse",
                    ),
                    write=False,
                )
                return
            if existing["payload_hash"] != payload:
                self.refuse(
                    "refused_external_payload_changed",
                    "delivered payload differs; ledger is not rewritten or compensated",
                )
                self._commit_event(
                    self._event(
                        inst,
                        "effect",
                        reads=[self._public_read(item) for item in observed],
                        writes=[],
                        outputs=(payload,),
                        decision="refuse",
                    ),
                    write=False,
                )
                return
            self.reused.append(inst)
            self.reasons.append(
                {
                    "id": inst,
                    "decision": "reuse",
                    "why": "irreversible delivery already recorded; send was not called again",
                }
            )
            event = self._event(
                inst,
                "effect",
                reads=[self._public_read(item) for item in observed],
                writes=[],
                outputs=(payload,),
                decision="reuse",
            )
            self._commit_event(event, write=False)
            return
        self.external_attempts += 1
        result = self.ledger.deliver(inst, payload)
        if self.mode != "original" and result == "appended":
            self.duplicate_applied += 1
            self.safety_failures.append("new_external_effect_applied")
        if result == "blocked":
            self.refuse(
                "external_blocked",
                "ledger refused a second payload and left the original entry in place",
            )
            return
        decision = "record" if self.mode == "original" else "effect_replay"
        self.recomputed.append(inst)
        self.reasons.append(
            {
                "id": inst,
                "decision": decision,
                "why": f"ledger returned {result}",
            }
        )
        event = self._event(
            inst,
            "effect",
            reads=[self._public_read(item) for item in observed],
            writes=[],
            outputs=(payload,),
            decision=decision,
        )
        self._commit_event(event, write=False)

    def finish(self, original_order: list[str] | None = None, started: int | None = None) -> Run:
        if self.mode == "original":
            if self.status == "ok":
                self.status = "recorded"
        elif self.status == "ok" and self.external_attempts:
            self.status = "effect_replay_suppressed"
        elif self.status == "ok":
            self.status = "recovered"
        visited = set(self.order)
        original = list(original_order or [])
        abandoned = [item for item in original if item not in visited and item not in self.prefix_skipped]
        new_events = [item for item in self.order if item not in set(original)]
        total = 0 if started is None else perf_counter_ns() - started
        candidate_values = self.state.values()
        publish = self.mode == "original" or self.status == "recovered"
        published_values = dict(candidate_values) if publish else None
        return Run(
            status=self.status,
            values=dict(candidate_values) if publish else None,
            events=self.events,
            reused=list(self.reused),
            recomputed=list(self.recomputed),
            prefix_skipped=list(self.prefix_skipped),
            abandoned=abandoned,
            new_events=new_events,
            reasons=self.reasons,
            ledger_entries=[dict(entry) for entry in self.ledger.entries],
            external_attempts=self.external_attempts,
            duplicate_applied=self.duplicate_applied,
            safety_failures=list(self.safety_failures),
            simulated_cost=self.simulated_cost,
            plan_ns=self.plan_ns,
            total_ns=total,
            used_checkpoint=self.used_checkpoint,
            closure=sorted(self.closure),
            order=list(self.order),
            cache=self.cache,
            marked=self.marked,
            snapshots=self.snapshots,
            genesis_blob=self.genesis_blob,
            fault=self.fault,
            log_text=self.log.dump(),
            candidate_values=candidate_values,
            published_values=published_values,
            published=publish,
            planner_used=self.planner_used,
        )

    def _apply_outputs(
        self,
        inst: str,
        reads: list[str],
        writes: list[str],
        outputs: tuple[Any, ...] | list[Any],
        observed: list[dict[str, Any]],
    ) -> None:
        observed_by_slot = {item["slot"]: item for item in observed}
        for slot in reads:
            if self.state.slots[slot].value_hash != observed_by_slot[slot]["value_hash"]:
                raise Conflict(slot)
        for slot, value in zip(writes, outputs):
            base = observed_by_slot[slot]["value_hash"] if slot in observed_by_slot else None
            self.state.write(slot, value, inst, expected_base_hash=base)

    def _event(
        self,
        inst: str,
        kind: str,
        *,
        reads: list[dict[str, Any]],
        writes: list[str],
        outputs: tuple[Any, ...],
        tool: Tool | None = None,
        config: dict[str, Any] | None = None,
        violation: bool = False,
        decision: str = "",
    ) -> dict[str, Any]:
        write_records = []
        ww = []
        for slot in writes:
            current = self.state.slots[slot]
            write_records.append(
                {
                    "slot": slot,
                    "version": current.version,
                    "producer": current.producer,
                    "value_hash": current.value_hash,
                }
            )
        read_slots = {item["slot"] for item in reads}
        # order constraints are descriptive; contaminates() does not walk them.
        for slot in writes:
            producers = [item["producer"] for item in reads if item["slot"] == slot]
            if slot not in read_slots:
                ww.append({"slot": slot, "kind": "write_write_or_blind", "instance": inst})
            elif producers:
                ww.append({"slot": slot, "kind": "read_modify_write", "instance": inst, "read_producer": producers[0]})
        return {
            "instance_id": inst,
            "kind": kind,
            "reads": reads,
            "control_reads": [],
            "writes": write_records,
            "order_ww": ww,
            "output_hash": semantic_hash(list(outputs)),
            "impl_id": None if tool is None else tool.impl_id,
            "config": config or {},
            "violation": violation,
            "escapes": bool(tool and tool.escapes),
            "decision": decision,
        }

    def _commit_event(self, event: dict[str, Any], write: bool) -> None:
        del write
        self.events.append(event)
        self.order.append(event["instance_id"])
        self.snapshots[event["instance_id"]] = snapshot(self.state, event["instance_id"])
        self.log.append({"type": "event", "event": event})

    @staticmethod
    def _public_read(read: dict[str, Any]) -> dict[str, Any]:
        return {
            "slot": read["slot"],
            "version": read["version"],
            "producer": read["producer"],
            "value_hash": read["value_hash"],
            "reader": read["reader"],
        }


def _copy_cache(cache: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    copied: dict[str, dict[str, Any]] = {}
    for key, value in cache.items():
        entry = {}
        for name, item in value.items():
            entry[name] = list(item) if isinstance(item, list) else item
        copied[key] = entry
    return copied


def record(
    workflow: Callable[[Engine], None],
    initial: dict[str, Any],
    fault: Fault | None,
    tools: dict[str, Tool] | None = None,
) -> Run:
    engine = Engine(
        mode="original",
        tools=tools,
        fault=None if fault is None else fault.copy(),
    )
    engine.begin(initial)
    workflow(engine)
    return engine.finish()


def _prepare(
    recording: Run,
    initial: dict[str, Any],
    *,
    honor_contract: bool,
    tools: dict[str, Tool],
    restore: dict[str, Any] | None,
    skip: set[str],
    mutate: dict[str, Any] | None,
    effect_guard: bool,
    refuse_escapes: bool,
    use_planner: bool,
    source_events: list[dict[str, Any]],
) -> Engine:
    fault = None if recording.fault is None else recording.fault.copy()
    engine = Engine(
        mode="recovery",
        tools=tools,
        cache=_copy_cache(recording.cache),
        ledger=Ledger(),
        fault=fault,
        honor_contract=honor_contract,
        skip=skip,
        restore=restore,
    )
    engine.effect_guard = effect_guard
    engine.refuse_escapes = refuse_escapes
    engine.ledger.entries = [dict(entry) for entry in recording.ledger_entries]
    if restore is None:
        engine.begin(initial)
    else:
        engine.genesis_blob = recording.genesis_blob
        engine._initial = dict(initial)
    if mutate:
        engine.mutate(mutate)
    if use_planner and fault is not None:
        started_plan = perf_counter_ns()
        engine.closure = contaminates(source_events, fault.instance_id)
        engine.plan_ns = perf_counter_ns() - started_plan
        engine.planner_used = True
    if refuse_escapes and any(event.get("escapes") for event in source_events):
        engine.refuse(
            "refused_unobservable_dependency",
            "original trace contains a tool that may bypass the controlled API",
        )
    return engine


def _stopped(recording: Run, status: str, why: str, started: int) -> Run:
    """Refusal before interpretation. Keeps the external ledger and does not mark a recovery."""
    engine = Engine(mode="recovery", ledger=Ledger())
    engine.ledger.entries = [dict(entry) for entry in recording.ledger_entries]
    engine.status = status
    engine.reasons.append({"id": "", "decision": "refuse", "why": why})
    return engine.finish(None, started)


def rerun(
    method: str,
    workflow: Callable[[Engine], None],
    recording: Run,
    initial: dict[str, Any],
    *,
    tools: dict[str, Tool] | None = None,
    mutate: dict[str, Any] | None = None,
) -> Run:
    tools = default_tools() if tools is None else tools
    started = perf_counter_ns()
    restore = None
    skip: set[str] = set()
    use_cache = method in {"B3", "B3+", "B4"}
    use_planner = method == "B4"
    if method == "B1":
        blob = _latest_checkpoint_before_fault(recording)
        if blob is None:
            return _stopped(
                recording,
                "no_checkpoint",
                "no explicit checkpoint strictly before the fault",
                started,
            )
        try:
            load_snapshot(blob)
        except Exception:
            return _stopped(
                recording,
                "checkpoint_corrupt",
                "checkpoint content hash failed; ledger was left unchanged",
                started,
            )
        restore = blob
        skip = _skip_through(recording.order, blob["body"]["after"])
    if method == "B2":
        if recording.fault is None:
            return _stopped(
                recording,
                "not_applicable",
                "suffix replay needs a known fault instance",
                started,
            )
        restore, skip = _prefix_before(recording, initial)
    source = recording
    if not use_cache:
        source = Run(
            status=recording.status,
            values={},
            events=recording.events,
            reused=[],
            recomputed=[],
            prefix_skipped=[],
            abandoned=[],
            new_events=[],
            reasons=[],
            ledger_entries=recording.ledger_entries,
            external_attempts=0,
            duplicate_applied=0,
            safety_failures=[],
            simulated_cost=0,
            plan_ns=0,
            total_ns=0,
            used_checkpoint=False,
            closure=[],
            cache={},
            marked=recording.marked,
            snapshots=recording.snapshots,
            genesis_blob=recording.genesis_blob,
            fault=recording.fault,
            order=recording.order,
        )
    engine = _prepare(
        source,
        initial,
        honor_contract=method in {"B3+", "B4"},
        tools=tools,
        restore=restore,
        skip=skip,
        mutate=mutate,
        effect_guard=method in {"B3+", "B4"},
        refuse_escapes=method in {"B3+", "B4"},
        use_planner=use_planner,
        source_events=recording.events,
    )
    if engine.status == "ok":
        workflow(engine)
        if engine.status == "ok" and use_planner and engine.fault is not None:
            problems = verify_reuse(
                recording.events,
                engine.fault.instance_id,
                set(engine.reused),
                {},
            )
            if problems:
                engine.status = "verifier_rejected"
                engine.safety_failures.extend(problems)
    return engine.finish(recording.order, started)


def _skip_through(order: list[str], after: str) -> set[str]:
    if after == "genesis":
        return set()
    if after not in order:
        return set()
    index = order.index(after)
    return set(order[: index + 1])


def _latest_checkpoint_before_fault(recording: Run) -> dict[str, Any] | None:
    if recording.fault is None or recording.fault.instance_id not in recording.order:
        return None
    limit = recording.order.index(recording.fault.instance_id)
    chosen = None
    chosen_at = -1
    for blob in recording.marked:
        after = blob["body"]["after"]
        if after == "genesis":
            position = -1
        elif after in recording.order:
            position = recording.order.index(after)
        else:
            continue
        if position < limit and position >= chosen_at:
            chosen = blob
            chosen_at = position
    return chosen


def _prefix_before(recording: Run, initial: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    assert recording.fault is not None
    index = recording.order.index(recording.fault.instance_id)
    if index == 0:
        state = State()
        for name, value in initial.items():
            state.init(name, value)
        return snapshot(state, "genesis"), set()
    previous = recording.order[index - 1]
    return recording.snapshots[previous], set(recording.order[:index])
