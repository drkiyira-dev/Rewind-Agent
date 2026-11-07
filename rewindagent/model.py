"""Controlled state, external ledger, and hash-chained log.

The ledger is not stored inside slot snapshots.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .canonical import semantic_hash

SCHEMA_VERSION = 1
MAX_ITERS = 4


class ContractError(Exception):
    pass


class Conflict(Exception):
    pass


class LogCorrupt(Exception):
    pass


@dataclass
class Slot:
    value: Any
    version: int
    producer: str
    value_hash: str


class State:
    def __init__(self) -> None:
        self.slots: dict[str, Slot] = {}

    def init(self, name: str, value: Any) -> None:
        self.slots[name] = Slot(value, 0, "genesis", semantic_hash(value))

    def copy(self) -> "State":
        cloned = State()
        cloned.slots = {
            name: Slot(slot.value, slot.version, slot.producer, slot.value_hash)
            for name, slot in self.slots.items()
        }
        return cloned

    def values(self) -> dict[str, Any]:
        return {name: slot.value for name, slot in self.slots.items()}

    def observe(self, name: str, reader: str) -> dict[str, Any]:
        if name not in self.slots:
            raise KeyError(name)
        slot = self.slots[name]
        return {
            "slot": name,
            "version": slot.version,
            "producer": slot.producer,
            "value_hash": slot.value_hash,
            "reader": reader,
            "value": slot.value,
        }

    def write(
        self,
        name: str,
        value: Any,
        producer: str,
        expected_base_hash: str | None = None,
    ) -> Slot:
        current = self.slots.get(name)
        current_hash = None if current is None else current.value_hash
        if expected_base_hash is not None and current_hash != expected_base_hash:
            raise Conflict(name)
        version = 0 if current is None else current.version + 1
        self.slots[name] = Slot(value, version, producer, semantic_hash(value))
        return self.slots[name]


class Ledger:
    def __init__(self) -> None:
        self.entries: list[dict[str, str]] = []

    def copy(self) -> "Ledger":
        cloned = Ledger()
        cloned.entries = [dict(entry) for entry in self.entries]
        return cloned

    def find(self, logical_id: str) -> dict[str, str] | None:
        for entry in self.entries:
            if entry["id"] == logical_id:
                return entry
        return None

    def deliver(self, logical_id: str, payload_hash: str) -> str:
        existing = self.find(logical_id)
        if existing is None:
            self.entries.append({"id": logical_id, "payload_hash": payload_hash})
            return "appended"
        if existing["payload_hash"] == payload_hash:
            return "suppressed"
        return "blocked"


def corrupt_value(value: Any) -> Any:
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1000
    raise TypeError(f"cannot corrupt {type(value).__name__}")


@dataclass(frozen=True)
class Tool:
    name: str
    impl_id: str
    fn: Callable[[list[Any]], tuple[Any, ...]]
    cost: int = 1
    effect: str = "none"
    escapes: bool = False
    undeclared_slot: str | None = None


def tool_set(values: list[Any], config: dict[str, Any] | None = None) -> tuple[Any, ...]:
    del values
    assert config is not None
    return (config["value"],)


def tool_inc(values: list[Any]) -> tuple[Any, ...]:
    return (values[0] + 1,)


def tool_dec(values: list[Any]) -> tuple[Any, ...]:
    return (values[0] - 1,)


def tool_add(values: list[Any]) -> tuple[Any, ...]:
    return (values[0] + values[1],)


def tool_is_pos(values: list[Any]) -> tuple[Any, ...]:
    return (values[0] > 0,)


def tool_spy(values: list[Any]) -> tuple[Any, ...]:
    return (values[0],)


def tool_const(values: list[Any]) -> tuple[Any, ...]:
    del values
    return (1,)


def default_tools() -> dict[str, Tool]:
    return {
        "set": Tool("set", "set.v1", lambda values: (values[0],)),
        "inc": Tool("inc", "inc.v1", tool_inc),
        "dec": Tool("dec", "dec.v1", tool_dec),
        "add": Tool("add", "add.v1", tool_add),
        "is_pos": Tool("is_pos", "is_pos.v1", tool_is_pos),
        "spy": Tool("spy", "spy.v1", tool_spy, undeclared_slot="hidden"),
        "escape": Tool("escape", "escape.v1", tool_const, escapes=True),
    }


class Log:
    def __init__(self) -> None:
        self.prev = "0" * 64
        self.records: list[dict[str, Any]] = []

    def append(self, body: dict[str, Any]) -> dict[str, Any]:
        digest = semantic_hash({"prev": self.prev, "body": body})
        record = {
            "schema_version": SCHEMA_VERSION,
            "prev": self.prev,
            "body": body,
            "hash": digest,
        }
        self.records.append(record)
        self.prev = digest
        return record

    def dump(self) -> str:
        return "".join(json.dumps(record, sort_keys=True) + "\n" for record in self.records)

    @staticmethod
    def load(text: str) -> "Log":
        log = Log()
        prev = "0" * 64
        for line in text.splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("schema_version") != SCHEMA_VERSION:
                raise LogCorrupt("schema_version")
            if record.get("prev") != prev:
                raise LogCorrupt("prev")
            digest = semantic_hash({"prev": record["prev"], "body": record["body"]})
            if digest != record.get("hash"):
                raise LogCorrupt("hash")
            log.records.append(record)
            prev = digest
        log.prev = prev
        return log


def snapshot(state: State, after: str) -> dict[str, Any]:
    slots = {
        name: {
            "value": slot.value,
            "version": slot.version,
            "producer": slot.producer,
            "value_hash": slot.value_hash,
        }
        for name, slot in state.slots.items()
    }
    body = {"after": after, "slots": slots}
    return {"body": body, "content_hash": semantic_hash(body)}


def load_snapshot(blob: dict[str, Any]) -> State:
    if semantic_hash(blob["body"]) != blob["content_hash"]:
        raise LogCorrupt("checkpoint")
    state = State()
    for name, raw in blob["body"]["slots"].items():
        if semantic_hash(raw["value"]) != raw["value_hash"]:
            raise LogCorrupt("slot")
        state.slots[name] = Slot(
            raw["value"], raw["version"], raw["producer"], raw["value_hash"]
        )
    return state
