"""Relative-time windows for recall ("10 days ago", "last Tuesday").

Semantic search cannot resolve "what did I buy 10 days ago": the words carry
no meaning an embedding can match, and the answer is whatever happened near a
DATE. On the 2026-09-30 LongMemEval-S run, 8 of the 11 questions recall still
missed at top-10 were relative-time questions of exactly this shape.

``parse_time_window`` turns the first relative-time expression in a query into
an absolute ``(start, end)`` window around ``as_of`` (the moment the query is
asked about — "now" for a live agent). The grammar is deliberately small and
the windows deliberately loose: a window only ever ADDS candidates to recall
(engine/rag.py runs it as a second, filtered vector search and keeps the
unfiltered one), so a miss costs nothing and a slightly wide window costs
little. Questions that ask FOR a duration ("how many weeks ago did I…") carry
no anchor and correctly parse to None.

Pure: no I/O, no clock reads (``as_of`` is always passed in), never raises.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "couple of": 2, "a couple of": 2,
}

_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

# unit -> (days per unit, half-width of the window in days). Wider units get
# wider windows because people round them more ("two months ago" is rarely 60
# days to the day).
_UNITS = {
    "day": (1, 1.0),
    "week": (7, 3.0),
    "month": (30, 10.0),
    "year": (365, 45.0),
}

_NUM = r"(\d{1,3}|a couple of|couple of|an?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_UNIT = r"(day|week|month|year)s?"

_AGO = re.compile(rf"\b{_NUM}\s+{_UNIT}\s+ago\b", re.IGNORECASE)
_LAST_WEEKDAY = re.compile(rf"\blast\s+({'|'.join(_WEEKDAYS)})\b", re.IGNORECASE)
_LAST_UNIT = re.compile(r"\blast\s+(week|month|year)\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\byesterday\b", re.IGNORECASE)


def _count(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        n = int(token)
        return n if n > 0 else None
    return _NUMBER_WORDS.get(token)


def _window(center: datetime, half_width_days: float) -> tuple[datetime, datetime]:
    half = timedelta(days=half_width_days)
    return center - half, center + half


def parse_time_window(text: str, as_of: datetime) -> tuple[datetime, datetime] | None:
    """Return the window the first relative-time expression in ``text`` names.

    Recognised, case-insensitive, first match wins by position:
      * ``N <unit>(s) ago`` — N in digits or one..twelve / a / an / a couple of
        ("ten days ago", "a week ago", "the Wednesday two months ago")
      * ``yesterday``
      * ``last <weekday>`` — the most recent such day strictly before as_of's date
      * ``last week|month|year``
    Returns None when nothing matches. Windows keep as_of's tzinfo.
    """
    try:
        candidates: list[tuple[int, tuple[datetime, datetime]]] = []

        m = _AGO.search(text)
        if m:
            n = _count(m.group(1))
            unit_days, half = _UNITS[m.group(2).lower()]
            if n is not None:
                candidates.append((m.start(), _window(as_of - timedelta(days=n * unit_days), half)))

        m = _YESTERDAY.search(text)
        if m:
            candidates.append((m.start(), _window(as_of - timedelta(days=1), 1.0)))

        m = _LAST_WEEKDAY.search(text)
        if m:
            target = _WEEKDAYS.index(m.group(1).lower())
            back = (as_of.weekday() - target) % 7 or 7
            candidates.append((m.start(), _window(as_of - timedelta(days=back), 1.0)))

        m = _LAST_UNIT.search(text)
        if m:
            unit_days, half = _UNITS[m.group(1).lower()]
            # "last month" spans roughly the previous unit: centre it one unit
            # back with a half-unit window (at least the unit's usual slack).
            candidates.append((
                m.start(),
                _window(as_of - timedelta(days=unit_days), max(half, unit_days / 2)),
            ))

        if not candidates:
            return None
        return min(candidates, key=lambda c: c[0])[1]
    except Exception:  # noqa: BLE001 — a parse helper on the recall path never raises
        return None
