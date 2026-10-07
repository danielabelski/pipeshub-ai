"""Block text the recognizers and the agent read. Code blocks are skipped."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TextUnit:
    block_index: int
    block_id: str
    text: str


def _block_type(block) -> str:
    raw = getattr(block, "type", "")
    return getattr(raw, "value", raw) or ""


def _block_text(block) -> str:
    kind = _block_type(block)
    data = getattr(block, "data", None)
    if kind == "text" and isinstance(data, str):
        return data
    if kind == "table_row" and isinstance(data, dict):
        text = data.get("row_natural_language_text") or ""
        return text if isinstance(text, str) else ""
    if kind == "image":
        meta = getattr(block, "image_metadata", None)
        description = getattr(meta, "description", None) if meta is not None else None
        return description if isinstance(description, str) else ""
    return ""


def text_units_from_blocks(blocks) -> list[TextUnit]:
    units: list[TextUnit] = []
    for index, block in enumerate(blocks or []):
        text = _block_text(block).strip()
        if not text:
            continue
        units.append(
            TextUnit(
                block_index=getattr(block, "index", index),
                block_id=str(getattr(block, "id", "") or ""),
                text=text,
            )
        )
    return units
