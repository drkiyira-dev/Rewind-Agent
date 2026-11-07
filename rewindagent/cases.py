"""Fixed scenarios. Expected slot values were written before any eval run."""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from .engine import Engine, Fault


Workflow = Callable[[Engine], None]


@dataclass
class Scenario:
    case_id: str
    family: str
    workflow: Workflow
    initial: dict[str, Any]
    expected_b4: str
    success_eligible: bool
    pure: bool
    fault: Fault | None = None
    mutate: dict[str, Any] | None = None
    expected_values: dict[str, Any] | None = None
    graph: dict[str, Any] | None = None
    change_impl: bool = False
    notes: str = ""
    tags: list[str] = field(default_factory=list)


def _linear(engine: Engine) -> None:
    engine.call("A", "set", [], ["a"], config={"value": 1})
    engine.checkpoint()
    engine.call("B", "inc", ["a"], ["b"])
    engine.call("C", "inc", ["b"], ["c"])


def _diamond(engine: Engine) -> None:
    engine.call("P", "set", [], ["p"], config={"value": 2})
    engine.checkpoint()
    engine.call("S", "set", [], ["s"], config={"value": 5})
    engine.call("L", "inc", ["p"], ["l"])
    engine.call("R", "inc", ["s"], ["r"])
    engine.call("J", "add", ["l", "r"], ["j"])


def _multi_reader(engine: Engine) -> None:
    engine.call("W", "set", [], ["x"], config={"value": 4})
    engine.call("A", "inc", ["x"], ["a"])
    engine.call("B", "inc", ["x"], ["b"])


def _multi_version(engine: Engine) -> None:
    engine.call("W1", "set", [], ["x"], config={"value": 1})
    engine.call("R1", "inc", ["x"], ["a"])
    engine.call("W2", "set", [], ["x"], config={"value": 7})
    engine.call("R2", "inc", ["x"], ["b"])


def _control(engine: Engine) -> None:
    engine.call("flag", "is_pos", ["n"], ["flag"])
    if engine.control_bool("flag"):
        engine.call("A", "inc", ["n"], ["out"])
    else:
        engine.call("B", "dec", ["n"], ["out"])


def _loop(engine: Engine) -> None:
    engine.call("init", "set", [], ["s"], config={"value": 1})
    for index in range(3):
        engine.call("body", "inc", ["s"], ["s"], iteration=index)


def _new_event(engine: Engine) -> None:
    engine.call("base", "inc", ["n"], ["acc"])
    engine.call("flag", "is_pos", ["n"], ["flag"])
    if engine.control_bool("flag"):
        engine.call("extra", "inc", ["acc"], ["acc"])


def _undeclared(engine: Engine) -> None:
    engine.call("spy", "spy", [], ["out"])


def _escape(engine: Engine) -> None:
    engine.call("gone", "escape", [], ["out"])


def _impl(engine: Engine) -> None:
    engine.call("A", "set", [], ["a"], config={"value": 3})
    engine.call("B", "inc", ["a"], ["b"])


def _unbounded(engine: Engine) -> None:
    engine.call("N", "set", [], ["n"], config={"value": 9})
    count = engine.control_int("n")
    for index in range(count):
        engine.call("body", "inc", ["n"], ["n"], iteration=index)


def _send_same(engine: Engine) -> None:
    engine.call("msg", "set", [], ["m"], config={"value": 4})
    engine.call("noise", "inc", ["seed"], ["noise"])
    engine.send("mail", ["m"])


def _send_changed(engine: Engine) -> None:
    engine.call("msg", "inc", ["seed"], ["m"])
    engine.send("mail", ["m"])


def _persistent(engine: Engine) -> None:
    engine.call("A", "set", [], ["a"], config={"value": 1})
    engine.call("B", "inc", ["a"], ["b"])
    engine.call("C", "inc", ["b"], ["c"])


def _random_workflow(graph: dict[str, Any]) -> Workflow:
    def workflow(engine: Engine) -> None:
        for node in graph["nodes"]:
            config = {"value": node["const"]} if node["kind"] == "set" else {}
            engine.call(node["id"], node["kind"], list(node["reads"]), [node["write"]], config=config)

    return workflow


def _random_graph(seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    nodes = []
    for index in range(5):
        if index == 0 or rng.random() < 0.25:
            kind = "set"
            reads: list[str] = []
            const = rng.randint(1, 9)
        elif rng.random() < 0.5 or index == 1:
            kind = "inc"
            parent = rng.randrange(index)
            reads = [f"v{parent}"]
            const = 0
        else:
            kind = "add"
            left = rng.randrange(index)
            right = rng.randrange(index)
            reads = [f"v{left}", f"v{right}"]
            const = 0
        nodes.append(
            {
                "id": f"n{index}",
                "kind": kind,
                "reads": reads,
                "write": f"v{index}",
                "const": const,
            }
        )
    fault_at = rng.randrange(5)
    return {"seed": seed, "nodes": nodes, "fault_id": f"n{fault_at}"}


def hand_scenarios() -> list[Scenario]:
    return [
        Scenario(
            "linear",
            "chain",
            _linear,
            {},
            "recovered",
            True,
            True,
            Fault("B", "removed"),
            expected_values={"a": 1, "b": 2, "c": 3},
            notes="selective recovery has no shorter recompute set than the suffix",
        ),
        Scenario(
            "diamond",
            "branch",
            _diamond,
            {},
            "recovered",
            True,
            True,
            Fault("L", "removed"),
            expected_values={"p": 2, "s": 5, "l": 3, "r": 6, "j": 9},
            notes="R is after the fault in trace order and does not read L",
        ),
        Scenario(
            "multi_reader",
            "read_fanout",
            _multi_reader,
            {},
            "recovered",
            True,
            True,
            Fault("W", "removed"),
            expected_values={"x": 4, "a": 5, "b": 5},
        ),
        Scenario(
            "multi_version",
            "versions",
            _multi_version,
            {},
            "recovered",
            True,
            True,
            Fault("W1", "removed"),
            expected_values={"x": 7, "a": 2, "b": 8},
        ),
        Scenario(
            "control_flip",
            "control",
            _control,
            {"n": -3},
            "recovered",
            True,
            True,
            Fault("flag", "removed"),
            expected_values={"n": -3, "flag": False, "out": -4},
        ),
        Scenario(
            "loop_carried",
            "loop",
            _loop,
            {},
            "recovered",
            True,
            True,
            Fault("body#0", "removed"),
            expected_values={"s": 4},
        ),
        Scenario(
            "new_event",
            "control",
            _new_event,
            {"n": 1},
            "recovered",
            True,
            True,
            Fault("flag", "removed"),
            expected_values={"n": 1, "acc": 3, "flag": True},
        ),
        Scenario(
            "undeclared",
            "contract",
            _undeclared,
            {"hidden": 1},
            "recovered",
            True,
            False,
            mutate={"hidden": 2},
            expected_values={"hidden": 2, "out": 2},
            notes="API sees the extra read, so B4 recomputes; this is not an unobservable escape",
        ),
        Scenario(
            "escape",
            "contract",
            _escape,
            {},
            "refused_unobservable_dependency",
            False,
            False,
        ),
        Scenario(
            "impl_change",
            "cache_key",
            _impl,
            {},
            "recovered",
            True,
            True,
            expected_values={"a": 3, "b": 4},
            change_impl=True,
        ),
        Scenario(
            "unbounded",
            "control",
            _unbounded,
            {},
            "refused_unbounded_control",
            False,
            False,
        ),
        Scenario(
            "irreversible_same",
            "effect",
            _send_same,
            {"seed": 1},
            "recovered",
            True,
            False,
            Fault("noise", "removed"),
            expected_values={"seed": 1, "m": 4, "noise": 2},
        ),
        Scenario(
            "irreversible_changed",
            "effect",
            _send_changed,
            {"seed": 1},
            "refused_external_payload_changed",
            False,
            False,
            Fault("msg", "removed"),
        ),
        Scenario(
            "persistent",
            "fault",
            _persistent,
            {},
            "persistent_fault",
            False,
            False,
            Fault("B", "persistent"),
        ),
    ]


def random_scenarios() -> list[Scenario]:
    found = []
    for offset in range(20):
        seed = 20261106 + offset
        graph = _random_graph(seed)
        found.append(
            Scenario(
                f"random_{offset:02d}",
                "random_pure",
                _random_workflow(graph),
                {},
                "recovered",
                True,
                True,
                Fault(graph["fault_id"], "removed"),
                graph=graph,
            )
        )
    return found


def all_scenarios() -> list[Scenario]:
    return hand_scenarios() + random_scenarios()
