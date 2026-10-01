"""Independent regression proofs for credential revocation in expanded reads."""

from dataclasses import replace
import json

import httpx
import pytest

from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied


@pytest.mark.parametrize("mutation", ["delete", "rotate"])
@pytest.mark.parametrize("boundary", ["response", "release"])
def test_expanded_read_denies_revoked_credential_before_output(monkeypatch, tmp_path, mutation, boundary):
    path = tmp_path / "posthog.env"
    path.write_text("POSTHOG_PERSONAL_API_KEY=synthetic_original_token\n")
    binding = replace(next(item for item in reads.bindings() if item.name == "codewords_posthog_read"),
                      credentials=(("env_file", str(path)),
                                   ("access_token_key", "POSTHOG_PERSONAL_API_KEY")))
    monkeypatch.setattr(reads, "bindings", lambda: (binding,))
    calls = []

    def revoke():
        if mutation == "delete":
            path.unlink()
        else:
            path.write_text("POSTHOG_PERSONAL_API_KEY=synthetic_replacement_token\n")

    def handle(request):
        calls.append(request.method)
        if request.method == "POST":
            if boundary == "response":
                revoke()
            return httpx.Response(200, json={"results": [{"days": ["2026-01-01"], "data": [3]}]})
        resources = dict(binding.resources)
        return httpx.Response(200, json={"id": int(resources["project_id"]),
            "organization": resources["organization_id"], "timezone": binding.time_zone})

    monkeypatch.setattr(reads.transport, "_http_client",
                        lambda: httpx.Client(transport=httpx.MockTransport(handle)))
    original_normalize = reads.transport._normalized

    def normalize_then_revoke(data, depth=0):
        original_normalize(data, depth)
        if depth == 0:
            revoke()

    if boundary == "release":
        monkeypatch.setattr(reads.transport, "_normalized", normalize_then_revoke)
    with pytest.raises(ContextDenied):
        reads.execute(binding.name, json.dumps({"operation": "event_trends",
            "start_date": "2026-01-01", "end_date": "2026-01-01"}), lambda: None)
    assert calls == (["GET", "POST"] if boundary == "response" else ["GET", "POST", "GET"])


@pytest.mark.parametrize("key", ["env_file", "oauth_file", "developer_token_file", "json_file"])
def test_every_direct_credential_reference_is_sealed(monkeypatch, tmp_path, key):
    path = tmp_path / "credential"
    path.write_text("synthetic-original")
    binding = reads.Binding("codewords_google_ads_read", credentials=((key, str(path)),))
    before = reads._credential_snapshot(binding)
    path.write_text("synthetic-replacement")
    assert reads._credential_snapshot(binding) != before
    path.unlink()
    with pytest.raises(ContextDenied):
        reads._credential_snapshot(binding)
