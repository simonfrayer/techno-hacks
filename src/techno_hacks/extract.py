"""Turn a spoken sentence into (name, expiry, tablet count).

People say things like:
    "Ibuprofen 400, expires March 2027, 20 tablets"
    "Paracetamol, 03/2027, thirty tablets"
    "Aspirin expiry 2026-11 count 50"

Rule-based on purpose: predictable, offline and easy to test. Expiry is
normalised to YYYY-MM (or YYYY-MM-DD when a day was said).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
    "seventy": 70, "eighty": 80, "ninety": 90,
}

_COUNT_UNITS = r"(?:tablets?|tabs?|pills?|capsules?|caps|pieces?|pcs|count)"
_MONTH_RE = "|".join(sorted(MONTHS, key=len, reverse=True))


@dataclass(frozen=True)
class Details:
    name: str
    expiry: str
    quantity: str


class NotUnderstood(ValueError):
    """The sentence did not contain all three details. The message is short enough for the screen."""


def _extend_number(value: Optional[int], word: str) -> Optional[int]:
    """value with `word` appended, or None if the word starts a new number."""
    if word in _TENS:
        if value is None or (value >= 100 and value % 100 == 0):
            return (value or 0) + _TENS[word]
        return None
    if word in _UNITS:
        if value is None:
            return _UNITS[word]
        if _UNITS[word] < 10 and ((value >= 20 and value % 10 == 0 and value % 100 != 0) or
                                  (value >= 100 and value % 100 == 0)):
            return value + _UNITS[word]
        if value >= 100 and value % 100 == 0:
            return value + _UNITS[word]
        return None
    if word == "hundred":
        if value is None:
            return 100
        if value < 10:
            return value * 100
        return None
    return None


def _words_to_numbers(text: str) -> str:
    """'thirty two tablets' -> '32 tablets', 'March twenty twenty seven' -> 'March 2027'."""
    tokens = re.findall(r"[A-Za-z]+|[^A-Za-z]+", text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        value: Optional[int] = None
        j = i
        end = i
        while j < len(tokens):
            tok = tokens[j]
            nxt = _extend_number(value, tok.lower()) if tok[0].isalpha() else None
            if nxt is not None:
                value = nxt
                j += 1
                end = j
                continue
            # a space or hyphen may join two number words ("thirty-two")
            if value is not None and re.fullmatch(r"[ \-]+", tok) and j + 1 < len(tokens) \
                    and _extend_number(value, tokens[j + 1].lower()) is not None:
                j += 1
                continue
            break
        if value is not None:
            out.append(str(value))
            i = end
        else:
            out.append(tokens[i])
            i += 1
    joined = "".join(out)
    # spoken years: "March 20 27" -> "March 2027"
    return re.sub(rf"\b({_MONTH_RE})(\.?,?\s+(?:of\s+)?)20\s+(\d{{2}})\b", r"\g<1>\g<2>20\g<3>", joined, flags=re.I)


def _year(y: str) -> int:
    n = int(y)
    return 2000 + n if n < 100 else n


def _find_expiry(text: str) -> Optional[tuple[str, int, int]]:
    """(normalised expiry, start, end) of the first date-like phrase."""
    patterns: list[tuple[str, callable]] = [
        # 2027-03-15 / 2027-03
        (r"\b(20\d{2})[-/.](\d{1,2})(?:[-/.](\d{1,2}))?\b",
         lambda m: (int(m[1]), int(m[2]), int(m[3]) if m[3] else None)),
        # 15.03.2027 / 15/03/27
        (r"\b(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})\b",
         lambda m: (_year(m[3]), int(m[2]), int(m[1]))),
        # 03/2027 / 3/27
        (r"\b(\d{1,2})[./-](\d{2}|20\d{2})\b",
         lambda m: (_year(m[2]), int(m[1]), None)),
        # 15th of March 2027 / 15 March 2027
        (rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({_MONTH_RE})\.?,?\s+(\d{{2,4}})\b",
         lambda m: (_year(m[3]), MONTHS[m[2].lower()], int(m[1]))),
        # March 15th, 2027 / March 15 2027
        (rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d{{2}})\b",
         lambda m: (int(m[3]), MONTHS[m[1].lower()], int(m[2]))),
        # March 2027 / March of 2027 / March 27
        (rf"\b({_MONTH_RE})\.?,?\s+(?:of\s+)?(\d{{2,4}})\b",
         lambda m: (_year(m[2]), MONTHS[m[1].lower()], None)),
    ]
    best = None
    for pattern, parts in patterns:
        for m in re.finditer(pattern, text, flags=re.IGNORECASE):
            year, month, day = parts(m)
            if not (1 <= month <= 12) or not (2000 <= year <= 2099):
                continue
            if day is not None and not (1 <= day <= 31):
                continue
            norm = f"{year:04d}-{month:02d}" + (f"-{day:02d}" if day else "")
            if best is None or m.start() < best[1]:
                best = (norm, m.start(), m.end())
            break
    return best


def _find_quantity(text: str, taken: list[tuple[int, int]]) -> Optional[tuple[str, int, int]]:
    def free(s: int, e: int) -> bool:
        return all(e <= ts or s >= te for ts, te in taken)

    m = next((m for m in re.finditer(rf"\b(\d{{1,4}})\s*{_COUNT_UNITS}\b", text, re.I) if free(m.start(), m.end())), None)
    if m is None:
        m = next((m for m in re.finditer(rf"\b{_COUNT_UNITS}\s*:?\s*(\d{{1,4}})\b", text, re.I) if free(m.start(), m.end())), None)
    if m is None:
        # last free standalone number after the expiry, e.g. "..., March 2027, 20"
        after = max((e for _, e in taken), default=0)
        candidates = [m for m in re.finditer(r"\b(\d{1,4})\b", text) if m.start() >= after and free(m.start(), m.end())]
        m = candidates[-1] if candidates else None
    if m is None:
        return None
    return str(int(m[1])), m.start(), m.end()


_NAME_STOP = re.compile(
    r"\b(?:expir\w*|exp|best\s+before|use\s+by|valid\s+until|until|good\s+until|"
    r"quantity|count|amount|tablets?|pills?|capsules?)\b",
    re.IGNORECASE,
)


def parse_details(transcript: str) -> Details:
    """Extract the three details or raise :class:`NotUnderstood`."""
    text = _words_to_numbers(transcript.strip())
    if not text:
        raise NotUnderstood("Heard nothing")

    expiry = _find_expiry(text)
    if expiry is None:
        raise NotUnderstood("No expiry date")
    quantity = _find_quantity(text, [(expiry[1], expiry[2])])
    if quantity is None:
        raise NotUnderstood("No tablet count")

    cut = min(expiry[1], quantity[1])
    stop = _NAME_STOP.search(text)
    if stop and stop.start() < cut:
        cut = stop.start()
    name = text[:cut]
    name = re.sub(r"^\s*(?:name\s*(?:is|:)?|product\s*(?:is|:)?|medicine\s*(?:is|:)?)\s*", "", name, flags=re.I)
    name = re.sub(r"[\s,;:.\-]+$", "", name).strip(" ,;:.-")
    if not name:
        raise NotUnderstood("No product name")
    return Details(name=name, expiry=expiry[0], quantity=quantity[0])
