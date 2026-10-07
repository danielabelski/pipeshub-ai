"""Resolve typed entity filters into the records a search may see.

The filter is parsed before retrieval lowercases keys, then intersected with
the accessible virtual-record map. A truncated match is an error, not a
partial answer: a capped set would silently drop records the filter named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.named_entities.graph_ops import NamedEntityQuery

_LOW = -1e308
_HIGH = 1e308


@dataclass(frozen=True, slots=True)
class EntityFilterDecision:
    allowed: dict[str, str]
    error: str | None = None


class EntityFilterResolver:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def resolve(
        self,
        org_id: str,
        raw: dict[str, Any],
        accessible: dict[str, str],
    ) -> EntityFilterDecision:
        if not isinstance(raw, dict):
            return EntityFilterDecision(allowed={}, error="entityFilters must be an object")
        entity_ids = [str(item) for item in (raw.get("entityIds") or []) if item]
        query = None if entity_ids else _query_from(raw)
        if not entity_ids and query is None:
            return EntityFilterDecision(allowed=accessible)
        hits = await self._graph.get_records_for_named_entities(
            org_id,
            entity_ids or None,
            query,
        )
        if hits.get("truncated"):
            return EntityFilterDecision(allowed={}, error="filter too broad, narrow it")
        record_to_virtual = {record_id: virtual for virtual, record_id in accessible.items()}
        allowed: dict[str, str] = {}
        for row in hits.get("hits") or []:
            record_id = str(row.get("recordId") or "")
            virtual = record_to_virtual.get(record_id)
            if virtual:
                allowed[virtual] = record_id
        return EntityFilterDecision(allowed=allowed)


def _bound(raw: dict[str, Any], low_key: str, high_key: str) -> tuple[float | None, float | None]:
    low = raw.get(low_key)
    high = raw.get(high_key)
    if low is None and high is None:
        return None, None
    return (_LOW if low is None else low), (_HIGH if high is None else high)


def _query_from(raw: dict[str, Any]) -> NamedEntityQuery | None:
    kinds = list(raw.get("kinds") or [])
    date = raw.get("mentionedDate") or {}
    amount = raw.get("amount") or {}
    quantity = raw.get("quantity") or {}
    percent = raw.get("percent") or {}
    date_start, date_end = _bound({"from": date.get("from"), "to": date.get("to")}, "from", "to") if date else (None, None)
    amount_min, amount_max = _bound(amount, "min", "max") if amount else (None, None)
    if amount.get("currency") and amount_min is None:
        amount_min, amount_max = _LOW, _HIGH
    quantity_min, quantity_max = _bound(quantity, "min", "max") if quantity else (None, None)
    if quantity.get("dimension") and quantity_min is None:
        quantity_min, quantity_max = _LOW, _HIGH
    percent_min, percent_max = _bound(percent, "min", "max") if percent else (None, None)
    name = raw.get("name") or ""
    if not any([kinds, name, date_start is not None, amount_min is not None, quantity_min is not None, percent_min is not None]):
        return None
    return NamedEntityQuery(
        kinds=kinds or None,
        name_prefix=str(name),
        date_start_ms=date_start,
        date_end_ms=date_end,
        amount_min=amount_min,
        amount_max=amount_max,
        currency=str(amount.get("currency") or ""),
        quantity_min=quantity_min,
        quantity_max=quantity_max,
        dimension=str(quantity.get("dimension") or ""),
        percent_min=percent_min,
        percent_max=percent_max,
        limit=5000,
    )
