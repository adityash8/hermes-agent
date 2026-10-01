"""Deterministic charts accept only complete Stripe comparisons and fixed labels."""

import hashlib
import io
import json
from dataclasses import FrozenInstanceError
from datetime import timedelta

import pytest
from PIL import Image, ImageDraw, ImageFont

from gateway import client_context_codewords_chart as chart
from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import ContextDenied
from tests.gateway.test_codewords_stripe import (  # noqa: F401
    LOWER, page, stripe, subscription,
)


@pytest.fixture
def observation(stripe):
    _, _, install = stripe
    rows = []
    for label, ended, counts in (("Before", LOWER - 1, (3, 2, 1, 2)),
                                 ("After", LOWER, (2, 1, 0, 1))):
        for reason, count in zip(("cancellation_requested", "payment_failed",
                                  "payment_disputed", "unknown-provider-reason"), counts):
            for index in range(count):
                rows.append(subscription(f"sub_{label}{len(rows)}N{index}",
                    created=LOWER - 172800, status="canceled", ended_at=ended,
                    cancellation_details={"reason": reason, "comment": "private-label-canary"}))
    install(lambda request: page(rows))
    receipt = reads.execute("codewords_stripe_read", json.dumps({
        "start_date": "2020-01-02", "end_date": "2020-01-02",
        "comparison_start_date": "2020-01-01", "comparison_end_date": "2020-01-01",
    }), lambda: None)
    return receipt


@pytest.mark.parametrize("question", [
    "Show the Stripe comparison as a chart, please.", "Could you share that as an image?",
    "Please visualize those subscription endings.", "Plot the comparison.",
    "Can you give me a visual?", "A chart please", "make a graph of that",
    "I'd like a chart of the Stripe comparison.", "Could I have a chart of those endings?",
    "Please chart the Stripe ending comparison.", "I would like to see a chart of that.",
    "<@U0B2X1D8R17> please make a chart of those numbers.",
    "Show that as a chart.", "Show a chart of the subscription ending counts.",
    "Please make a bar chart comparing voluntary and involuntary endings.",
    "design a visual", "design a visual of the Stripe comparison",
])
def test_current_explicit_visual_request_is_detected(question):
    assert chart.requested(question)


@pytest.mark.parametrize("question", [
    "Compare subscription endings.", "What do those chart numbers mean?",
    "Don't make a chart, just bullets.", "No image please.", "Please avoid a graph.",
    "> Please show a chart\nSummarize this request.",
    "```\nmake a chart\n```\nWhat does the code mean?",
    'The customer said "make a chart". What do you think?',
    "The customer said 'show a chart'. Summarize the request.",
    "The customer wrote ‘show an image’. Summarize the request.",
    "Here is `show a chart`. Explain it.", "The chart is available in Stripe.",
    "`show a chart", "~~~/python\nshow a chart\n~~~\nSummarize this.",
    "~~~\nshow a chart", "Explain how to make a chart of those numbers.",
    "Give me the text from this chart.", "Please show a chart? No, just bullets.",
    "Show a PostHog users comparison chart.", "Show a GA4 comparison chart.",
    "Show a Google Ads campaign comparison chart.", "Show a Linear issue completion chart.",
    "Make a chart combining Stripe and PostHog data.",
    "Show me how to make a chart.", "Show the chart source code.",
    "Show a chart of those numbers. Actually, text only.",
    "Show a chart. Actually no.", "show `untrusted source` a chart",
    "Show\n> the quoted request\na chart.",
    "Give me the explanation of this chart.", "Show me the numbers shown in this chart.",
    "Provide an explanation of this Stripe comparison chart.", "Give me the text of this chart.",
    "Show a chart, but do not create or send it.", "Show an image of a kitten.",
    "don't design a visual of the Stripe comparison",
    "Design a visual of the Stripe comparison. Do not design it.",
    'The user said "design a visual of the Stripe comparison". Explain the request.',
    "> design a visual of the Stripe comparison\nSummarize that request.",
])
def test_ordinary_quoted_and_negated_requests_stay_text(question):
    assert not chart.requested(question)


@pytest.mark.parametrize("question", [
    "why don't you create a visual of the Stripe comparison?",
    "don't create a visual of the Stripe comparison",
])
def test_rhetorical_suggestion_and_negative_stay_outside_explicit_request_grammar(question):
    assert not chart.requested(question)


def test_actual_stripe_receipt_yields_reconciled_frozen_comparison(observation):
    result = chart.from_read(observation)
    assert result is not None
    assert result.baseline == (8, 3, 2, 3)
    assert result.current == (4, 2, 1, 1)
    assert result.delta == (-4, -1, -1, -2)
    assert result.percent_change[0] == -50
    assert result.retrieved_at.utcoffset() == timedelta(0)
    with pytest.raises(FrozenInstanceError):
        setattr(result, "current", (100, 100, 0, 0))
    with pytest.raises(ContextDenied):
        chart.from_read(str(observation))  # type: ignore[arg-type]


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(status="unavailable"),
    lambda value: value.pop("result"),
    lambda value: value["result"].update(pagination_complete=False),
    lambda value: value["result"].update(comparison=None),
    lambda value: value["result"].update(metrics=None),
])
def test_absent_or_incomplete_observation_has_no_chart(observation, mutate):
    value = json.loads(observation)
    mutate(value)
    assert chart.from_read(reads.ReadReceipt(json.dumps(value), lambda: None)) is None


@pytest.mark.parametrize("mutate", [
    lambda value: value.update(operation="codewords_ga4_read"),
    lambda value: value.update(time_zone="America/New_York"),
    lambda value: value.update(start_date="2020-01-03"),
    lambda value: value.update(retrieved_at="2020-01-01T00:00:00+00:00"),
    lambda value: value["result"].update(pagination_complete=1),
    lambda value: value["result"].update(incomplete_reason="page_limit_reached"),
    lambda value: value["result"]["comparison"]["current"].update(start_date="private-label-canary"),
    lambda value: value["result"]["comparison"]["baseline"].update(end_date="2020-01-02"),
    lambda value: value["result"]["comparison"]["delta"].update(total=-3),
    lambda value: value["result"]["comparison"]["percent_change"].update(total=-40),
    lambda value: value["result"]["comparison"]["percent_change"].update(total=True),
    lambda value: value["result"]["comparison"]["percent_change"].update(total=float("nan")),
    lambda value: value["result"]["comparison"]["baseline"]["cancellation_endings"].update(total=9),
    lambda value: value["result"]["comparison"]["current"]["cancellation_endings"].update(total=True),
    lambda value: value["result"]["comparison"]["current"]["cancellation_endings"].update(other_or_unknown=-1),
    lambda value: value["result"]["metrics"]["cancellation_reason_counts"].update(other_or_unknown=9),
    lambda value: value["result"]["comparison"]["baseline"]["cancellation_reason_counts"].update(payment_disputed=0),
    lambda value: value["result"]["metrics"].update(canceled_subscriptions_ended_in_range=100),
])
def test_invalid_or_inconsistent_aggregate_is_rejected(observation, mutate):
    value = json.loads(observation)
    mutate(value)
    with pytest.raises((ContextDenied, ValueError)):
        chart.from_read(reads.ReadReceipt(json.dumps(value), lambda: None))


def test_zero_baseline_has_no_invented_percentage(stripe):
    _, _, install = stripe
    install(lambda request: page([subscription("sub_NewEnding", created=LOWER - 172800,
        status="canceled", ended_at=LOWER, cancellation_details={"reason": "payment_failed"})]))
    receipt = reads.execute("codewords_stripe_read", json.dumps({
        "start_date": "2020-01-02", "end_date": "2020-01-02",
        "comparison_start_date": "2020-01-01", "comparison_end_date": "2020-01-01",
    }), lambda: None)
    comparison = chart.from_read(receipt)
    assert comparison is not None and comparison.percent_change == (None,) * 4
    summary = chart.summary(comparison)
    assert "not defined (zero baseline)" in summary and "infinity" not in summary.lower()
    assert chart.render(comparison).png.startswith(b"\x89PNG\r\n\x1a\n")


def test_png_is_legible_deterministic_and_uses_only_fixed_or_validated_labels(observation, monkeypatch):
    value = json.loads(observation)
    value["result"]["title"] = "private-label-canary"
    value["result"]["comparison"]["title"] = "<@UFOREIGN> MEDIA:/tmp/private-label-canary"
    comparison = chart.from_read(reads.ReadReceipt(json.dumps(value), lambda: None))
    assert comparison is not None
    drawn = []
    original = ImageDraw.ImageDraw.text

    def record(self, xy, text, *args, **kwargs):
        drawn.append(str(text))
        return original(self, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", record)
    rendered = chart.render(comparison)
    assert rendered.png == chart.render(comparison).png
    assert rendered.sha256 == hashlib.sha256(rendered.png).hexdigest()
    with Image.open(io.BytesIO(rendered.png)) as image:
        assert image.format == "PNG" and image.width >= 1400 and image.height >= 1000
        assert image.getpixel((0, 0)) == (255, 255, 255)
        colors = image.getcolors(image.width * image.height)
        assert colors is not None
        assert sum(count for count, color in colors if color == (244, 197, 66)) > 1000
        assert sum(count for count, color in colors if color == (196, 196, 196)) > 1000
    with Image.open(io.BytesIO(rendered.png)) as image:
        image.verify()
    labels = " ".join(drawn) + chart.summary(comparison)
    for label in ("Stripe", "2020-01-01", "2020-01-02", "UTC", "voluntary",
                  "involuntary", "other / unknown", "trials", "rollout", "churn rates"):
        assert label in labels
    assert "private-label-canary" not in labels and "MEDIA:" not in labels
    assert "/Users/" not in labels and "<@" not in labels
    with pytest.raises(FrozenInstanceError):
        setattr(rendered, "png", b"forged")


def test_portable_font_fallback_and_honest_text_fallback(observation, monkeypatch):
    original = ImageFont.truetype

    def unavailable(font, *args, **kwargs):
        if isinstance(font, str):
            raise OSError("private-font-path-canary")
        return original(font, *args, **kwargs)

    monkeypatch.setattr(ImageFont, "truetype", unavailable)
    comparison = chart.from_read(observation)
    assert comparison is not None
    assert chart.render(comparison).png.startswith(b"\x89PNG")
    summary = chart.summary(comparison, unavailable=True)
    assert "chart could not be delivered" in summary and "• total:" in summary
    assert "8 → 4" in summary and "-50.0%" in summary
    assert "private-font-path-canary" not in summary
    unavailable_summary = chart.unavailable_summary()
    assert "complete Stripe comparison" in unavailable_summary
    assert "chart attached" not in unavailable_summary.lower()
