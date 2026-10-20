"""Fixed Stripe comparison graphics; no model text, file paths, or network I/O."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from gateway import client_context_codewords_reads as reads
from gateway.client_context_policy import _reject_json_constant, _unique_object, fields, require

FILENAME = "codewords-subscription-endings.png"
TITLE = "Codewords | subscription endings"
_KEYS = ("total", "voluntary", "involuntary", "other_or_unknown")
_LABELS = ("total", "voluntary", "involuntary", "other / unknown")
_REASONS = ("cancellation_requested", "payment_failed", "payment_disputed", "other_or_unknown")
_VISUAL = r"(?:chart|graph|image|visual|plot)(?:s)?"
_TARGET_WORDS = frozenset((
    "stripe codewords subscription subscriptions ending endings cancellation cancellations "
    "comparison comparisons count counts number numbers figure figures data result results "
    "this that it these those the a an our current baseline before after previous last prior "
    "both one two seven week weeks day days window windows period periods voluntary involuntary "
    "other unknown utc of for from with showing comparing about and to vs versus"
).split())
_OUTPUT = (rf"(?:(?:a|an|the|this|that)\s+)?(?:(?:simple|clear|readable|bar|line|stripe|codewords|"
           rf"subscription|ending|endings|cancellation|count|counts|comparison|weekly|daily)\s+){{0,6}}{_VISUAL}\b")


@dataclass(frozen=True)
class Comparison:
    baseline_start: date
    baseline_end: date
    current_start: date
    current_end: date
    retrieved_at: datetime
    baseline: tuple[int, ...]
    current: tuple[int, ...]
    delta: tuple[int, ...]
    percent_change: tuple[float | None, ...]


@dataclass(frozen=True)
class Chart:
    comparison: Comparison
    png: bytes
    sha256: str


def _target(text: str) -> bool:
    text = re.sub(r"\b(?:please|now|again)\b", "", text)
    words = re.findall(r"[a-z]+|[0-9]+", text)
    return all(word in _TARGET_WORDS or word.isdecimal() for word in words)


def requested(question: str) -> bool:
    """Only current sender prose can opt in; quoted instructions confer no intent."""
    if type(question) is not str:
        return False
    text = "\n".join("." if line.lstrip().startswith(">") else line
                     for line in question.splitlines())
    chunks, delimiter, start = [], None, 0
    for match in re.finditer(r"`+|~{3,}", text):
        if delimiter is None:
            chunks.extend((text[start:match.start()], "."))
            delimiter = match.group()
        elif match.group() == delimiter:
            delimiter, start = None, match.end()
    if delimiter is None:
        chunks.append(text[start:])
    # Boundaries prevent omitted literals from manufacturing a new request.
    prose = re.sub(r'''"[^"]*(?:"|$)|“[^”]*(?:”|$)|(?<!\w)'[^'\n]+(?:'(?!\w)|$)|‘[^’]*(?:’|$)''',
                   ".", "".join(chunks))
    prose = re.sub(r"<@[A-Z0-9_]+(?:\|[^>\n]*)?>", " ", prose).lower().replace("’", "'")
    # Other fixed integrations cannot acquire a Stripe chart by an errant tool call.
    if re.search(r"\b(?:posthog|ga4|gsc|gtm|google (?:analytics|ads|search console|tag manager)"
                 r"|search console|meta ads|facebook ads|postgres(?:ql)?|linear|customer[. -]?io)\b", prose):
        return False
    if re.search(rf"\b(?:no|without|avoid|skip|never|not|don['’]t)\b[^.!?]{{0,60}}\b{_VISUAL}\b", prose):
        return False
    if re.search(r"\b(?:just|only)\s+(?:text|bullets)\b|\b(?:text|bullets)[ -]only\b"
                 r"|(?:^|[.!?;])\s*(?:actually[, ]*)?(?:no\b|never mind\b|cancel\b)"
                 r"|\b(?:do not|don't|never)\s+(?:create|make|design|render|generate|send|share|upload|attach|post)\b", prose):
        return False
    for clause in re.split(r"[.!?;\n]+", prose):
        clause = re.sub(r"^please[, ]+", "", clause.strip())
        clause = re.sub(r"^(?:can|could|would|will) you (?:please )?", "", clause)
        clause = re.sub(r"^(?:can|could|may) i (?:please )?(?:have|get|see) ", "show ", clause)
        clause = re.sub(r"^(?:i'd like|i would like|i want|i need)(?: to see)? ", "show ", clause)
        if re.search(r"\b(?:how|why|what|explain|instructions|steps|source code)\b"
                     rf"|\b(?:text|data|numbers|values|labels) (?:from|in|on) (?:this |that |the |a )?{_VISUAL}\b", clause):
            continue
        direct = re.match(r"^(?:chart|plot|visuali[sz]e)\s+(\S.*)", clause)
        if direct and _target(direct[1]):
            return True
        if re.fullmatch(rf"(?:a |an )?{_VISUAL}\s+please", clause):
            return True
        action = re.match(r"^(?:show|make|design|create|generate|render|share|give|provide|draw|send|attach|turn|put|get|see)\s+(.*)", clause)
        if action:
            body = re.sub(r"^(?:me|us)\s+", "", action[1])
            output = re.match(_OUTPUT, body)
            if output and _target(body[output.end():]):
                return True
            conversion = re.fullmatch(rf"(.{{1,120}})\s+(?:as|into)\s+{_OUTPUT}(?:,?\s+please)?", body)
            if conversion and _target(conversion[1]):
                return True
    return False


def _integer(value: object, maximum: int) -> int:
    require(type(value) is int and 0 <= value <= maximum)
    return value  # type: ignore[return-value]


def _counts(value: object, maximum: int) -> tuple[int, ...]:
    value = fields(value, set(_KEYS))
    counts = tuple(_integer(value[key], maximum) for key in _KEYS)
    require(counts[0] == sum(counts[1:]))
    return counts


def _reconcile_reasons(value: object, counts: tuple[int, ...], maximum: int) -> None:
    reasons = fields(value, set(_REASONS))
    values = tuple(_integer(reasons[key], maximum) for key in _REASONS)
    require((sum(values), values[0], values[1], values[2] + values[3]) == counts)


def _date(value: object) -> date:
    require(type(value) is str and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value))
    return date.fromisoformat(value)  # type: ignore[arg-type]


def from_read(receipt: reads.ReadReceipt) -> Comparison | None:
    """The caller supplies the actual executor receipt from this admitted turn."""
    require(type(receipt) is reads.ReadReceipt and len(receipt.encode()) <= reads.MAX_OUTPUT)
    raw = json.loads(receipt, object_pairs_hook=_unique_object, parse_constant=_reject_json_constant)
    require(type(raw) is dict and raw.get("operation") == "codewords_stripe_read"
            and raw.get("read_operation") == "subscriptions")
    if raw.get("status") == "unavailable" or raw.get("result") is None:
        return None
    require(raw.get("status") == "observation" and raw.get("time_zone") == "UTC")
    result = raw["result"]
    require(type(result) is dict and type(result.get("pagination_complete")) is bool)
    if not result["pagination_complete"] or result.get("comparison") is None or result.get("metrics") is None:
        return None
    require(result.get("incomplete_reason") is None)
    maximum = reads.STRIPE_PAGE_SIZE * reads.STRIPE_MAX_PAGES
    scanned = _integer(result.get("subscriptions_scanned"), maximum)
    pages = _integer(result.get("pages_read"), reads.STRIPE_MAX_PAGES)
    require(pages >= 1 and scanned <= pages * reads.STRIPE_PAGE_SIZE)
    comparison, metrics = result["comparison"], result["metrics"]
    require(type(comparison) is dict and type(metrics) is dict)
    baseline, current = comparison.get("baseline"), comparison.get("current")
    require(type(baseline) is dict and type(current) is dict)
    before = _counts(baseline.get("cancellation_endings"), scanned)
    after = _counts(current.get("cancellation_endings"), scanned)
    require(_counts(metrics.get("cancellation_endings"), scanned) == after
            and type(metrics.get("canceled_subscriptions_ended_in_range")) is int
            and metrics["canceled_subscriptions_ended_in_range"] == after[0])
    _reconcile_reasons(baseline.get("cancellation_reason_counts"), before, scanned)
    _reconcile_reasons(metrics.get("cancellation_reason_counts"), after, scanned)
    status = fields(metrics.get("current_status_counts"), set(reads.STRIPE_STATUSES))
    require(sum(_integer(count, scanned) for count in status.values()) == scanned
            and before[0] + after[0] <= status["canceled"])
    before_start, before_end = _date(baseline.get("start_date")), _date(baseline.get("end_date"))
    after_start, after_end = _date(current.get("start_date")), _date(current.get("end_date"))
    require(0 <= (after_end - after_start).days < 31
            and before_end - before_start == after_end - after_start and before_end < after_start
            and raw.get("start_date") == after_start.isoformat() and raw.get("end_date") == after_end.isoformat())
    retrieved = raw.get("retrieved_at")
    require(type(retrieved) is str and re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:\+00:00|Z)", retrieved))
    retrieved_at = datetime.fromisoformat(retrieved)
    require(retrieved_at.utcoffset() == timedelta(0) and after_end < retrieved_at.date()
            and retrieved_at <= datetime.now(timezone.utc) + timedelta(minutes=1))
    deltas = fields(comparison.get("delta"), set(_KEYS))
    percentages = fields(comparison.get("percent_change"), set(_KEYS))
    delta, percent = [], []
    for key, old, new in zip(_KEYS, before, after):
        change = deltas[key]
        require(type(change) is int and change == new - old)
        percentage = percentages[key]
        if old == 0:
            require(percentage is None)
        else:
            require(type(percentage) in (int, float) and math.isfinite(percentage)
                    and math.isclose(percentage, change / old * 100, rel_tol=1e-12, abs_tol=1e-12))
        delta.append(change)
        percent.append(float(percentage) if percentage is not None else None)
    return Comparison(before_start, before_end, after_start, after_end, retrieved_at,
                      before, after, tuple(delta), tuple(percent))


def _window(start: date, end: date) -> str:
    return f"{start.isoformat()} to {end.isoformat()} UTC"


def _percent(value: float | None) -> str:
    return f"{value:+.1f}%" if value is not None else "not defined (zero baseline)"


def summary(comparison: Comparison, *, unavailable: bool = False) -> str:
    lines = ["stripe subscription endings",
             f"• baseline: {_window(comparison.baseline_start, comparison.baseline_end)}",
             f"• current: {_window(comparison.current_start, comparison.current_end)}"]
    for label, old, new, delta, percent in zip(_LABELS, comparison.baseline, comparison.current,
                                               comparison.delta, comparison.percent_change):
        lines.append(f"• {label}: {old:,} → {new:,} ({delta:+,}; {_percent(percent)})")
    lines.extend([
        "• source: Stripe; complete subscription scan; checked "
        + comparison.retrieved_at.strftime("%Y-%m-%d %H:%M UTC") + ".",
        "• ending counts, not customer churn rates; includes trials. Current objects are not an atomic event history.",
        "• chronological comparison; rollout date unverified. No causal attribution.",
    ])
    if unavailable:
        lines.append("• the chart could not be delivered; the verified figures are above.")
    return "\n".join(lines)


def unavailable_summary() -> str:
    return ("• i could not obtain a complete Stripe comparison for this request.\n"
            "• no chart was created; verified comparison counts are unavailable.")


def _font(size: int):
    from PIL import ImageFont
    return ImageFont.load_default(size=size)


def render(comparison: Comparison) -> Chart:
    from PIL import Image, ImageDraw
    from PIL.PngImagePlugin import PngInfo

    require(type(comparison) is Comparison)
    image = Image.new("RGB", (1600, 1200), "white")
    draw = ImageDraw.Draw(image)
    black, gray, gold, line = "#171717", "#c4c4c4", "#f4c542", "#ececec"
    draw.rectangle((24, 24, 1575, 1175), outline=line, width=2)
    draw.text((80, 65), TITLE, font=_font(54), fill=black, stroke_width=1, stroke_fill=black)
    draw.text((80, 140), "Chronological comparison of canceled subscription endings",
              font=_font(30), fill=black)
    for x, fill, first, last in ((80, gray, comparison.baseline_start, comparison.baseline_end),
                                 (815, gold, comparison.current_start, comparison.current_end)):
        draw.rectangle((x, 218, x + 25, 243), fill=fill)
        draw.text((x + 41, 213), _window(first, last), font=_font(28), fill=black)
    maximum = max(*comparison.baseline, *comparison.current, 1)
    scale = 650 / maximum
    for index, (label, old, new, delta, percent) in enumerate(zip(
            _LABELS, comparison.baseline, comparison.current, comparison.delta, comparison.percent_change)):
        y = 300 + index * 171
        draw.text((80, y + 16), label, font=_font(34), fill=black,
                  stroke_width=1, stroke_fill=black)
        for offset, value, fill in ((0, old, gray), (62, new, gold)):
            if value:
                draw.rectangle((455, y + offset, 455 + value * scale, y + offset + 43), fill=fill)
            draw.text((473 + value * scale, y + offset + 2), f"{value:,}", font=_font(32),
                      fill=black, stroke_width=1, stroke_fill=black)
        draw.text((1260, y + 3), f"{delta:+,}", font=_font(45), fill=black,
                  stroke_width=1, stroke_fill=black)
        percentage = f"{percent:+.1f}%" if percent is not None else "no baseline"
        draw.text((1260, y + 65), percentage, font=_font(29), fill=black)
        if index < 3:
            draw.line((80, y + 144, 1515, y + 144), fill=line, width=2)
    footer = (
        "Source: Stripe | complete subscription scan | checked "
        + comparison.retrieved_at.strftime("%Y-%m-%d %H:%M UTC"),
        "Ending counts, not customer churn rates; includes trials.",
        "Chronological windows; rollout date unverified. No causal attribution.",
        "Current objects, not an atomic event history. No baseline means percent change is undefined.",
    )
    for index, text in enumerate(footer):
        draw.text((80, 986 + index * 39), text, font=_font(25), fill="#444444")
    metadata = PngInfo()
    metadata.add_text("Title", TITLE)
    metadata.add_text("Description", summary(comparison))
    output = io.BytesIO()
    image.save(output, format="PNG", pnginfo=metadata)
    png = output.getvalue()
    return Chart(comparison, png, hashlib.sha256(png).hexdigest())
