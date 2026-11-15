"""OpenAI Responses function-calling subset.

The official SDK performs the HTTP request. Local mode uses a fixed non-secret
credential and a loopback base URL. This module does not read the process
environment for an API key and does not call a live provider by itself.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

LOCAL_TEST_CREDENTIAL = "rewind-local-test-credential"
SUPPORTED_OUTPUT_TYPES = frozenset({"message", "function_call", "reasoning"})
INSTALL_HINT = "可选适配器未安装。安装方式：python -m pip install 'rewindagent[responses]'"


@dataclass(frozen=True)
class ResponsesConfig:
    """Caller-supplied provider settings. Live mode is not executed in this phase."""

    mode: str
    model: str
    base_url: str
    timeout_s: float = 5.0
    live_authorized: bool = False
    api_key: str | None = None


def endpoint_class(base_url: str) -> str:
    try:
        parts = urlsplit(base_url)
        if (parts.scheme == "http" and parts.hostname in {"127.0.0.1", "localhost"}
                and parts.port and parts.username is None and parts.password is None
                and not parts.query and not parts.fragment):
            return "loopback"
    except (ValueError, TypeError):
        pass
    return "other"


def config_problem(config: ResponsesConfig) -> str | None:
    if config.mode == "live":
        return "live_not_authorized"
    if config.mode != "local_protocol_fixture":
        return "unsupported_provider_mode"
    if config.live_authorized or config.api_key:
        return "local_mode_rejects_live_credentials"
    if endpoint_class(config.base_url) != "loopback":
        return "endpoint_not_loopback"
    if not isinstance(config.model, str) or not config.model or len(config.model) > 80:
        return "model_required"
    if (type(config.timeout_s) not in {int, float} or not math.isfinite(config.timeout_s)
            or not 0 < config.timeout_s <= 30):
        return "timeout_out_of_range"
    return None


def create_response(
    config: ResponsesConfig,
    *,
    instructions: str,
    input_items: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a plain response dict, or an error dict. Never follows a redirect."""
    problem = config_problem(config)
    if problem:
        return {"ok": False, "error": problem}
    try:
        import httpx2
        from openai import OpenAI
    except ImportError:
        return {"ok": False, "error": "dependency_missing", "message": INSTALL_HINT}
    http_client = httpx2.Client(
        trust_env=False,
        follow_redirects=False,
        timeout=config.timeout_s,
    )
    try:
        client = OpenAI(
            api_key=LOCAL_TEST_CREDENTIAL,
            base_url=config.base_url,
            timeout=config.timeout_s,
            max_retries=0,
            http_client=http_client,
        )
        response = client.responses.create(
            model=config.model,
            instructions=instructions,
            input=input_items,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
            stream=False,
            store=False,
            include=["reasoning.encrypted_content"],
            timeout=config.timeout_s,
        )
    except Exception as exc:  # SDK and transport errors stay on this attempt.
        return {"ok": False, "error": "transport", "detail_type": type(exc).__name__}
    finally:
        http_client.close()
    try:
        dumped = response.model_dump(mode="json")
    except Exception as exc:
        return {"ok": False, "error": "transport", "detail_type": type(exc).__name__}
    return {"ok": True, "response": dumped}


def parse_arguments(text: str, schema: dict[str, Any]) -> dict[str, Any] | str:
    if not isinstance(text, str) or len(text) > 8000:
        return "invalid_arguments"
    try:
        value = json.loads(text, object_pairs_hook=_pairs)
    except (json.JSONDecodeError, ValueError, RecursionError):
        return "invalid_arguments"
    if _check_schema(value, schema):
        return "invalid_arguments"
    return value


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in items:
        if key in found:
            raise ValueError("duplicate key")
        found[key] = value
    return found


def _check_schema(value: Any, schema: dict[str, Any]) -> str | None:
    if schema.get("type") != "object" or schema.get("additionalProperties") is not False:
        return "unsupported_schema"
    if not isinstance(value, dict):
        return "invalid_arguments"
    properties = schema.get("properties", {})
    required = schema.get("required", [])
    if not isinstance(properties, dict) or not isinstance(required, list):
        return "unsupported_schema"
    if set(value) - set(properties):
        return "invalid_arguments"
    for name in required:
        if name not in value:
            return "invalid_arguments"
    for name, item in value.items():
        rule = properties[name]
        if not isinstance(rule, dict) or _check_value(item, rule):
            return "invalid_arguments"
    return None


def _check_value(value: Any, rule: dict[str, Any]) -> str | None:
    kind = rule.get("type")
    if kind == "string":
        if not isinstance(value, str):
            return "invalid_arguments"
        if len(value) > int(rule.get("maxLength", 200)):
            return "invalid_arguments"
        return None
    if kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return "invalid_arguments"
        if "minimum" in rule and value < int(rule["minimum"]):
            return "invalid_arguments"
        if "maximum" in rule and value > int(rule["maximum"]):
            return "invalid_arguments"
        return None
    if kind == "boolean":
        if not isinstance(value, bool):
            return "invalid_arguments"
        return None
    return "unsupported_schema"


def public_tool_schema(name: str, description: str, parameters: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "parameters": parameters,
        "strict": False,
    }
