"""Bounded operator capabilities for the stateless restricted Slack turn."""

from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import unquote, urlsplit, urlunsplit

from gateway.client_context_policy import (
    ContextDenied, _reject_json_constant, _unique_object, require,
)

_GROUPS = {"public_web_search": "web", "clarify": "clarify", "todo_list": "todo"}
_STATUSES = ("pending", "in_progress", "completed")
_PRIVATE_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home", ".test", ".invalid", ".onion")
_LOCAL_PATH = re.compile(r"(?:file:|~[/\\]|/(?:Users|home|private|tmp|var|etc)/|[A-Za-z]:\\)", re.I)


def _text(value, maximum):
    require(isinstance(value, str) and 0 < len(value) <= maximum and value.strip())
    require(not any(ord(c) < 32 or ord(c) == 127 for c in value))
    return value.strip()


def _public_url(value):
    value = _text(value, 2048)
    require(not re.search(r"[\s\\<>]", value))
    parsed = urlsplit(value)
    require(parsed.scheme in {"http", "https"} and parsed.hostname)
    require(parsed.username is None and parsed.password is None)
    require(parsed.port in {None, 80, 443})
    host = parsed.hostname.lower().rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        require("." in host and not host.endswith(_PRIVATE_SUFFIXES))
        require(re.fullmatch(r"[a-z0-9.-]+", host) and re.search(r"[a-z]", host.rsplit(".", 1)[-1]))
        require(all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in host.split(".")))
    else:
        require(address.is_global)
    # Search results are citations only: never fetch a returned URL or retain
    # query/fragment parameters that may carry account or credential material.
    require(not _LOCAL_PATH.search(unquote(parsed.path)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def validate(name: str, args: str | dict) -> dict:
    """Reject unknown tool names, duplicate JSON keys, and every extra field."""
    require(name in _GROUPS)
    try:
        if isinstance(args, str):
            require(len(args) <= 8192)
            args = json.loads(args, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
        require(type(args) is dict)
        if name == "public_web_search":
            require(set(args) == {"query"})
            query = _text(args["query"], 500)
            require(not _LOCAL_PATH.search(query))
            for target in re.findall(r"https?://\S+", query, re.I):
                _public_url(target)
            for target in re.findall(r"\bsite:([^\s]+)", query, re.I):
                _public_url(target if "://" in target else "https://" + target)
            return {"query": query}
        if name == "clarify":
            require({"question"} <= set(args) <= {"question", "choices"})
            question = _text(args["question"], 500)
            choices = args.get("choices", [])
            require(type(choices) is list and len(choices) <= 5)
            return {"question": question, "choices": [_text(choice, 120) for choice in choices]}
        require(set(args) <= {"items"})
        if "items" not in args:
            return {}
        require(type(args["items"]) is list and len(args["items"]) <= 12)
        entries = []
        for entry in args["items"]:
            require(type(entry) is dict and set(entry) == {"text", "status"})
            require(entry["status"] in _STATUSES)
            entries.append({"text": _text(entry["text"], 240), "status": entry["status"]})
        return {"items": entries}
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError) as exc:
        raise ContextDenied("restricted tool request denied") from exc


def _result_text(value, maximum):
    from agent.redact import redact_sensitive_text

    if not isinstance(value, str) or _LOCAL_PATH.search(value):
        return ""
    value = " ".join(value.split())[:maximum]
    return redact_sensitive_text(value, force=True)


def _search(query):
    from tools.web_tools import web_search_tool

    raw = web_search_tool(query=query, limit=5)
    require(isinstance(raw, str) and len(raw) <= 131072)
    response = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    require(type(response) is dict and response.get("success") is True)
    data = response.get("data")
    require(type(data) is dict and type(data.get("web")) is list)
    results = []
    for row in data["web"][:5]:
        if type(row) is not dict:
            continue
        try:
            url = _public_url(row.get("url"))
            if _result_text(url, 2048) != url:
                continue
        except (ValueError, TypeError):
            continue
        results.append({"title": _result_text(row.get("title"), 200), "url": url,
                        "description": _result_text(row.get("description"), 800)})
    return {"results": results}


def execute(name: str, args: str, guard, todos: list) -> str:
    """Run one fixed capability; permission revocation escapes to abort the turn."""
    guard()
    updated = None
    try:
        value = validate(name, args)
        if name == "public_web_search":
            result = _search(value["query"])
        elif name == "clarify":
            result = value
        else:
            updated = value.get("items")
            result = {"items": updated if updated is not None else validate(name, {"items": todos})["items"]}
    except ContextDenied:
        result = {"error": "restricted tool request unavailable"}
    except Exception:
        result = {"error": "restricted tool unavailable"}
    guard()
    if updated is not None:
        todos[:] = updated
    return json.dumps(result, ensure_ascii=False)


def schemas(grants: frozenset[str]) -> list:
    """Advertise only the groups explicitly granted to this restricted turn."""
    def text(size):
        return {"type": "string", "minLength": 1, "maxLength": size}

    definitions = {
        "public_web_search": (
            "Search public web metadata only; no page fetching or private sources.",
            {"query": text(500)}, ["query"],
        ),
        "clarify": (
            "Prepare a short question and optional choices for the final reply; no interactive callback.",
            {"question": text(500), "choices": {"type": "array", "maxItems": 5, "items": text(120)}},
            ["question"],
        ),
        "todo_list": (
            "Read or replace this turn's task list. It is discarded after this reply.",
            {"items": {"type": "array", "maxItems": 12, "items": {
                "type": "object", "properties": {"text": text(240), "status": {"type": "string", "enum": list(_STATUSES)}},
                "required": ["text", "status"], "additionalProperties": False,
            }}}, [],
        ),
    }
    return [{"type": "function", "function": {"name": name, "description": description,
             "parameters": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False}}}
            for name, (description, properties, required) in definitions.items() if _GROUPS[name] in grants]
