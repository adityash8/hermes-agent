"""Restricted capabilities cannot become generic tool or host-data access."""

import json
from unittest.mock import Mock

import pytest

from gateway import client_context_restricted_tools as tools
from gateway.client_context_policy import ContextChanged, ContextDenied


def test_public_search_uses_fixed_backend_and_returns_only_public_metadata(monkeypatch):
    from tools import web_tools

    provider = Mock(return_value=json.dumps({"success": True, "secret": "private-envelope", "data": {"web": [
        {"url": "https://example.com/report?token=private-query#private-fragment", "title": "Public report",
         "description": "Public summary", "cache_path": "/Users/operator/private"},
        {"url": "http://127.0.0.1/secrets", "title": "loopback"},
        {"url": "https://operator:password@example.com", "title": "credentials"},
        {"url": "http://intranet.internal/private", "title": "internal"},
        {"url": "https://public.example.org", "description": "/Users/operator/.hermes/private"},
        {"url": "https://sixth.example.org", "title": "beyond fixed limit"},
    ]}}))
    monkeypatch.setattr(web_tools, "web_search_tool", provider)
    guard = Mock()
    result = json.loads(tools.execute("public_web_search", '{"query":"screen printing trends"}', guard, []))
    provider.assert_called_once_with(query="screen printing trends", limit=5)
    assert guard.call_count == 2
    assert result == {"results": [
        {"title": "Public report", "url": "https://example.com/report", "description": "Public summary"},
        {"title": "", "url": "https://public.example.org", "description": ""},
    ]}
    provider.return_value = json.dumps({"success": False, "error": "private-token /Users/operator/cache"})
    assert "private" not in tools.execute("public_web_search", '{"query":"printing"}', guard, [])
    provider.side_effect = RuntimeError("private-token /Users/operator/cache")
    assert "private" not in tools.execute("public_web_search", '{"query":"printing"}', guard, [])
    provider.side_effect = None
    provider.return_value = '{"success":true,"data":{"web":[]}}'
    with pytest.raises(ContextChanged):
        tools.execute("public_web_search", '{"query":"printing"}',
                      Mock(side_effect=[None, ContextChanged("revoked")]), [])


def test_strict_tools_revoke_without_callbacks_persistence_or_generic_dispatch(monkeypatch):
    from tools import web_tools

    search = Mock(side_effect=AssertionError("network must not be reached"))
    monkeypatch.setattr(web_tools, "web_search_tool", search)
    for name, args in [
        ("terminal", {"command": "pwd"}), ("execute_code", {}), ("tool_call", {}),
        ("web_extract", {"url": "https://example.com"}), ("delegate_task", {}),
        ("public_web_search", {"query": "printing", "limit": 100}),
        ("public_web_search", {"query": "printing", "endpoint": "https://example.com"}),
        ("public_web_search", {"query": "x" * 501}),
        ("public_web_search", {"query": "file:///etc/passwd"}),
        ("public_web_search", {"query": "site:127.0.0.1"}),
        ("public_web_search", {"query": "http://[::1]/"}),
        ("public_web_search", {"query": "https://user:password@example.com"}),
        ("clarify", {"question": "Choose", "choices": ["x"] * 6}),
        ("clarify", {"question": "Choose", "callback": "operator"}),
        ("todo_list", {"items": [{"text": "task", "status": "pending"}] * 13}),
        ("todo_list", {"items": [{"text": "task", "status": "done"}]}),
        ("todo_list", {"items": [], "path": "/tmp/tasks"}),
    ]:
        with pytest.raises(ContextDenied):
            tools.validate(name, json.dumps(args))
        assert "error" in json.loads(tools.execute(name, json.dumps(args), lambda: None, []))
    for raw in ['{"query":"one","query":"two"}', '{"query":NaN}', '[]', '{']:
        with pytest.raises(ContextDenied):
            tools.validate("public_web_search", raw)
    search.assert_not_called()
    names = {row["function"]["name"] for row in tools.schemas(frozenset({"web", "clarify", "todo"}))}
    assert names == {"public_web_search", "clarify", "todo_list"}
    assert tools.schemas(frozenset({"terminal", "file", "delegation"})) == []
    todos = []
    entries = [{"text": "Summarize SPI traffic", "status": "in_progress"}]
    assert json.loads(tools.execute("todo_list", json.dumps({"items": entries}), lambda: None, todos)) == {"items": entries}
    assert todos == entries
    assert json.loads(tools.execute("todo_list", "{}", lambda: None, todos)) == {"items": entries}
    assert json.loads(tools.execute("todo_list", "{}", lambda: None, [])) == {"items": []}
    assert json.loads(tools.execute("clarify", '{"question":"Which dates?","choices":["Last week"]}', lambda: None, todos)) == {
        "question": "Which dates?", "choices": ["Last week"],
    }
    guard = Mock(side_effect=[None, ContextChanged("revoked")])
    with pytest.raises(ContextChanged):
        tools.execute("todo_list", '{"items":[]}', guard, todos)
    assert todos == entries
    with pytest.raises(ContextChanged):
        tools.execute("public_web_search", '{"query":"printing"}', Mock(side_effect=ContextChanged("revoked")), todos)
    search.assert_not_called()
