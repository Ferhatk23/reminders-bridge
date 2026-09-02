"""Deutsche Schnelleingabe: aus einer Zeile Titel, Faelligkeit und Prioritaet.

Bewusst konservativ - erkannt wird nur, was eindeutig ist. Alles andere bleibt
Teil des Titels, damit die Eingabe nie ueberraschend Text verschluckt.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

WEEKDAYS_FULL = {
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3,
    "freitag": 4, "samstag": 5, "sonnabend": 5, "sonntag": 6,
}
# Die zweibuchstabigen Kuerzel sind allesamt auch normale deutsche Woerter
# ("so", "am", "di"...). Sie zaehlen deshalb nur, wenn ein "am"/"naechsten"
# davorsteht - sonst frisst die Schnelleingabe Titel-Text.
WEEKDAYS_ABBR = {
    "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6,
}

_TIME = re.compile(r"(?<!\d)(?:um\s+)?([01]?\d|2[0-3])[:.]([0-5]\d)(?!\d)", re.I)
_TIME_UHR = re.compile(r"(?<!\d)(?:um\s+)?([01]?\d|2[0-3])\s*uhr\b", re.I)
_DATE_DE = re.compile(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})?(?!\d)")
_DATE_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{2})-(\d{2})(?!\d)")
_PRIO = re.compile(r"(?:^|\s)(!{1,3})(?=\s|$)")
_IN_DAYS = re.compile(r"\bin\s+(\d{1,3})\s+tag(?:en)?\b", re.I)
_IN_WEEKS = re.compile(r"\bin\s+(\d{1,2})\s+wochen?\b", re.I)


def parse(text: str, today: date | None = None) -> dict:
    """-> {"title", "due" (ISO oder None), "priority" (0/1/5/9)}"""
    today = today or date.today()
    s = text.strip()
    due_date: date | None = None
    due_time: tuple[int, int] | None = None
    priority = 0

    def cut(m: re.Match, keep: str = " ") -> None:
        nonlocal s
        s = (s[: m.start()] + keep + s[m.end():]).strip()

    m = _PRIO.search(s)
    if m:
        priority = {1: 9, 2: 5, 3: 1}[len(m.group(1))]
        cut(m)

    m = _DATE_ISO.search(s)
    if m:
        try:
            due_date = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            cut(m)
        except ValueError:
            pass

    if due_date is None:
        m = _DATE_DE.search(s)
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), m.group(3)
            if year:
                y = int(year)
                y += 2000 if y < 100 else 0
            else:
                y = today.year
            try:
                cand = date(y, month, day)
                # "3.1." im Dezember meint fast immer das kommende Jahr.
                if not year and cand < today:
                    cand = date(y + 1, month, day)
                due_date = cand
                cut(m)
            except ValueError:
                pass

    if due_date is None:
        m = _IN_DAYS.search(s)
        if m:
            due_date = today + timedelta(days=int(m.group(1)))
            cut(m)

    if due_date is None:
        m = _IN_WEEKS.search(s)
        if m:
            due_date = today + timedelta(weeks=int(m.group(1)))
            cut(m)

    if due_date is None:
        for word, delta in (("uebermorgen", 2), ("übermorgen", 2), ("morgen", 1), ("heute", 0)):
            m = re.search(rf"(?:^|\s){word}(?=\s|$)", s, re.I)
            if m:
                due_date = today + timedelta(days=delta)
                cut(m)
                break

    if due_date is None:
        prefix = r"(?:am\s+|naechsten\s+|nächsten\s+|kommenden\s+)?"
        patterns = [(rf"(?:^|\s){prefix}{w}(?=\s|$)", wd) for w, wd in WEEKDAYS_FULL.items()]
        req = r"(?:am|naechsten|nächsten|kommenden)\s+"
        patterns += [(rf"(?:^|\s){req}{w}(?=\s|$)", wd) for w, wd in WEEKDAYS_ABBR.items()]
        for pat, wd in patterns:
            m = re.search(pat, s, re.I)
            if m:
                ahead = (wd - today.weekday()) % 7 or 7
                due_date = today + timedelta(days=ahead)
                cut(m)
                break

    m = _TIME.search(s)
    if m:
        due_time = (int(m.group(1)), int(m.group(2)))
        cut(m)
    else:
        m = _TIME_UHR.search(s)
        if m:
            due_time = (int(m.group(1)), 0)
            cut(m)

    if due_time and due_date is None:
        due_date = today

    due = None
    if due_date and due_time:
        due = datetime(due_date.year, due_date.month, due_date.day, *due_time).strftime(
            "%Y-%m-%dT%H:%M"
        )
    elif due_date:
        due = due_date.isoformat()

    title = re.sub(r"\s{2,}", " ", s).strip(" -–,")
    return {"title": title or text.strip(), "due": due, "priority": priority}
