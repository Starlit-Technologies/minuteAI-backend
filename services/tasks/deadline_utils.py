"""
Normalize natural-language deadline strings to ISO date strings (YYYY-MM-DD).
Pure rule-based — no LLM call. Used when creating tasks from meeting action items.
"""

import re
from datetime import date, timedelta
from typing import Optional

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_QUARTER_END = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}


def _next_weekday(d: date, weekday: int) -> date:
    days_ahead = weekday - d.weekday()
    if days_ahead <= 0:
        days_ahead += 7
    return d + timedelta(days=days_ahead)


def _end_of_month(d: date) -> date:
    if d.month == 12:
        return d.replace(day=31)
    return (d.replace(month=d.month + 1, day=1) - timedelta(days=1))


def normalize_deadline(raw: Optional[str]) -> Optional[str]:
    """
    Convert a natural-language deadline string to YYYY-MM-DD.

    Returns the original string if the input cannot be parsed — preserves
    human-readable fallback for display in the UI.
    Returns None if input is None or empty.
    """
    if not raw or not raw.strip():
        return raw

    clean = raw.strip().lower()
    today = date.today()

    # Already ISO format — pass through
    if re.match(r"^\d{4}-\d{2}-\d{2}$", clean):
        return clean

    # Immediate
    if clean in ("asap", "immediately", "urgent", "now", "today", "end of day", "eod"):
        return today.isoformat()

    # Tomorrow
    if clean in ("tomorrow", "next day", "by tomorrow"):
        return (today + timedelta(days=1)).isoformat()

    # End of week / by Friday
    if any(x in clean for x in ("end of week", "this week", "end of the week", "by friday", "eow")):
        return _next_weekday(today, 4).isoformat()

    # Next week → next Monday
    if "next week" in clean and "month" not in clean:
        return _next_weekday(today + timedelta(days=7), 0).isoformat()

    # End of month
    if any(x in clean for x in ("end of month", "end of the month", "eom", "month end", "by month end")):
        return _end_of_month(today).isoformat()

    # Next month → end of next month
    if "next month" in clean:
        next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
        return _end_of_month(next_month_start).isoformat()

    # "in X days/weeks"
    m = re.search(r"in\s+(\d+)\s+(day|days|week|weeks)", clean)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        delta = timedelta(days=n * 7 if "week" in unit else n)
        return (today + delta).isoformat()

    # Quarter: Q1–Q4
    m = re.search(r"\bq([1-4])\b", clean)
    if m:
        q = int(m.group(1))
        mo, dy = _QUARTER_END[q]
        year = today.year
        try:
            target = date(year, mo, dy)
            if target < today:
                target = date(year + 1, mo, dy)
            return target.isoformat()
        except ValueError:
            pass

    # Weekday name: "by Thursday", "this Tuesday", "next Friday"
    for day_name, day_num in _WEEKDAYS.items():
        if day_name in clean:
            base = today
            if "next" in clean:
                base = today + timedelta(days=7)
            return _next_weekday(base, day_num).isoformat()

    # "Month Day" pattern: "May 30", "June 15th", "30th May"
    # Try "Month Day" first
    m = re.search(r"\b(" + "|".join(_MONTHS.keys()) + r")\s+(\d{1,2})(?:st|nd|rd|th)?\b", clean)
    if not m:
        # Try "Day Month"
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(" + "|".join(_MONTHS.keys()) + r")\b", clean)
        if m:
            day_str, month_str = int(m.group(1)), m.group(2)
        else:
            day_str, month_str = None, None
    else:
        month_str, day_str = m.group(1), int(m.group(2))

    if month_str and day_str:
        month_num = _MONTHS.get(month_str)
        if month_num and 1 <= int(day_str) <= 31:
            year = today.year
            try:
                target = date(year, month_num, int(day_str))
                if target < today:
                    target = date(year + 1, month_num, int(day_str))
                return target.isoformat()
            except ValueError:
                pass

    # Can't parse — return original for human-readable display
    return raw


# Trigger words that typically precede a deadline expression in a task title
_TRIGGER_RE = re.compile(
    r'\b(?:by|due|before|until|no later than|'
    r'submit(?:ted)?\s+by|complet(?:e|ed)\s+by|finish(?:ed)?\s+by|deliver(?:ed)?\s+by)\b',
    re.IGNORECASE,
)

# Self-contained time phrases that can stand alone anywhere in a title
_STANDALONE_RE = re.compile(
    r'\b('
    r'asap|urgently?|immediately|today|tomorrow|eod|eow|eom|'
    r'end of (?:day|week|month|the week|the month)|'
    r'next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week|month)|'
    r'this (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|week)|'
    r'in \d+ (?:days?|weeks?)|'
    r'q[1-4](?!\w)|'
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)'
    r'\s+\d{1,2}(?:st|nd|rd|th)?|'
    r'\d{1,2}(?:st|nd|rd|th)?\s+'
    r'(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|'
    r'jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)|'
    r'(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)'
    r')\b',
    re.IGNORECASE,
)

_ISO_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def extract_deadline_from_title(title: str) -> Optional[str]:
    """Scan a task title for an embedded deadline and return a YYYY-MM-DD string.

    Returns None if no parseable date expression is found.

    Examples:
      "Send report by Friday"          → next Friday's date
      "Submit proposal - due Q2"       → end of Q2
      "Fix blocker asap"               → today
      "Review deck by end of month"    → last day of current month
      "Weekly standup notes"           → None
    """
    if not title or not title.strip():
        return None

    # Strategy 1: trigger word + trailing expression
    trigger = _TRIGGER_RE.search(title)
    if trigger:
        after = title[trigger.end():].strip()
        # Strip leading punctuation / separators
        after = re.sub(r'^[\s\-–:,]+', '', after)
        if after:
            candidate = after.split()[0] if len(after.split()) == 1 else after
            result = normalize_deadline(candidate)
            if result and _ISO_RE.match(result):
                return result
            # Try the full remainder (handles "end of month", "next Friday", etc.)
            result = normalize_deadline(after)
            if result and _ISO_RE.match(result):
                return result

    # Strategy 2: standalone time expression anywhere in the title
    m = _STANDALONE_RE.search(title)
    if m:
        result = normalize_deadline(m.group(0))
        if result and _ISO_RE.match(result):
            return result

    return None
