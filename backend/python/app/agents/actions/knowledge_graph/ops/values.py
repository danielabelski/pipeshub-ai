"""Find accessible records that mention a typed named-entity value."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from app.agents.actions.knowledge_graph.ops.entity_discovery import (
    NAMED_ENTITIES_DISABLED_MSG,
    _named_entities_enabled,
)
from app.agents.actions.knowledge_graph.ops.entity_filters import (
    load_entity_access_context,
)
from app.agents.actions.knowledge_graph.ops.entity_records import LOOKUP_FAILED_MSG
from app.exceptions.graph_db_exceptions import PermissionVerificationUnavailableError
from app.modules.retrieval.entity_filters import (
    EntityFilterHit,
    EntityFilterResolver,
    EntityFilterUnavailableError,
)
from app.modules.retrieval.entity_permissions import EntityAccessError

if TYPE_CHECKING:
    from app.modules.agents.qna.chat_state import ChatState
    from app.services.graph_db.interface.graph_db_provider import IGraphDBProvider

logger = logging.getLogger(__name__)

_MAX_ROWS = 50
# Hits checked per permission query; the check stops at the first readable
# record past _MAX_ROWS instead of checking every match.
_CHECK_BATCH = 250
TOO_MANY_RECORDS_MSG = (
    f"More than {_MAX_ROWS} accessible records match; narrow the filter "
    "(a tighter range, a name, or fewer kinds)."
)


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
        return False, NAMED_ENTITIES_DISABLED_MSG
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
    try:
        match = await EntityFilterResolver(graph).match(org_id, raw)
        if match.error:
            return False, match.error
        if not match.constrained:
            return False, "Pass a kind, name, date, amount, or quantity."
        context = await load_entity_access_context(state)
        in_scope = [hit for hit in match.hits if hit.connector_id in context.app_ids]
        rows = await _readable_records(graph, in_scope, user_id, org_id)
    except (EntityAccessError, EntityFilterUnavailableError, PermissionVerificationUnavailableError):
        logger.warning("find_records_by_value failed", exc_info=True)
        return False, LOOKUP_FAILED_MSG
    if rows is None:
        return False, TOO_MANY_RECORDS_MSG
    if not rows:
        return True, json.dumps({"status": "success", "records": [], "message": "No accessible records matched"})
    return True, json.dumps({"status": "success", "records": rows})


async def _readable_records(
    graph: "IGraphDBProvider", hits: list[EntityFilterHit], user_id: str, org_id: str,
) -> list[dict[str, str]] | None:
    """The readable hits, or None once more than ``_MAX_ROWS`` are readable."""
    unique = list({hit.record_id: hit for hit in hits}.values())
    rows: list[dict[str, str]] = []
    for start in range(0, len(unique), _CHECK_BATCH):
        batch = unique[start:start + _CHECK_BATCH]
        readable = await graph.filter_accessible_record_ids(
            [hit.record_id for hit in batch], user_id, org_id,
        )
        for hit in batch:
            if hit.record_id not in readable:
                continue
            if len(rows) == _MAX_ROWS:
                return None
            rows.append({"virtualRecordId": hit.virtual_record_id, "recordId": hit.record_id})
    return rows
