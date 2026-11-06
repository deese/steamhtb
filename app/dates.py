import re
import unicodedata
from datetime import date

# Steam returns release dates as localized free text ("5 SEP 2011",
# "24 ABR 2025", "Sep 5, 2011", sometimes just "2011" or "Coming soon").
# Keys are the first three letters of the accent-stripped, lowercased month.
_MONTHS = {
    "jan": 1, "ene": 1, "feb": 2, "mar": 3, "apr": 4, "abr": 4, "may": 5,
    "jun": 6, "jul": 7, "aug": 8, "ago": 8, "sep": 9, "set": 9, "oct": 10,
    "nov": 11, "dec": 12, "dic": 12,
}

_DAY_MONTH_YEAR = re.compile(r"(\d{1,2})\s+([^\W\d_]+)\.?,?\s+(\d{4})")
_MONTH_DAY_YEAR = re.compile(r"([^\W\d_]+)\.?\s+(\d{1,2}),?\s+(\d{4})")
_MONTH_YEAR = re.compile(r"([^\W\d_]+)\.?\s+(\d{4})")
_YEAR = re.compile(r"(\d{4})")


def _month(name: str) -> int | None:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    return _MONTHS.get(folded[:3])


def parse_release_date(text: str | None) -> date | None:
    """Best-effort parse of Steam's release-date text; None if it isn't a
    concrete date (empty, "Coming soon", "To be announced"...). Month-only and
    year-only values resolve to the first day of that month/year."""
    if not text:
        return None
    text = text.strip()
    try:
        if m := _DAY_MONTH_YEAR.fullmatch(text):
            month = _month(m.group(2))
            return date(int(m.group(3)), month, int(m.group(1))) if month else None
        if m := _MONTH_DAY_YEAR.fullmatch(text):
            month = _month(m.group(1))
            return date(int(m.group(3)), month, int(m.group(2))) if month else None
        if m := _MONTH_YEAR.fullmatch(text):
            month = _month(m.group(1))
            return date(int(m.group(2)), month, 1) if month else None
        if m := _YEAR.fullmatch(text):
            return date(int(m.group(1)), 1, 1)
    except ValueError:  # e.g. day 31 in a 30-day month
        return None
    return None
