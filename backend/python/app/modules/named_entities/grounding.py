"""Align a model's verbatim span to a block. Offsets come from this search."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.modules.named_entities.text import TextUnit

_WS = re.compile(r"\s+")
FUZZY_MIN = 0.85


def _fold(text: str) -> str:
    return _WS.sub(" ", text).casefold().strip()


class Grounder:
    def __init__(self, units: list[TextUnit]) -> None:
        self._by_index = {unit.block_index: unit for unit in units}

    def align(self, block_index: int, surface: str) -> tuple[int, int, str, str] | None:
        """Return ``(char_start, char_end, matched_surface, grounding)`` or None."""
        unit = self._by_index.get(block_index)
        text = (surface or "").strip()
        if unit is None or not text or len(text) > 256:
            return None
        exact = unit.text.find(text)
        if exact >= 0:
            return exact, exact + len(text), unit.text[exact : exact + len(text)], "exact"
        folded_text = unit.text.casefold()
        folded_needle = text.casefold()
        folded = folded_text.find(folded_needle)
        if folded >= 0:
            return folded, folded + len(text), unit.text[folded : folded + len(text)], "exact"
        return self._fuzzy(unit.text, text)

    def nearest(self, block_index: int, surface: str) -> str:
        unit = self._by_index.get(block_index)
        if unit is None:
            return ""
        needle = _fold(surface)
        if not needle:
            return ""
        words = unit.text.split()
        best = ""
        best_ratio = 0.0
        width = max(1, len(needle.split()))
        for size in (width, width + 1):
            for i in range(0, max(1, len(words) - size + 1)):
                window = " ".join(words[i : i + size])
                ratio = SequenceMatcher(None, needle, _fold(window)).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best = window
        return best if best_ratio >= 0.5 else ""

    def _fuzzy(self, text: str, surface: str) -> tuple[int, int, str, str] | None:
        needle = _fold(surface)
        words = text.split()
        if not words or not needle:
            return None
        width = max(1, len(needle.split()))
        best: tuple[float, int, int] | None = None
        offset = 0
        spans: list[tuple[int, int]] = []
        for word in words:
            start = text.find(word, offset)
            if start < 0:
                start = offset
            spans.append((start, start + len(word)))
            offset = start + len(word)
        for size in (width, max(1, width - 1), width + 1):
            for i in range(0, len(spans) - size + 1):
                start = spans[i][0]
                end = spans[i + size - 1][1]
                window = text[start:end]
                ratio = SequenceMatcher(None, needle, _fold(window)).ratio()
                if best is None or ratio > best[0]:
                    best = (ratio, start, end)
        if best is None or best[0] < FUZZY_MIN:
            return None
        _, start, end = best
        return start, end, text[start:end], "fuzzy"
