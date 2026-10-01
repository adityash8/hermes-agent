"""Stripe fixed reads through the real executor, file loader and bounded HTTP transport."""

import json
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied

NAME = "codewords_stripe_read"
ARGS = '{"start_date":"2020-01-02","end_date":"2020-01-02"}'
LOWER = 1577923200
UPPER = LOWER + 86400
KEY = "rk_live_StripeSecretCanary"


def subscription(ident="sub_One", **kwargs):
    return {"object": "subscription", "id": ident, "livemode": True,
            "status": "active", "created": LOWER, "ended_at": None,
            "cancel_at_period_end": False, "customer": "cus_PrivateCanary",
            "metadata": {"instruction": "private-canary"}, "description": "pii-canary@example.test",
            **kwargs}


def page(rows, more=False):
    return {"object": "list", "url": "/v1/subscriptions", "data": rows, "has_more": more}


@pytest.fixture
def stripe(monkeypatch, tmp_path):
    # These synthetic credentials exercise actual dotenv loading; no installed secret is read.
    path = tmp_path / "codewords-stripe.env"
    path.write_text("CODEWORDS_STRIPE_RESTRICTED_KEY=" + KEY + "\n", encoding="utf-8")
    monkeypatch.setattr(reads, "STRIPE_ENV_FILE", path)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_DefaultProfileCanary")
    monkeypatch.setenv("CODEWORDS_STRIPE_RESTRICTED_KEY", "rk_live_AmbientCanary")
    original = next(b for b in reads.bindings() if b.name == NAME)
    binding = replace(original, credentials=(("env_file", str(path)),
                                            ("access_token_key", "CODEWORDS_STRIPE_RESTRICTED_KEY")))
    monkeypatch.setattr(reads, "_BINDINGS", tuple(binding if b.name == NAME else b for b in reads.bindings()))
    calls = []

    def install(handler):
        def send(request):
            calls.append(request)
            assert request.method == "GET"
            assert request.url.host == "api.stripe.com"
            assert request.url.path == "/v1/subscriptions"
            assert request.headers["authorization"] == "Bearer " + KEY
            assert request.headers["stripe-version"] == "2024-06-20"
            assert request.headers["stripe-account"] == dict(original.resources)["account_id"]
            result = handler(request)
            return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)

        monkeypatch.setattr(reads.transport, "_http_client", lambda: httpx.Client(
            transport=httpx.MockTransport(send), follow_redirects=False, trust_env=False))

    return path, calls, install


def execute(check=lambda: None):
    return reads.execute(NAME, ARGS, check)


@pytest.mark.parametrize("resources", [(("account_id", "acct_Foreign"), ("livemode", "true")),
                                      (("account_id", "acct_1Oyuv1Ka5VSTfRCx"), ("livemode", "false"))])
def test_wrong_account_or_mode_binding_never_reads_credential_or_network(stripe, monkeypatch, resources):
    _, calls, install = stripe

    def forbidden(*args, **kwargs):
        pytest.fail("unverified binding reached I/O")

    monkeypatch.setattr(reads, "_BINDINGS", tuple(
        replace(b, resources=resources, blocker="") if b.name == NAME else b for b in reads.bindings()))
    monkeypatch.setattr(reads, "_secret", forbidden)
    install(forbidden)
    assert json.loads(execute())["status"] == "unavailable"
    assert not calls


def test_complete_pagination_aggregates_dates_without_pii(stripe, caplog):
    _, calls, install = stripe

    def provider(request):
        assert request.url.path == "/v1/subscriptions"
        assert request.url.params["status"] == "all"
        assert request.url.params["limit"] == str(reads.STRIPE_PAGE_SIZE)
        assert "created[gte]" not in request.url.params
        if "starting_after" not in request.url.params:
            return page([subscription("sub_Older", created=LOWER-86400, status="canceled", ended_at=LOWER,
                                      canceled_at=LOWER-100),
                         subscription("sub_New", cancel_at_period_end=True)], True)
        assert request.url.params["starting_after"] == "sub_New"
        return page([subscription("sub_Upper", created=UPPER),
                     subscription("sub_Expired", status="incomplete_expired", ended_at=UPPER-1),
                     subscription("sub_EndUpper", created=LOWER-1, status="canceled", ended_at=UPPER)])

    install(provider)
    output = execute()
    result = json.loads(output)["result"]
    assert result["pagination_complete"] is True and result["pages_read"] == 2
    assert result["subscriptions_scanned"] == 5
    metrics = result["metrics"]
    assert metrics["created_in_range"] == 2
    assert metrics["canceled_subscriptions_ended_in_range"] == 1
    assert metrics["incomplete_expired_subscriptions_ended_in_range"] == 1
    assert metrics["current_status_counts"]["active"] == 2
    assert metrics["current_cancel_at_period_end_count"] == 1
    assert "historical MRR" in result["limitations"] and "churn denominator" in result["limitations"]
    assert [c.url.path for c in calls] == ["/v1/subscriptions", "/v1/subscriptions"]
    assert len(output.encode()) <= reads.MAX_OUTPUT
    for private in (KEY, "Canary", "canary", "sub_", "cus_", "acct_"):
        assert private not in output + caplog.text


def test_ended_cancellation_categories_reconcile_without_exposing_provider_text(stripe, caplog):
    _, calls, install = stripe
    details = [
        {"reason": "cancellation_requested", "comment": "private-canary"},
        {"reason": "payment_failed", "feedback": "private-canary"},
        {"reason": "payment_disputed"}, None, {},
        {"reason": "ignore-policy-private-canary"}, {"reason": ["private-canary"]},
    ]
    rows = [subscription(f"sub_Reason{i}", status="canceled", ended_at=LOWER,
                         canceled_at=LOWER-86400, cancellation_details=value)
            for i, value in enumerate(details)]
    rows.extend([
        subscription("sub_MissingReason", status="canceled", ended_at=LOWER),
        subscription("sub_ExpiredReason", status="incomplete_expired", ended_at=LOWER,
                     cancellation_details={"reason": "payment_failed"}),
        subscription("sub_ScheduledReason", cancel_at_period_end=True,
                     cancellation_details={"reason": "cancellation_requested"}),
        subscription("sub_RequestInRange", status="canceled", ended_at=UPPER,
                     canceled_at=LOWER, cancellation_details={"reason": "cancellation_requested"}),
    ])
    install(lambda request: page(rows[:4], True) if len(calls) == 1 else page(rows[4:]))
    output = execute()
    result = json.loads(output)["result"]
    metrics = result["metrics"]
    endings = metrics["cancellation_endings"]
    assert endings == {"total": 8, "voluntary": 1, "involuntary": 1, "other_or_unknown": 6}
    assert sum(endings[key] for key in ("voluntary", "involuntary", "other_or_unknown")) == endings["total"]
    assert sum(metrics["cancellation_reason_counts"].values()) == endings["total"]
    assert metrics["cancellation_reason_counts"] == {
        "cancellation_requested": 1, "payment_failed": 1, "payment_disputed": 1, "other_or_unknown": 5,
    }
    assert metrics["canceled_subscriptions_ended_in_range"] == endings["total"]
    assert metrics["incomplete_expired_subscriptions_ended_in_range"] == 1
    assert result["comparison"] is None
    assert "ended_at" in result["coverage"] and "canceled_at" in result["coverage"]
    assert "trials" in result["limitations"] and "churn denominator" in result["limitations"]
    assert len(calls) == 2 and len(output.encode()) <= reads.MAX_OUTPUT
    assert "canary" not in (output + caplog.text).lower()


@pytest.mark.parametrize("baseline_counts", [(0, 0, 0), (3, 2, 1)])
def test_comparison_uses_one_scan_with_numeric_deltas_and_safe_zero_baseline(stripe, baseline_counts):
    _, calls, install = stripe
    args = {"start_date": "2020-01-02", "end_date": "2020-01-02",
            "comparison_start_date": "2020-01-01", "comparison_end_date": "2020-01-01"}
    reasons = ("cancellation_requested", "payment_failed", "payment_disputed")
    rows = []
    for window, ended, counts in (("Before", LOWER-1, baseline_counts), ("After", LOWER, (2, 4, 1))):
        for reason, count in zip(reasons, counts):
            for index in range(count):
                rows.append(subscription(f"sub_{window}{len(rows)}N{index}", created=LOWER-2*86400,
                                         status="canceled", ended_at=ended,
                                         cancellation_details={"reason": reason}))
    install(lambda request: page(rows))
    output = reads.execute(NAME, json.dumps(args), lambda: None)
    result = json.loads(output)["result"]
    comparison = result["comparison"]
    before = dict(zip(("voluntary", "involuntary", "other_or_unknown"), baseline_counts))
    before["total"] = sum(baseline_counts)
    after = {"total": 7, "voluntary": 2, "involuntary": 4, "other_or_unknown": 1}
    assert comparison["baseline"]["start_date"] == args["comparison_start_date"]
    assert comparison["baseline"]["end_date"] == args["comparison_end_date"]
    assert comparison["current"]["start_date"] == args["start_date"]
    assert comparison["current"]["end_date"] == args["end_date"]
    assert comparison["baseline"]["cancellation_endings"] == before
    assert comparison["current"]["cancellation_endings"] == after == result["metrics"]["cancellation_endings"]
    assert comparison["delta"] == {key: after[key] - before[key] for key in after}
    for key in after:
        if before[key] == 0:
            assert comparison["percent_change"][key] is None
        else:
            assert comparison["percent_change"][key] == pytest.approx((after[key] - before[key]) / before[key] * 100)
    assert "zero" in comparison["percent_change_definition"]
    assert len(calls) == 1 and len(output.encode()) <= reads.MAX_OUTPUT


@pytest.mark.parametrize("after", [False, True])
@pytest.mark.parametrize("status,code", [(401, "invalid_api_key"), (403, "more_permissions_required"),
                                       (403, "account_invalid"), (400, "account_invalid")])
def test_permission_or_account_failure_on_any_page_withholds_data(stripe, after, status, code, caplog):
    _, calls, install = stripe

    def provider(request):
        if after and len(calls) == 1:
            return page([subscription()], True)
        return httpx.Response(status, json={"error": {"code": code, "message": KEY + " acct_Foreign"}})

    install(provider)
    output = execute()
    result = json.loads(output)
    assert result["status"] == "unavailable" and "result" not in result
    assert len(calls) == (2 if after else 1)
    assert KEY not in output + caplog.text and "acct_Foreign" not in output + caplog.text


@pytest.mark.parametrize("args", [
    '{"start_date":"2020-01-02","end_date":"2020-01-01"}',
    '{"start_date":"2020-01-01","end_date":"2020-02-01"}',
    '{"start_date":"2020-02-30","end_date":"2020-02-30"}',
    '{"start_date":"2999-01-01","end_date":"2999-01-01"}',
    '{"start_date":"2020-01-01T00:00:00Z","end_date":"2020-01-02"}',
    '{"start_date":true,"end_date":"2020-01-02"}',
    '{"start_date":"2020-01-01","start_date":"2020-01-02","end_date":"2020-01-02"}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","account_id":"acct_Foreign"}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","stripe_account":"acct_Foreign"}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","Stripe-Account":"acct_Foreign"}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","headers":{"Stripe-Account":"acct_Foreign"}}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","livemode":false}',
    '{"start_date":"2020-01-01","end_date":"2020-01-02","access_token_key":"STRIPE_SECRET_KEY"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":"2020-01-01"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_end_date":"2020-01-01"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":"2019-12-31","comparison_end_date":"2020-01-01"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":"2020-01-02","comparison_end_date":"2020-01-02"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":"2020-01-03","comparison_end_date":"2020-01-03"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":true,"comparison_end_date":"2020-01-01"}',
    '{"start_date":"2020-01-02","end_date":"2020-01-02","comparison_start_date":"2019-02-30","comparison_end_date":"2019-02-30"}',
])
def test_invalid_dates_and_identity_arguments_never_reach_io(stripe, args):
    _, calls, install = stripe
    install(lambda request: pytest.fail("invalid arguments reached HTTP"))
    with pytest.raises(ContextDenied):
        reads.execute(NAME, args, lambda: None)
    assert not calls


def test_completed_day_and_inclusive_31_day_validation():
    assert reads.validate_call(NAME, '{"start_date":"2020-01-01","end_date":"2020-01-31"}')
    today = datetime.now(timezone.utc).date().isoformat()
    with pytest.raises(ContextDenied):
        reads.validate_call(NAME, json.dumps({"start_date": today, "end_date": today}))


@pytest.mark.parametrize("complete", [False, True])
def test_page_bound_withholds_partial_counts_but_accepts_final_page(stripe, complete, monkeypatch):
    monkeypatch.setattr(reads, "STRIPE_MAX_PAGES", 3)
    _, calls, install = stripe
    pages = 0

    def provider(request):
        nonlocal pages
        pages += 1
        return page([subscription(f"sub_Page{pages}Row{i}") for i in range(reads.STRIPE_PAGE_SIZE)],
                    not (complete and pages == reads.STRIPE_MAX_PAGES))

    install(provider)
    args = {"start_date": "2020-01-02", "end_date": "2020-01-02",
            "comparison_start_date": "2020-01-01", "comparison_end_date": "2020-01-01"}
    result = json.loads(reads.execute(NAME, json.dumps(args), lambda: None))["result"]
    assert pages == reads.STRIPE_MAX_PAGES and len(calls) == reads.STRIPE_MAX_PAGES
    assert result["pagination_complete"] is complete
    assert result["subscriptions_scanned"] == reads.STRIPE_MAX_PAGES * reads.STRIPE_PAGE_SIZE
    if complete:
        assert result["metrics"]["created_in_range"] == result["subscriptions_scanned"]
        assert result["comparison"]["current"]["cancellation_endings"]["total"] == 0
    else:
        assert result["metrics"] is None and result["incomplete_reason"] == "page_limit_reached"
        assert result["comparison"] is None


@pytest.mark.parametrize("failure", ["duplicate", "empty_more", "missing_more", "string_more", "oversize_page",
                                     "missing_ended", "missing_created", "test_mode", "unknown_status",
                                     "missing_terminal_end", "future_end", "redirect", "timeout", "oversize_body"])
def test_incomplete_or_untrusted_provider_data_never_releases_metrics(stripe, failure, caplog):
    _, calls, install = stripe

    def provider(request):
        row = subscription()
        result = page([row])
        if failure == "duplicate":
            result["has_more"] = True
        elif failure == "empty_more":
            result = page([], True)
        elif failure == "missing_more":
            del result["has_more"]
        elif failure == "string_more":
            result["has_more"] = "false"
        elif failure == "oversize_page":
            result["data"] = [subscription(f"sub_{i}") for i in range(reads.STRIPE_PAGE_SIZE + 1)]
        elif failure in {"missing_ended", "missing_created"}:
            del row["ended_at" if failure == "missing_ended" else "created"]
        elif failure == "test_mode":
            row["livemode"] = False
        elif failure == "unknown_status":
            row["status"] = "pii-canary@example.test"
        elif failure == "missing_terminal_end":
            row["status"] = "canceled"
        elif failure == "future_end":
            row.update(status="canceled", ended_at=10**12)
        elif failure == "redirect":
            return httpx.Response(302, headers={"location": "https://foreign.invalid/private-canary"})
        elif failure == "timeout":
            raise httpx.ReadTimeout(KEY + " pii-canary@example.test")
        elif failure == "oversize_body":
            from gateway.client_context_codewords_stripe_http import MAX_RESPONSE
            row["metadata"] = {"oversize": "x" * MAX_RESPONSE}
        return result

    install(provider)
    output = execute()
    assert json.loads(output)["status"] == "unavailable" and '"result"' not in output
    assert len(calls) == (2 if failure == "duplicate" else 1)
    assert "canary" not in (output + caplog.text).lower()


@pytest.mark.parametrize("content", [None, "", "STRIPE_SECRET_KEY=sk_live_DefaultProfileCanary\n",
                                    'CODEWORDS_STRIPE_RESTRICTED_KEY="${STRIPE_SECRET_KEY}"\n',
                                    'CODEWORDS_STRIPE_RESTRICTED_KEY="${CODEWORDS_STRIPE_RESTRICTED_KEY}"\n',
                                    "CODEWORDS_STRIPE_RESTRICTED_KEY=sk_live_DefaultProfileCanary\n"])
def test_missing_or_wrong_dedicated_credential_never_falls_back(stripe, content):
    path, calls, install = stripe
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    install(lambda request: pytest.fail("invalid dedicated credential reached HTTP"))
    output = execute()
    assert json.loads(output)["status"] == "unavailable" and not calls
    assert "canary" not in output.lower()


@pytest.mark.parametrize("key,value", [("env_file", "/tmp/foreign-stripe.env"),
                                       ("access_token_key", "STRIPE_SECRET_KEY")])
def test_foreign_credential_reference_is_rejected_before_secret_load(stripe, monkeypatch, key, value):
    _, calls, install = stripe

    def forbidden(*args, **kwargs):
        pytest.fail("foreign credential binding reached I/O")

    original = next(b for b in reads.bindings() if b.name == NAME)
    credentials = tuple((k, value if k == key else v) for k, v in original.credentials)
    monkeypatch.setattr(reads, "_BINDINGS", tuple(
        replace(b, credentials=credentials) if b.name == NAME else b for b in reads.bindings()))
    monkeypatch.setattr(reads, "_secret", forbidden)
    install(forbidden)
    assert json.loads(execute())["status"] == "unavailable"
    assert not calls


@pytest.mark.parametrize("more", [False, True])
def test_empty_complete_scan_is_zero_and_revocation_prevents_release(stripe, monkeypatch, more):
    _, calls, install = stripe
    install(lambda request: page([]))
    result = json.loads(execute())["result"]
    assert result["pagination_complete"] and result["subscriptions_scanned"] == 0
    assert result["metrics"]["canceled_subscriptions_ended_in_range"] == 0
    calls.clear()
    install(lambda request: page([subscription()], more))

    def check():
        if len(calls) == 1:
            monkeypatch.setattr(reads, "_BINDINGS", tuple(
                replace(b, blocker="revoked") if b.name == NAME else b for b in reads.bindings()))

    with pytest.raises(ContextDenied):
        execute(check)
    assert len(calls) == 1


def test_scan_completes_beyond_old_cap_and_timeout_never_releases_partial_totals(stripe, monkeypatch):
    _, calls, install = stripe

    def provider(request):
        offset = (len(calls) - 1) * reads.STRIPE_PAGE_SIZE
        count = min(reads.STRIPE_PAGE_SIZE, 217 - offset)
        return page([subscription(f"sub_Record{offset+i}") for i in range(count)], offset + count < 217)

    install(provider)
    result = json.loads(execute())["result"]
    assert result["pagination_complete"] is True
    assert result["subscriptions_scanned"] == result["metrics"]["created_in_range"] == 217
    assert len(calls) == (217 + reads.STRIPE_PAGE_SIZE - 1) // reads.STRIPE_PAGE_SIZE

    calls.clear()
    # Advance the scan clock after the first provider response without sleeping.
    monkeypatch.setattr(reads.time, "monotonic", lambda: 100.0 if calls else 0.0)
    result = json.loads(execute())["result"]
    assert result["pagination_complete"] is False and result["metrics"] is None
    assert result["incomplete_reason"] == "scan_time_budget_reached"
    assert result["pages_read"] == 0 and len(calls) == 1


@pytest.mark.parametrize("change", ["delete", "rotate"])
def test_credential_revocation_during_final_page_denies_release(stripe, change):
    path, _, install = stripe

    def provider(request):
        if change == "delete":
            path.unlink()
        else:
            path.write_text("CODEWORDS_STRIPE_RESTRICTED_KEY=rk_live_Replacement\n", encoding="utf-8")
        return page([subscription()])

    install(provider)
    with pytest.raises(ContextDenied):
        execute()


def test_final_page_arriving_after_scan_deadline_withholds_totals(stripe, monkeypatch):
    _, calls, install = stripe
    monkeypatch.setattr(reads.time, "monotonic", lambda: reads.STRIPE_SCAN_SECONDS + 1 if calls else 0.0)
    install(lambda request: page([subscription()]))
    result = json.loads(execute())["result"]
    assert result["pagination_complete"] is False and result["metrics"] is None
    assert result["incomplete_reason"] == "scan_time_budget_reached"
