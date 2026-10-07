"""Find accessible records that mention a typed named-entity value."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from app.agents.actions.knowledge_graph.ops.entity_discovery import (
    _named_entities_enabled,
)
from app.modules.retrieval.entity_filters import EntityFilterResolver

if TYPE_CHECKING:
    from app.modules.agents.qna.chat_state import ChatState

_MAX_ROWS = 50


async def execute_find_records_by_value(
    state: "ChatState",
    *,
    kinds: list[str] | None = None,
    name: str | None = None,
    date_from: int | None = None,
    date_to: int | None = None,
    amount_min: float | None = None,
    amount_max: float | None = None,
    currency: str | None = None,
    quantity_min: float | None = None,
    quantity_max: float | None = None,
    dimension: str | None = None,
) -> tuple[bool, str]:
    if not state or not state.get("graph_provider"):
        return False, "Knowledge graph tool state not initialized."
    if not await _named_entities_enabled(state):
        return False, "Named-entity lookup is not enabled."
    raw: dict[str, Any] = {}
    if kinds:
        raw["kinds"] = kinds
    if name:
        raw["name"] = name
    if date_from is not None or date_to is not None:
        raw["mentionedDate"] = {"from": date_from, "to": date_to}
    if amount_min is not None or amount_max is not None or currency:
        raw["amount"] = {"min": amount_min, "max": amount_max, "currency": currency or ""}
    if quantity_min is not None or quantity_max is not None or dimension:
        raw["quantity"] = {"min": quantity_min, "max": quantity_max, "dimension": dimension or ""}
    if not raw:
        return False, "Pass a kind, name, date, amount, or quantity."
    org_id = state.get("org_id") or ""
    user_id = state.get("user_id") or ""
    graph = state["graph_provider"]
    accessible = await graph.get_accessible_virtual_record_ids(
        user_id=user_id, org_id=org_id, filters={},
    )
    decision = await EntityFilterResolver(graph).resolve(org_id, raw, accessible or {})
    if decision.error:
        return False, decision.error
    rows = [
        {"virtualRecordId": virtual, "recordId": record}
        for virtual, record in list(decision.allowed.items())[:_MAX_ROWS]
    ]
    if not rows:
        return True, json.dumps({"status": "success", "records": [], "message": "No accessible records matched"})
    payload: dict[str, Any] = {"status": "success", "records": rows}
    if len(decision.allowed) > _MAX_ROWS:
        payload["message"] = "Showing the first page; narrow the filter for the rest."
    return True, json.dumps(payload)
