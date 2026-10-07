"""Resolve typed entity filters into the records that mention a match.

Matching grants nothing. It is one capped graph lookup, and each caller then
checks only these hits against what the user may read, so a filter never
loads the user's whole accessible corpus to intersect a few thousand records.
A truncated match is an error, not a partial answer: a capped set would
silently drop records the filter named.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.named_entities.graph_ops import NamedEntityQuery

_LOW = -1e308
_HIGH = 1e308

TOO_BROAD_MESSAGE = "filter too broad, narrow it"


class EntityFilterUnavailableError(Exception):
    """The named-entity graph could not answer; never the same as no match."""


@dataclass(frozen=True, slots=True)
class EntityFilterHit:
    record_id: str
    virtual_record_id: str
    connector_id: str


@dataclass(frozen=True, slots=True)
class EntityFilterMatch:
    hits: tuple[EntityFilterHit, ...] = ()
    error: str | None = None
    # False when the filter names no constraint, so it must not narrow anything.
    constrained: bool = True

    @property
    def virtual_record_ids(self) -> list[str]:
        return list(dict.fromkeys(hit.virtual_record_id for hit in self.hits if hit.virtual_record_id))


class EntityFilterResolver:
    def __init__(self, graph: Any) -> None:
        self._graph = graph

    async def match(self, org_id: str, raw: dict[str, Any]) -> EntityFilterMatch:
        if not isinstance(raw, dict):
            return EntityFilterMatch(error="entityFilters must be an object")
        entity_ids = [str(item) for item in (raw.get("entityIds") or []) if item]
        query = _query_from(raw)
        if not entity_ids and query is None:
            return EntityFilterMatch(constrained=False)
        try:
            result = await self._graph.get_records_for_named_entities(org_id, entity_ids or None, query)
        except Exception as exc:
            raise EntityFilterUnavailableError("named-entity lookup failed") from exc
        if result.get("truncated"):
            return EntityFilterMatch(error=TOO_BROAD_MESSAGE)
        hits = tuple(
            EntityFilterHit(
                record_id=str(row["recordId"]),
                virtual_record_id=str(row.get("virtualRecordId") or ""),
                connector_id=str(row.get("connectorId") or ""),
            )
            for row in result.get("hits") or []
            if row.get("recordId")
        )
        return EntityFilterMatch(hits=hits)


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
