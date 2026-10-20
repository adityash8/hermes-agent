"""CookUnity-only stateless Slack lane. Rhiz ordinary grants elsewhere stay retired."""
from __future__ import annotations

import asyncio
import json
import threading
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

from gateway.client_context_policy import fields, require
from gateway import client_context_cookunity as cookunity

USER = "U0AF9Q2EZQV"
WORKSPACE = "T07MMEX5658"
CODEWORDS = "C09PF4QGQMC"
MAX_CALLS = 6
TIMEOUT = 90.0
SYSTEM = cookunity.SYSTEM
_issued: weakref.WeakSet = weakref.WeakSet()


def selected(source) -> bool:
    # Fence the destination even for privileged callers or malformed workspaces.
    # Analysts cannot use this restoration to enter legacy DMs or other channels.
    return (getattr(getattr(source, "platform", None), "value", getattr(source, "platform", None)) == "slack"
            and (getattr(source, "chat_id", None) == cookunity.CHANNEL
                 or getattr(source, "user_id", None) in cookunity.EXTRA_USERS
                 or hasattr(source, "_restricted_native")))


def identity(source) -> tuple:
    return tuple(getattr(source, key, None) for key in (
        "platform", "scope_id", "chat_id", "user_id", "chat_type", "profile",
        "delivered_via_upstream_relay", "profile_route_rejected", "is_bot", "thread_id",
    ))


@dataclass(frozen=True, eq=False)
class _Native:
    identity: tuple
    adapter: Any
    client: Any
    expires: float
    policy: tuple
    bot: str
    request: tuple


async def admit_native(adapter, event, body, source):
    """Called only from Bolt admission, before enrichment. No model/env identity inputs."""
    require(type(body) is dict and body.get("team_id", body.get("team")) == WORKSPACE)
    require(source.scope_id == WORKSPACE and source.user_id in cookunity.USERS
            and source.chat_id == cookunity.CHANNEL and source.chat_type == "group")
    require(not source.profile and not source.profile_route_rejected and not source.delivered_via_upstream_relay)
    require(not source.is_bot and not event.get("bot_id") and not event.get("bot_profile"))
    require(event.get("type") in {"message", "app_mention"} and event.get("subtype") in {None, ""})
    require(not event.get("files") and not event.get("attachments"))
    require(type(event.get("text")) is str and bool(event["text"].strip()))
    client = adapter._team_clients.get(WORKSPACE)
    require(client is not None and bool(adapter._team_bot_user_ids.get(WORKSPACE)))
    payload = await client.users_info(user=source.user_id)
    require(payload.get("ok") is True)
    member = payload.get("user")
    require(type(member) is dict and member.get("id") == source.user_id)
    # Slack Connect members may belong to another home workspace. The fresh
    # Bolt event and authenticated receiving client bind this channel's workspace.
    require(member.get("is_bot") is False and not member.get("deleted") and not member.get("is_workflow_bot"))
    require(event.get("channel") == cookunity.CHANNEL and event.get("user") == source.user_id)
    require(event.get("channel_type") in {"channel", "group"}
            or event.get("type") == "app_mention" and event.get("channel_type") in {None, ""})
    from gateway.client_context_codewords_history import slack_timestamp
    slack_timestamp(event.get("ts"))
    source.thread_id = slack_timestamp(event.get("thread_ts") or event["ts"])
    require(Decimal(source.thread_id) <= Decimal(event["ts"]))
    await asyncio.to_thread(cookunity.ensure_ready)
    grant = policy()
    receipt = _Native(identity(source), adapter, client, time.monotonic() + TIMEOUT + 30,
                      grant, adapter._team_bot_user_ids[WORKSPACE],
                      (event["text"], event["ts"], source.thread_id))
    _issued.add(receipt)
    source._restricted_native = receipt


def native(source, adapter=None):
    receipt = getattr(source, "_restricted_native", None)
    require(type(receipt) is _Native and receipt in _issued)
    receipt = cast(_Native, receipt)
    require(receipt.identity == identity(source) and time.monotonic() < receipt.expires)
    require(source.scope_id == WORKSPACE and source.user_id in cookunity.USERS
            and source.chat_id == cookunity.CHANNEL and source.chat_type == "group")
    require(adapter is None or receipt.adapter is adapter)
    require(receipt.adapter._team_clients.get(WORKSPACE) is receipt.client)
    require(receipt.adapter._team_bot_user_ids.get(WORKSPACE) == receipt.bot)
    require(policy() == receipt.policy)
    return receipt


def policy():
    return cookunity.policy()


def seal_event(event):
    return (identity(event.source), event.source.thread_id, event.text, event.message_id,
            getattr(event.message_type, "value", event.message_type), tuple(event.media_urls),
            tuple(event.media_types), event.internal, bool(event.prompt_response), getattr(event.source, "_cookunity_context", ()))


def validate_event(runner, event):
    from gateway.client_context_policy import MAX_QUESTION

    source = event.source
    native(source, runner._delivery_adapter_for(source))
    require(not getattr(runner.config, "multiplex_profiles", False))
    require(runner._is_user_authorized_for_source(source))
    require(getattr(event, "_restricted_request", None) == seal_event(event))
    require(not event.internal and not event.prompt_response and not event.media_urls and not event.media_types)
    require(getattr(event.message_type, "value", event.message_type) == "text")
    require(type(event.text) is str and 0 < len(event.text.encode()) <= MAX_QUESTION)
    require(bool(event.text.strip()) and not event.text.lstrip().startswith(("/", "!")))
    require(not any(ord(c) < 32 and c not in "\n\t\r" for c in event.text))
    return policy()


def session_key(key, source):
    return f"{key}:cookunity:{source.user_id}" if selected(source) else key


def _loop(grant, question, validate, context=(), history=None):
    from gateway import client_context as cc
    from gateway import client_context_codewords_history as slack_history

    _, opts, _ = grant
    history_schema = slack_history.schema("cookunity")
    history_name = history_schema["function"]["name"]
    tools = cookunity.schemas() + [history_schema]
    allowed = {t["function"]["name"] for t in tools}
    messages = cc.request_messages(json.dumps({"current_request": question,
        "same_thread_evidence": context, "request_time_utc": datetime.now(timezone.utc).isoformat()}))
    messages[0]["content"] = SYSTEM
    seen, count = set(), 0
    for _ in range(MAX_CALLS + 1):
        validate()
        require(len(json.dumps(messages, allow_nan=False).encode()) <= 64000)
        response = cc.complete(opts, "", messages=messages, tools=tools if count < MAX_CALLS else [], before_request=validate)
        validate()
        require(type(response) is dict and response.get("role") == "assistant")
        require(len(json.dumps(response, allow_nan=False).encode()) <= 16000)
        calls = response.get("tool_calls") or []
        require(type(calls) is list and len(calls) <= MAX_CALLS - count)
        if not calls:
            return cc.safe_output(response.get("content"))
        validated = []
        for call in calls:
            fields(call, {"id", "type", "function"})
            require(call["type"] == "function" and type(call["id"]) is str and 0 < len(call["id"]) <= 200)
            require(call["id"] not in seen)
            seen.add(call["id"])
            fn = fields(call["function"], {"name", "arguments"})
            require(type(fn["name"]) is str and fn["name"] in allowed)
            require(type(fn["arguments"]) is str and len(fn["arguments"].encode()) <= 4096)
            if fn["name"] == history_name:
                slack_history.validate_call(fn["arguments"])
            else:
                cookunity.parameters(fn["name"], cookunity.validate(fn["name"], fn["arguments"]))
            validated.append((call["id"], fn))
        messages.append(response)
        for cid, fn in validated:
            validate()
            if fn["name"] == history_name:
                require(history is not None)
                result = history.execute(fn["arguments"])
            else:
                result = cookunity.execute(fn["name"], fn["arguments"], validate)
            validate()
            require(type(result) is str and len(result.encode()) <= 10000)
            messages.append({"role": "tool", "tool_call_id": cid, "content": result})
            count += 1
    require(False)


async def handle_turn(runner, event, source, key, generation):
    stopped = threading.Event()
    deadline = time.monotonic() + TIMEOUT
    try:
        grant = validate_event(runner, event)
        original = seal_event(event)
        require(event.source is source)
        adapter = runner._delivery_adapter_for(source)
        require(native(source, adapter).adapter is adapter)
        receipts = []

        def validate():
            require(not stopped.is_set() and time.monotonic() < deadline)
            require(event.source is source and seal_event(event) == original)
            require(validate_event(runner, event) == grant)
            require(runner._is_session_run_current(key, generation))
            # Retained history receipt: an authenticated edit/deletion in the
            # CookUnity channel invalidates this turn through completion and send.
            for receipt in receipts:
                receipt()

        from gateway.client_context_codewords_history import HistoryReader

        history = HistoryReader(adapter, event, validate, asyncio.get_running_loop(),
                                client_id="cookunity", channels=(cookunity.CHANNEL,))
        receipts.append(history.validate_receipt)
        reply = await asyncio.wait_for(asyncio.to_thread(
            _loop, grant, event.text, validate, getattr(source, "_cookunity_context", ()), history,
        ), TIMEOUT)
        validate()
        reply._restricted_validate = validate
        reply._restricted_destination = (WORKSPACE, source.chat_id, source.thread_id)
        reply._restricted_adapter = adapter
        runner._pop_post_delivery_callback(runner._delivery_adapter_for(source), key, generation)
        return True, reply
    except Exception:
        stopped.set()
        return True, None  # Never retry through the ordinary agent or return raw provider errors.
    except BaseException:
        stopped.set()
        raise
    finally:
        await runner._hmwa_stop_typing_for_turn(event, source)


async def send(adapter, chat_id, content, reply_to, metadata):
    from gateway.client_context import MAX_OUTPUT, ScopedReply
    from gateway.platforms.base import SendResult

    try:
        require(isinstance(content, ScopedReply) and content._restricted_adapter is adapter)
        require(chat_id == cookunity.CHANNEL)
        validate = content._restricted_validate
        require(callable(validate))
        validate()
        team = adapter._metadata_team_id(metadata)
        thread = adapter._resolve_thread_ts(reply_to, metadata)
        destination = getattr(content, "_restricted_destination", None)
        require((team, chat_id, thread) == destination)
        # The reply has already passed safe_output; preserve its escaped entities
        # and inert @ characters while using the ordinary Slack markdown renderer.
        text = str(content)
        require(0 < len(text.encode("utf-8")) <= MAX_OUTPUT)
        text = adapter.format_message(text)
        require(isinstance(text, str) and bool(text.strip())
                and len(text.encode("utf-8")) <= MAX_OUTPUT)
        require(not any(entity in text for entity in ("<@", "<!", "<#")))
        validate()
        require((adapter._metadata_team_id(metadata), chat_id,
                 adapter._resolve_thread_ts(reply_to, metadata)) == destination)
        client = adapter._team_clients[WORKSPACE]
        payload = {"channel": chat_id, "text": text, "mrkdwn": True, "parse": "none",
                   "link_names": False, "unfurl_links": False, "unfurl_media": False}
        if thread:
            payload["thread_ts"] = thread
        validate()
        result = await adapter._deletion_write(client, "chat_postMessage", **payload)
        return SendResult(success=True, message_id=result.get("ts"))
    except Exception:
        return SendResult(success=False, error="restricted_release_denied")


async def thread_question(adapter, event, source, text, bare):
    """One bounded native thread read; never consult stored agent/session history."""
    receipt = native(source, adapter)
    require((event.get("text"), event.get("ts"), source.thread_id) == receipt.request)
    require(not adapter._is_ignored_channel(source.chat_id))
    if source.thread_id == event.get("ts"):
        require(not bare)
        source._cookunity_context = ()
        return text
    payload = {"channel": cookunity.CHANNEL, "ts": source.thread_id, "limit": 15}
    native(source, adapter)
    result = await asyncio.wait_for(receipt.client.conversations_replies(**payload), 10)
    native(source, adapter)
    require(result.get("ok") is True and type(result.get("messages")) is list)
    from gateway.client_context_codewords_history import slack_timestamp
    context = []
    size = 0
    question = None
    for message in result["messages"][:15]:
        require(type(message) is dict)
        ts = slack_timestamp(message.get("ts"))
        require(message.get("channel", cookunity.CHANNEL) == cookunity.CHANNEL)
        require(ts == source.thread_id or message.get("thread_ts") == source.thread_id)
        if Decimal(ts) >= Decimal(event["ts"]) or message.get("bot_id") or message.get("subtype"):
            continue
        author, body = message.get("user"), message.get("text")
        if author not in cookunity.USERS or type(body) is not str or not body.strip():
            continue
        prose = adapter._participation_text(body)
        if adapter._silence_requested(prose):
            continue
        clean = prose.replace(f"<@{receipt.bot}>", "").strip()
        if not clean or clean.startswith(("/", "!")) or adapter._participation_chatter(prose):
            continue
        if len(body.encode()) > 3000 or size + len(body.encode()) > 3000:
            continue
        context.append((ts, author, body))
        size += len(body.encode())
        question = body
    source._cookunity_context = tuple(context)
    require(not bare or question is not None)
    return question if bare else text
