"""Candidate spans for dates, money, percents and quantities.

A regex only proposes a span. The normalizer decides whether it is real,
so 'v1.2' never becomes money.
"""

from __future__ import annotations

import re

from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.mentions import RawMention
from app.modules.named_entities.normalizers.money import ISO_4217
from app.modules.named_entities.text import TextUnit

_MONTH = (
    r"January|February|March|April|May|June|July|August|September|October|November|December|"
    r"Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec"
)
_DATE = re.compile(
    rf"\b(?:Q[1-4]\s*(?:FY\s*)?\d{{2,4}}|FY\s?(?:\d{{4}}|\d{{2}})|(?:{_MONTH})\.?\s+\d{{1,2}}(?:(?:,\s*|\s+)\d{{4}})?|"
    rf"\d{{1,2}}\s+(?:{_MONTH})\b(?:(?:,\s*|\s+)\d{{4}})?|"
    rf"(?:{_MONTH})\.?\s+\d{{4}}|\d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?(?:Z|[+-]\d{{2}}:?\d{{2}})?)?|"
    rf"\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}}|(?:next|last|this)\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|week|month|quarter|year)|"
    rf"\d+\s+(?:days?|weeks?|months?|years?|hours?|minutes?))\b",
    re.IGNORECASE,
)
_CODES = "|".join(sorted(ISO_4217))
_NUMBER = r"\d{1,3}(?:[,.\u00a0 ]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?"
_SCALE = r"(?:\s?(?:bn|mm|[kKmMbB])\b)?"
_MONEY = re.compile(
    rf"(?<![A-Za-z0-9.])(?:"
    rf"(?:US\$|R\$|C\$|A\$|[$€£¥₹₩₽₺₪₫₱])\s?(?:{_NUMBER}){_SCALE}"
    rf"|(?:{_CODES})\s?(?:{_NUMBER}){_SCALE}"
    rf"|(?:{_NUMBER}){_SCALE}\s?(?:(?:{_CODES})\b|[€£₹]|(?i:dollar|euro|yen|rupee)s?\b))",
)
# No \b after "%": it is not a word character, so "12.5% " has no boundary there.
_PERCENT = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s?(?:%|(?:bps|percent|per\s?cent|pct)\b)", re.IGNORECASE)
_QUANTITY = re.compile(
    r"(?<!\d)\d+(?:\.\d+)?\s?(?:km/h|km|m|cm|mm|kg|g|lb|lbs|mi|ft|mph|kph|°C|°F|celsius|fahrenheit)\b",
    re.IGNORECASE,
)
_AGE = re.compile(r"\b\d{1,3}[\-\s]?year[\-\s]?old\b", re.IGNORECASE)


class ValueCandidateRecognizer:
    name = "value"

    def __init__(self, enabled: frozenset[EntityKind]) -> None:
        self._enabled = enabled

    def recognize(self, units: list[TextUnit]) -> list[RawMention]:
        found: list[RawMention] = []
        for unit in units:
            found.extend(self._scan(unit))
        return found

    def _add(
        self,
        out: list[RawMention],
        unit: TextUnit,
        kind: EntityKind,
        match: re.Match[str],
    ) -> None:
        if kind not in self._enabled:
            return
        surface = match.group(0)
        out.append(
            RawMention(
                kind=kind,
                surface=surface,
                block_index=unit.block_index,
                block_id=unit.block_id,
                char_start=match.start(),
                char_end=match.end(),
                extractor="value",
                evidence_score=0.8,
            )
        )

    def _scan(self, unit: TextUnit) -> list[RawMention]:
        out: list[RawMention] = []
        date_kinds = {EntityKind.DATE, EntityKind.DATE_RANGE, EntityKind.DATE_TIME, EntityKind.DURATION}
        if date_kinds & self._enabled:
            for match in _DATE.finditer(unit.text):
                surface = match.group(0)
                kind = _date_kind(surface)
                self._add(out, unit, kind, match)
        if EntityKind.CURRENCY in self._enabled:
            for match in _MONEY.finditer(unit.text):
                self._add(out, unit, EntityKind.CURRENCY, match)
        if EntityKind.PERCENTAGE in self._enabled:
            for match in _PERCENT.finditer(unit.text):
                self._add(out, unit, EntityKind.PERCENTAGE, match)
        if EntityKind.DIMENSION in self._enabled:
            for match in _QUANTITY.finditer(unit.text):
                self._add(out, unit, EntityKind.DIMENSION, match)
        if EntityKind.AGE in self._enabled:
            for match in _AGE.finditer(unit.text):
                self._add(out, unit, EntityKind.AGE, match)
        return out


def _date_kind(surface: str) -> EntityKind:
    lowered = surface.casefold()
    if re.search(r"\d+\s+(?:days?|weeks?|months?|years?|hours?|minutes?)", lowered):
        return EntityKind.DURATION
    if lowered.startswith(("q", "fy")) or " to " in lowered or "/" in lowered and "fy" in lowered:
        return EntityKind.DATE_RANGE
    if re.fullmatch(r"(?:next|last|this)\s+(?:week|month|quarter|year)", lowered):
        return EntityKind.DATE_RANGE
    if "t" in lowered and ":" in lowered:
        return EntityKind.DATE_TIME
    return EntityKind.DATE
