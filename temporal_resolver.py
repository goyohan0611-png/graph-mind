"""Weapon 1: resolve the relative time phrases an extraction leaves behind into absolute dates.

Only 7% of ingested events carry a date_value, while 21% carry a phrase ("today", "last weekend",
"two weeks ago") that is meaningless without its anchor. Every event knows the session it came from,
so the engine — not the reader — can pin those phrases to real dates, deterministically and for free.

Returns a (start, end) ISO date pair: a single day is start == end, "last month" is that whole month.
Unresolvable phrases ("soon", "ongoing", "daily") return None; guessing would be worse than silence.

    python temporal_resolver.py   # runs the self-check
"""
from __future__ import annotations

from datetime import date, timedelta
import re

_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
             "saturday": 5, "sunday": 6}
_MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})
_UNITS = {"day": 1, "week": 7, "month": 30, "year": 365}


def session_day(session_date: str) -> date | None:
    """'2023/05/20 (Sat) 15:08' -> date(2023, 5, 20)."""
    match = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", session_date or "")
    return date(*map(int, match.groups())) if match else None


def _nearest_year(anchor: date, month: int, day_of: int) -> date | None:
    """A bare 'February 10' means the February nearest the session, not always the past one:
    said on 2023-01-15 it is 2023-02-10 (26 days away), not 2022-02-10 (a year off)."""
    options = []
    for year in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            options.append(date(year, month, day_of))
        except ValueError:
            continue
    return min(options, key=lambda d: abs((d - anchor).days)) if options else None


def _month_span(anchor: date, months_back: int) -> tuple[date, date]:
    year, month = anchor.year, anchor.month - months_back
    while month < 1:
        month += 12
        year -= 1
    first = date(year, month, 1)
    last = date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
    return first, last


def resolve(phrase: str, session_date: str) -> tuple[str, str] | None:
    """Anchor a time phrase to the session it was said in. None when it cannot be pinned."""
    anchor = session_day(session_date)
    if not anchor or not phrase:
        return None
    text = phrase.strip().lower()
    # A relative phrase often quotes its own anchor ("last month relative to session date
    # 2023-05-21"); the anchor is not the answer, so relative wins over any date inside the text.
    relative = re.search(r"\b(last|this|next|ago|yesterday|today|tonight|tomorrow|past)\b", text)

    # "three weeks before 2023-05-28" / "two days after March 3": offset from a quoted date.
    offset = re.search(r"\b(\d+|a|an|one|two|three|four|five|six)\s+(day|week|month|year)s?\s+"
                       r"(before|prior to|after|later than)\b(.*)", text)
    if offset:
        base = resolve(offset.group(4), session_date) if offset.group(4).strip() else None
        if base:
            words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
            count = int(offset.group(1)) if offset.group(1).isdigit() else words[offset.group(1)]
            days = count * _UNITS[offset.group(2)]
            sign = -1 if offset.group(3) in ("before", "prior to") else 1
            start = date.fromisoformat(base[0]) + timedelta(days=sign * days)
            return start.isoformat(), start.isoformat()

    explicit = None if relative else re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", text)
    if explicit:
        try:
            day = date(*map(int, explicit.groups()))
        except ValueError:
            return None
        return day.isoformat(), day.isoformat()

    named = None if relative else re.search(r"\b([a-z]{3,9})\s+(\d{1,2})(?:st|nd|rd|th)?\b", text)
    if named and named.group(1) in _MONTHS:
        day = _nearest_year(anchor, _MONTHS[named.group(1)], int(named.group(2)))
        return (day.isoformat(), day.isoformat()) if day else None

    if re.search(r"\btoday\b|\bthis morning\b|\btonight\b|\bjust now\b", text):
        return anchor.isoformat(), anchor.isoformat()
    if re.search(r"\byesterday\b", text):
        day = anchor - timedelta(days=1)
        return day.isoformat(), day.isoformat()
    if re.search(r"\btomorrow\b", text):
        day = anchor + timedelta(days=1)
        return day.isoformat(), day.isoformat()

    ago = re.search(r"\b(\d+|a|an|one|two|three|four|five|six)\s+(day|week|month|year)s?\s+ago\b", text)
    if ago:
        words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
        count = int(ago.group(1)) if ago.group(1).isdigit() else words[ago.group(1)]
        day = anchor - timedelta(days=count * _UNITS[ago.group(2)])
        return day.isoformat(), day.isoformat()

    weekday = re.search(r"\b(last|this|next)?\s*(" + "|".join(_WEEKDAYS) + r")\b", text)
    if weekday:
        delta = (anchor.weekday() - _WEEKDAYS[weekday.group(2)]) % 7
        day = anchor - timedelta(days=delta or 7) if weekday.group(1) == "last" \
            else anchor - timedelta(days=delta)
        return day.isoformat(), day.isoformat()

    if re.search(r"\blast weekend\b", text):
        saturday = anchor - timedelta(days=(anchor.weekday() - 5) % 7 or 7)
        if anchor.weekday() == 6:  # said ON a Sunday: "last weekend" is the one before this one
            saturday -= timedelta(days=7)
        return saturday.isoformat(), (saturday + timedelta(days=1)).isoformat()
    if re.search(r"\bthis weekend\b", text):
        saturday = anchor + timedelta(days=(5 - anchor.weekday()) % 7)
        return saturday.isoformat(), (saturday + timedelta(days=1)).isoformat()
    if re.search(r"\blast week\b", text):
        monday = anchor - timedelta(days=anchor.weekday() + 7)
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if re.search(r"\bthis week\b", text):
        monday = anchor - timedelta(days=anchor.weekday())
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if re.search(r"\blast month\b", text):
        first, last = _month_span(anchor, 1)
        return first.isoformat(), last.isoformat()
    if re.search(r"\bthis month\b", text):
        first, last = _month_span(anchor, 0)
        return first.isoformat(), last.isoformat()
    if re.search(r"\blast year\b", text):
        return date(anchor.year - 1, 1, 1).isoformat(), date(anchor.year - 1, 12, 31).isoformat()

    if re.search(r"\bthis year\b", text):
        return date(anchor.year, 1, 1).isoformat(), date(anchor.year, 12, 31).isoformat()
    in_year = re.search(r"\bin\s+((?:19|20)\d{2})\b", text)
    if in_year:
        value = int(in_year.group(1))
        return date(value, 1, 1).isoformat(), date(value, 12, 31).isoformat()

    span = re.search(r"\b(?:past|last)\s+(\d+)\s*(?:-\s*\d+\s*)?(day|week|month|year)s?\b", text)
    if span:
        start = anchor - timedelta(days=int(span.group(1)) * _UNITS[span.group(2)])
        return start.isoformat(), anchor.isoformat()

    month_only = re.search(r"\bin\s+([a-z]{3,9})\b", text)
    if month_only and month_only.group(1) in _MONTHS:
        middle = _nearest_year(anchor, _MONTHS[month_only.group(1)], 15)
        if middle:
            first, last = _month_span(middle, 0)
            return first.isoformat(), last.isoformat()
    return None


# --- structured offsets: the model says HOW FAR, the engine does the calendar -----------------
# No language patterns below this line. The ingestion model reports time_offset_count/unit
# ("last weekend" -> -1 weekend) in whatever language the session is in; this turns that into real
# dates, which is arithmetic, not understanding. resolve() above stays only for older events that
# carry an English phrase and no offset.

def _add_months(anchor: date, months: int) -> date:
    month = anchor.month - 1 + months
    year, month = anchor.year + month // 12, month % 12 + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return date(year, month, min(anchor.day, last))


def from_offset(count, unit: str, session_date: str) -> tuple[str, str] | None:
    """(start, end) for an offset the extraction model measured from the session day."""
    anchor = session_day(session_date)
    if anchor is None or count is None or unit in (None, "", "none"):
        return None
    try:
        steps = int(count)
    except (TypeError, ValueError):
        return None
    if unit == "day":
        day = anchor + timedelta(days=steps)
        return day.isoformat(), day.isoformat()
    if unit == "week":
        monday = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=steps)
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    if unit == "weekend":
        saturday = anchor - timedelta(days=(anchor.weekday() - 5) % 7) + timedelta(weeks=steps)
        if anchor.weekday() < 5:      # said midweek: "this weekend" is the one coming up
            saturday += timedelta(days=7)
        return saturday.isoformat(), (saturday + timedelta(days=1)).isoformat()
    if unit == "month":
        middle = _add_months(anchor, steps)
        first, last = _month_span(middle, 0)
        return first.isoformat(), last.isoformat()
    if unit == "year":
        year = anchor.year + steps
        return date(year, 1, 1).isoformat(), date(year, 12, 31).isoformat()
    return None


def enrich(events: list[dict]) -> int:
    """Fill date_value (and date_end for spans) from the phrase + session_date. Returns how many."""
    filled = 0
    for event in events:
        if event.get("date_value") or not event.get("session_date"):
            continue
        span = from_offset(event.get("time_offset_count"), event.get("time_offset_unit"),
                           event["session_date"])
        if span is None:   # legacy events: no structured offset, fall back to the phrase
            span = resolve(event.get("effective_time_text") or "", event["session_date"])
        if span:
            event["date_value"], event["date_end"] = span
            event["date_source"] = "resolved"
            filled += 1
    return filled


def _self_check():
    sunday = "2023/05/28 (Sun) 10:00"   # anchor: Sunday 2023-05-28
    cases = {
        "today": ("2023-05-28", "2023-05-28"),
        "yesterday": ("2023-05-27", "2023-05-27"),
        "last Saturday": ("2023-05-27", "2023-05-27"),
        "two weeks ago": ("2023-05-14", "2023-05-14"),
        "3 days ago": ("2023-05-25", "2023-05-25"),
        "last week": ("2023-05-15", "2023-05-21"),
        "last weekend": ("2023-05-20", "2023-05-21"),
        "last month": ("2023-04-01", "2023-04-30"),
        "stated on 2023/04/27": ("2023-04-27", "2023-04-27"),
        "March 15th": ("2023-03-15", "2023-03-15"),
        "in March": ("2023-03-01", "2023-03-31"),
        "past 3 months": ("2023-02-27", "2023-05-28"),
        "How many weddings have I attended in this year?": ("2023-01-01", "2023-12-31"),
        "trips in 2022": ("2022-01-01", "2022-12-31"),
        # the phrase quotes its own anchor: resolve the relation, not the quoted date
        "last month relative to session date 2023-05-28": ("2023-04-01", "2023-04-30"),
        "turned 32 last month": ("2023-04-01", "2023-04-30"),
        # offsets from a quoted date (dev2: "three weeks before ..." used to return the date itself)
        "three weeks before 2023-05-28": ("2023-05-07", "2023-05-07"),
        "two days after 2023-05-28": ("2023-05-30", "2023-05-30"),
    }
    for phrase, want in cases.items():
        got = resolve(phrase, sunday)
        assert got == want, f"{phrase!r}: {got} != {want}"
    # a bare month/day takes the NEAREST year, not blindly the past one (dev2 sent a 2023-02-10
    # flight back to 2022 and broke three temporal answers)
    assert resolve("February 10", "2023/01/15 (Sun) 09:00") == ("2023-02-10", "2023-02-10")
    assert resolve("December 20", "2023/01/15 (Sun) 09:00") == ("2022-12-20", "2022-12-20")
    # structured offsets (the path that replaces language patterns)
    wednesday = "2023/05/31 (Wed) 09:00"
    assert from_offset(0, "day", wednesday) == ("2023-05-31", "2023-05-31")
    assert from_offset(-1, "day", wednesday) == ("2023-05-30", "2023-05-30")
    assert from_offset(-3, "week", wednesday) == ("2023-05-08", "2023-05-14")
    assert from_offset(-1, "weekend", wednesday) == ("2023-05-27", "2023-05-28")
    assert from_offset(1, "month", wednesday) == ("2023-06-01", "2023-06-30")
    assert from_offset(-1, "year", wednesday) == ("2022-01-01", "2022-12-31")
    assert from_offset(None, "day", wednesday) is None
    assert from_offset(-1, "none", wednesday) is None
    assert from_offset(-1, "day", "") is None
    assert _add_months(date(2023, 3, 31), -1) == date(2023, 2, 28)
    events = [{"session_date": wednesday, "time_offset_count": -1, "time_offset_unit": "day"}]
    assert enrich(events) == 1 and events[0]["date_value"] == "2023-05-30"
    for phrase in ("soon", "ongoing", "daily", "recently", ""):
        assert resolve(phrase, sunday) is None, phrase
    assert resolve("today", "") is None
    events = [{"session_date": sunday, "effective_time_text": "last week"},
              {"session_date": sunday, "date_value": "2023-01-01"},
              {"session_date": sunday, "effective_time_text": "soon"}]
    assert enrich(events) == 1 and events[0]["date_value"] == "2023-05-15"
    print("temporal_resolver self-check ok")


if __name__ == "__main__":
    _self_check()
