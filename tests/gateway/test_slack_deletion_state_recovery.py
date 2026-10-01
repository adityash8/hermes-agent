"""EZ-853: a bad slack-deletions.json must not take every Slack message offline."""
import json
import logging

import pytest

from hermes_constants import get_hermes_home
from tests.gateway.test_slack_message_deletion import META, deletion, make_adapter, mention

# Reuse the scheduler delivery authority those transport tests depend on.
from tests.gateway.test_slack_message_deletion import approved_delivery_scope  # noqa: F401

KEY = json.dumps(["T1", "C1", META["thread_id"]])


def _state_path():
    return get_hermes_home() / "slack-deletions.json"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    b'{"threads": {"[\\"T1\\"', b"\xff\xfe not utf-8", b"[]", b'{"threads": []}',
    b'{"threads": {"k": "not-a-record"}}', b'{"threads": {}, "sent": []}',
], ids=["truncated", "not-utf8", "list", "threads-list", "record-not-dict", "sent-list"])
async def test_corrupt_state_is_quarantined_logged_once_and_slack_keeps_working(caplog, payload):
    path = _state_path()
    path.write_bytes(payload)
    adapter, client = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")

    assert (await adapter.send("C1", "still works", metadata=META)).success
    assert (await adapter.send("C1", "and again", metadata=META)).success
    await adapter._handle_slack_message(mention("<@UBOT> hello", ts="130.000001"))
    adapter.handle_message.assert_awaited_once()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and str(path) in errors[0].getMessage()
    quarantined = list(path.parent.glob("slack-deletions.json.corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == payload
    assert adapter._deletion_state()["threads"] == {}


@pytest.mark.asyncio
async def test_unreadable_state_starts_empty_without_crashing(caplog, monkeypatch):
    path = _state_path()
    path.write_text('{"threads": {}}', encoding="utf-8")
    real_read = type(path).read_text

    def deny(self, *args, **kwargs):
        if self == path:
            raise PermissionError(13, "Permission denied", str(self))
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(type(path), "read_text", deny)
    adapter, _ = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")
    assert (await adapter.send("C1", "works", metadata=META)).success
    assert (await adapter.send("C1", "works again", metadata=META)).success
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


@pytest.mark.asyncio
async def test_new_deletion_after_recovery_fences_the_surface_again():
    _state_path().write_bytes(b"{not json")
    adapter, client = make_adapter()
    assert (await adapter.send_or_update_status("C1", "progress", "working", metadata=META)).success
    await adapter._handle_slack_message(deletion())
    client.reset_mock()
    assert not (await adapter.send("C1", "must stay muted", metadata=META)).success
    assert not client.mock_calls
    assert json.loads(_state_path().read_text(encoding="utf-8"))["threads"][KEY]["muted"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("record", [{"muted": True}, {"muted": True, "generation": 2}, {"muted": True, "cutoff": "1.0"}],
                         ids=["no-cutoff-no-generation", "no-cutoff", "no-generation"])
async def test_muted_record_missing_fields_neither_crashes_nor_skips_authorization(record):
    _state_path().write_text(json.dumps({"threads": {KEY: record}, "cleanup": {}}), encoding="utf-8")
    adapter, _ = make_adapter()
    assert not (await adapter.send("C1", "muted", metadata=META)).success

    adapter._authorization_check = lambda *args, **kwargs: False
    await adapter._handle_slack_message(mention("<@UBOT> unauthorized", ts="140.000001"))
    adapter.handle_message.assert_not_awaited()

    adapter._authorization_check = lambda *args, **kwargs: True
    await adapter._handle_slack_message(mention("<@UBOT> resume", ts="140.000002"))
    adapter.handle_message.assert_awaited_once()
    assert (await adapter.send("C1", "reopened", metadata=META)).success
