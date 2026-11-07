"""Arithmetic reference for random graphs. Does not import the engine."""

from __future__ import annotations

from typing import Any


def evaluate_graph(graph: dict[str, Any]) -> dict[str, int]:
    values: dict[str, int] = {}
    for node in graph["nodes"]:
        kind = node["kind"]
        if kind == "set":
            values[node["write"]] = node["const"]
        elif kind == "inc":
            values[node["write"]] = values[node["reads"][0]] + 1
        elif kind == "add":
            values[node["write"]] = values[node["reads"][0]] + values[node["reads"][1]]
        else:
            raise ValueError(kind)
    return values
