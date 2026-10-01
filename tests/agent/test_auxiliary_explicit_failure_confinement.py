"""Explicit auxiliary providers cannot escape to discovery after fallback failure."""
from types import SimpleNamespace
from typing import cast
from unittest.mock import MagicMock, patch
import httpx
import openai
import pytest
import agent.auxiliary_client as aux

@pytest.mark.parametrize('provider,expected_discovery', [('openai-codex',0),('xai-oauth',0),('auto',1)])
def test_failed_fallback_respects_explicit_provider(provider, expected_discovery):
    route=SimpleNamespace(task='approval',tag='',resolved_provider=provider,client=MagicMock(),main_runtime=None,final_model='test-model',route_info={})
    error=openai.APIConnectionError(request=httpx.Request('POST','https://test.invalid'))
    with patch.object(aux,'_try_configured_fallback_chain',return_value=(MagicMock(),'test-fallback','fallback_chain[0](openai-codex)')), patch.object(aux,'_try_payment_fallback',return_value=(None,None,'')) as discovery:
        gen=aux._ladder_provider_fallback(error,cast(aux._LadderRoute, route))
        next(gen)
        with pytest.raises(StopIteration):
            gen.send(None)
        assert discovery.call_count==expected_discovery


@pytest.mark.parametrize("async_mode", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("provider", ["custom", "auto"])
@pytest.mark.parametrize("failure", ["connection", "rate", "model"])
@pytest.mark.parametrize("policy", ["empty", "null", "malformed", "invalid_entries", "exhausted", "candidate_failure", "success", "absent"])
def test_task_chain_boundary_public_api(tmp_path, monkeypatch, async_mode, provider, failure, policy):
    """A real task config bounds both public APIs, independently of provider selection."""
    import asyncio
    from unittest.mock import AsyncMock
    import yaml

    request = httpx.Request("POST", "https://aux.invalid/v1/chat/completions")
    errors = {
        "connection": openai.APIConnectionError(request=request),
        "rate": openai.RateLimitError("rate limit", response=httpx.Response(429, request=request), body=None),
        "model": openai.BadRequestError("model is not supported", response=httpx.Response(400, request=request), body=None),
    }
    original = errors[failure]
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="approved", tool_calls=None))])
    entry = {"provider": "custom", "model": "approved-fallback", "base_url": "https://aux.invalid/v1", "api_key": "test", "api_mode": "chat_completions"}
    chains = {"empty": [], "null": None, "malformed": "auto", "invalid_entries": [None, "auto", {}],
              "exhausted": [dict(entry, model="unavailable")], "candidate_failure": [entry], "success": [entry]}
    task_config = {"provider": provider, "model": "primary", "api_key": "test", "api_mode": "chat_completions"}
    if policy != "absent":
        task_config["fallback_chain"] = chains[policy]
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "model": {"provider": "custom", "default": "unapproved-main", "base_url": "https://main.invalid/v1"},
        "fallback_providers": [{"provider": "openrouter", "model": "unapproved-main-fallback", "api_mode": "chat_completions"}],
        "auxiliary": {"transient_retries": 0, "approval": task_config},
    }), encoding="utf-8")
    monkeypatch.setattr(aux, "_client_cache", {})
    monkeypatch.setattr(aux, "_aux_unhealthy_until", {})
    built = []

    def wire_client(provider, model=None, async_mode=False, **kwargs):
        built.append((provider, model))
        if model == "unavailable":
            return None, None
        client = SimpleNamespace(base_url="https://aux.invalid/v1", api_key="test")
        create = AsyncMock() if async_mode else MagicMock()
        if model == "primary":
            create.side_effect = original
        elif model == "approved-fallback" and policy == "candidate_failure":
            create.side_effect = openai.AuthenticationError("invalid token", response=httpx.Response(401, request=request), body=None)
        else:
            create.return_value = response
        client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        return client, model

    monkeypatch.setattr(aux, "resolve_provider_client", wire_client)
    monkeypatch.setattr(aux, "_to_async_client", lambda client, model, **kwargs: wire_client("custom", model, True))
    discovery = MagicMock(return_value=(None, None, ""))
    monkeypatch.setattr(aux, "_try_payment_fallback", discovery)
    # Observe the real fallback resolvers; do not replace their config or policy logic.
    with patch.object(aux, "_try_main_agent_model_fallback", wraps=aux._try_main_agent_model_fallback) as main, \
         patch.object(aux, "_try_main_fallback_chain", wraps=aux._try_main_fallback_chain) as main_chain:
        def invoke():
            kwargs = {"task": "approval", "messages": [{"role": "user", "content": "test"}]}
            return asyncio.run(aux.async_call_llm(**kwargs)) if async_mode else aux.call_llm(**kwargs)

        if policy in {"success", "absent"}:
            assert invoke() is response
        else:
            with pytest.raises(type(original)) as caught:
                invoke()
            assert caught.value is original
        if policy != "absent":
            main.assert_not_called()
            main_chain.assert_not_called()
            discovery.assert_not_called()
            assert all(model in {"primary", "approved-fallback", "unavailable"} for _, model in built)
        else:
            assert main_chain.called if provider == "auto" else main.called
        if policy in {"success", "candidate_failure"}:
            assert ("custom", "approved-fallback") in built
