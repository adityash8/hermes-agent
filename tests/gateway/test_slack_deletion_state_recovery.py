"""EZ-853: a bad slack-deletions.json must not take every Slack message offline."""
import json
import logging
import time

import pytest

import plugins.platforms.slack.deletion as deletion_state
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
    b'{"threads": {"k": "not-a-record"}}', b'{"threads": {}, "sent": []}', b"[" * 100000 + b"]" * 100000,
], ids=["truncated", "not-utf8", "list", "threads-list", "record-not-dict", "sent-list", "deep-nesting"])
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


@pytest.mark.asyncio
async def test_one_bad_record_keeps_every_valid_fence_across_restart(caplog):
    path = _state_path()
    fence = {"muted": True, "cutoff": "1.0", "generation": 1}
    payload = json.dumps({"threads": {KEY: fence, "bad": "x"}, "cleanup": {}}).encode()
    path.write_bytes(payload)
    adapter, client = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")

    assert not (await adapter.send("C1", "must stay muted", metadata=META)).success
    assert not client.mock_calls
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and str(path) in errors[0].getMessage() and "dropped or reset 1" in errors[0].getMessage()
    quarantined = list(path.parent.glob("slack-deletions.json.corrupt-*"))
    assert len(quarantined) == 1 and quarantined[0].read_bytes() == payload
    assert json.loads(path.read_text(encoding="utf-8"))["threads"] == {KEY: fence}

    restarted, client = make_adapter()
    assert not (await restarted.send("C1", "still muted after restart", metadata=META)).success
    assert not client.mock_calls
    assert len([r for r in caplog.records if r.levelno == logging.ERROR]) == 1


def test_malformed_sent_receipts_are_dropped_not_raised():
    good = [json.dumps(["T1", "C1", "9.000001"]), ["T1", "C1", META["thread_id"]]]
    _state_path().write_text(json.dumps({"threads": {}, "cleanup": {}, "sent": {
        "not-json": ["T1", "C1", "1.0"], json.dumps(["T1", "C1"]): ["T1", "C1", "1.0"],
        json.dumps(["T1", "C1", "2.0"]): "not-a-surface", good[0]: good[1],
        json.dumps([["T1"], "C1", "3.0"]): ["T1", "C1", "1.0"],  # unhashable once turned into a tuple key
        json.dumps(["T1", "C1", "4.0"]): ["T1", {"C1": 1}, "1.0"]}}), encoding="utf-8")
    adapter, _ = make_adapter()
    assert adapter._deletion_messages() == {("T1", "C1", "9.000001"): ("T1", "C1", META["thread_id"])}


def test_deeply_nested_sent_receipt_key_is_dropped_not_raised():
    good = [json.dumps(["T1", "C1", "9.000001"]), ["T1", "C1", META["thread_id"]]]
    deep = "[" * 100000 + "]" * 100000  # json.loads raises RecursionError, which is not a ValueError
    _state_path().write_text(json.dumps({"threads": {}, "cleanup": {}, "sent": {
        deep: ["T1", "C1", "1.0"], good[0]: good[1]}}), encoding="utf-8")
    adapter, _ = make_adapter()
    assert adapter._deletion_messages() == {("T1", "C1", "9.000001"): ("T1", "C1", META["thread_id"])}


DELETE_BREAKERS = [("generation", "2"), ("generation", None), ("generation", [1]),
                   ("cutoff", 5), ("cutoff", None), ("cutoff", ["1.0"]), ("deleted", None), ("deleted", 3)]
FIELD_DEFAULTS = {"generation": 0, "cutoff": "", "deleted": []}


def _ids(cases):
    return [f"{field}-{type(value).__name__}" for field, value in cases]


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), DELETE_BREAKERS, ids=_ids(DELETE_BREAKERS))
async def test_wrong_typed_thread_field_is_reset_so_a_delete_still_mutes(caplog, field, value):
    record = {"muted": False, "generation": 1, "cutoff": "1.0", "deleted": [], field: value}
    _state_path().write_text(json.dumps({"threads": {KEY: record}, "cleanup": {}}), encoding="utf-8")
    adapter, client = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")

    assert (await adapter.send("C1", "open thread", metadata=META)).success
    await adapter._handle_slack_message(deletion())
    client.reset_mock()
    assert not (await adapter.send("C1", "must be muted now", metadata=META)).success
    assert not client.mock_calls
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "dropped or reset 1" in errors[0].getMessage()


SUMMON_BREAKERS = DELETE_BREAKERS[:3]  # cutoff resets are covered by the replay test below


@pytest.mark.asyncio
@pytest.mark.parametrize(("field", "value"), SUMMON_BREAKERS, ids=_ids(SUMMON_BREAKERS))
async def test_wrong_typed_thread_field_is_reset_and_kept_muted_until_summoned(field, value):
    record = {"muted": True, "generation": 1, "cutoff": "1.0", "deleted": ["101.000001"], field: value}
    path = _state_path()
    path.write_text(json.dumps({"threads": {KEY: record}, "cleanup": {}}), encoding="utf-8")
    adapter, _ = make_adapter()

    assert not (await adapter.send("C1", "still muted", metadata=META)).success
    saved = json.loads(path.read_text(encoding="utf-8"))["threads"][KEY]
    assert saved == {**record, field: FIELD_DEFAULTS[field]}
    assert len(list(path.parent.glob("slack-deletions.json.corrupt-*"))) == 1

    adapter._authorization_check = lambda *args, **kwargs: True
    await adapter._handle_slack_message(mention("<@UBOT> resume", ts="140.000002"))
    adapter.handle_message.assert_awaited_once()
    assert (await adapter.send("C1", "reopened", metadata=META)).success


@pytest.mark.asyncio
@pytest.mark.parametrize("cutoff", [123, None, ["x"]], ids=["int", "NoneType", "list"])
async def test_muted_record_with_wrong_typed_cutoff_still_refuses_replayed_mentions(cutoff):
    record = {"muted": True, "generation": 1, "cutoff": cutoff, "deleted": ["101.000001"]}
    _state_path().write_text(json.dumps({"threads": {KEY: record}, "cleanup": {}}), encoding="utf-8")
    adapter, _ = make_adapter()
    adapter._authorization_check = lambda *args, **kwargs: True

    await adapter._handle_slack_message(mention("<@UBOT> replayed from before the reload", ts="140.000002"))
    adapter.handle_message.assert_not_awaited()
    await adapter._handle_slack_message(mention("<@UBOT> fresh", ts=f"{time.time() + 60:.6f}"))
    adapter.handle_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_unencodable_state_is_healed_so_a_new_mute_survives_restart(caplog):
    # A lone surrogate is valid JSON but cannot be written back as UTF-8, so every later save would fail.
    path = _state_path()
    path.write_text('{"threads": {}, "cleanup": {"\\ud800": 1}}', encoding="utf-8")
    adapter, _ = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")

    assert (await adapter.send("C1", "open thread", metadata=META)).success
    await adapter._handle_slack_message(deletion())
    restarted, client = make_adapter()
    assert not (await restarted.send("C1", "still muted after restart", metadata=META)).success
    assert not client.mock_calls
    assert "ud800" not in path.read_text(encoding="utf-8")
    assert len(list(path.parent.glob(path.name + ".corrupt-*"))) == 1
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "dropped or reset 1" in errors[0].getMessage()


@pytest.mark.asyncio
async def test_failed_save_is_logged_and_an_already_posted_send_still_succeeds(caplog, monkeypatch):
    def refuse(*args, **kwargs):
        raise ValueError("unencodable")

    monkeypatch.setattr(deletion_state, "atomic_json_write", refuse)
    adapter, client = make_adapter()
    caplog.set_level(logging.ERROR, logger="plugins.platforms.slack.deletion")

    assert (await adapter.send("C1", "posted", metadata=META)).success
    client.chat_postMessage.assert_awaited_once()
    assert any("Cannot persist deletion state" in r.getMessage() for r in caplog.records)
