"""In-memory staging for one extraction run. Resubmitting a span is an upsert."""

from __future__ import annotations

from app.modules.named_entities.mentions import RawMention


class StagingSet:
    def __init__(self) -> None:
        self._items: dict[tuple, RawMention] = {}
        self.submitted = 0
        self.rejected = 0
        self.empty_submits = 0

    def upsert(self, mention: RawMention) -> bool:
        key = (mention.block_index, mention.char_start, mention.char_end, mention.kind.value)
        is_new = key not in self._items
        self._items[key] = mention
        return is_new

    def mentions(self) -> list[RawMention]:
        return list(self._items.values())

    def __len__(self) -> int:
        return len(self._items)
