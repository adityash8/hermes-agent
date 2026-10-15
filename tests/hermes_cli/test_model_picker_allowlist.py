"""Tests for ``model_catalog.picker_allowlist`` — per-provider picker rows show
exactly the configured model ids (visibility only)."""

from unittest.mock import patch

from hermes_cli import model_catalog
from hermes_cli.model_switch_providers import _apply_picker_allowlist


def _rows():
    return [
        {"slug": "openai-codex", "models": ["gpt-5.6", "gpt-6.1-sol", "gpt-6-luna"], "total_models": 3},
        {"slug": "openrouter", "models": ["x-ai/grok-4.7"], "total_models": 1},
        {"slug": "nous", "models": ["hermes-4"], "total_models": 1},
    ]


def _with_config(cfg):
    return patch("hermes_cli.config.load_config", return_value={"model_catalog": cfg})


def test_get_picker_allowlist_normalizes_slugs_and_ids():
    with _with_config({"picker_allowlist": {" OpenRouter ": "openrouter/auto", "xai-oauth": ["grok-4.7", " ", 7]}}):
        assert model_catalog.get_picker_allowlist() == {
            "openrouter": ["openrouter/auto"],
            "xai-oauth": ["grok-4.7", "7"],
        }


def test_get_picker_allowlist_ignores_non_dict():
    with _with_config({"picker_allowlist": ["openrouter"]}):
        assert model_catalog.get_picker_allowlist() == {}


def test_allowlist_replaces_listed_rows_and_keeps_others():
    allow = {"openai-codex": ["gpt-6.1-sol", "gpt-6-luna"], "openrouter": ["openrouter/auto"]}
    with patch.object(model_catalog, "get_picker_allowlist", return_value=allow):
        rows = {r["slug"]: r for r in _apply_picker_allowlist(_rows())}
    assert rows["openai-codex"]["models"] == ["gpt-6.1-sol", "gpt-6-luna"]
    assert rows["openai-codex"]["total_models"] == 2
    # Configured ids are shown even when absent from the (possibly stale) cached row.
    assert rows["openrouter"]["models"] == ["openrouter/auto"]
    assert rows["nous"]["models"] == ["hermes-4"]


def test_empty_id_list_drops_row():
    with patch.object(model_catalog, "get_picker_allowlist", return_value={"nous": []}):
        assert [r["slug"] for r in _apply_picker_allowlist(_rows())] == ["openai-codex", "openrouter"]


def test_no_allowlist_is_a_noop():
    rows = _rows()
    with patch.object(model_catalog, "get_picker_allowlist", return_value={}):
        assert _apply_picker_allowlist(rows) is rows
