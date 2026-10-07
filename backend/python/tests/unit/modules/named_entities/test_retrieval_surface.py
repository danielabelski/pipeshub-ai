import json
from unittest.mock import AsyncMock, MagicMock

from app.agents.actions.knowledge_graph.ops import values
from app.exceptions.fastapi_responses import Status
from app.modules.retrieval.entity_filters import (
    EntityFilterDecision,
    EntityFilterResolver,
)
from app.modules.retrieval.retrieval_service import RetrievalService


def _service() -> RetrievalService:
    svc = RetrievalService.__new__(RetrievalService)
    svc.logger = MagicMock()
    svc.graph_provider = AsyncMock()
    svc.vector_db_service = AsyncMock()
    return svc


async def test_entity_filters_are_taken_out_before_keys_are_lowercased(monkeypatch):
    svc = _service()
    scope = AsyncMock(return_value=(None, {"v1": "r1", "v2": "r2"}, {"_key": "u1"}))
    monkeypatch.setattr(svc, "_resolve_search_scope", scope)
    seen = {}

    async def resolve(self, org_id, raw, accessible):
        seen.update(raw=raw, accessible=dict(accessible))
        return EntityFilterDecision(allowed={})

    monkeypatch.setattr(EntityFilterResolver, "resolve", resolve)
    raw = {"kinds": ["organization"], "name": "Acme"}

    response = await svc.search_with_filters(
        ["q"], "u1", "org", filter_groups={"Departments": ["Legal"], "entityFilters": raw},
    )

    filters = scope.await_args.args[2]
    assert filters == {"departments": ["Legal"]}
    assert scope.await_args.kwargs["force_record_scope"] is True
    assert seen == {"raw": raw, "accessible": {"v1": "r1", "v2": "r2"}}
    assert response["status"] == Status.ACCESSIBLE_RECORDS_NOT_FOUND.value


async def test_a_truncated_entity_filter_is_reported_not_searched(monkeypatch):
    svc = _service()
    monkeypatch.setattr(svc, "_resolve_search_scope", AsyncMock(return_value=(None, {"v1": "r1"}, None)))
    svc.graph_provider.get_records_for_named_entities = AsyncMock(return_value={"hits": [], "truncated": True})

    response = await svc.search_with_filters(["q"], "u1", "org", filter_groups={"entityFilters": {"kinds": ["date"]}})

    assert response["message"] == "filter too broad, narrow it"
    svc.vector_db_service.filter_collection.assert_not_called()


def _state(graph) -> dict:
    return {"graph_provider": graph, "org_id": "org", "user_id": "u1", "config_service": object()}


async def test_find_records_by_value_returns_only_accessible_records(monkeypatch):
    monkeypatch.setattr(values, "_named_entities_enabled", AsyncMock(return_value=True))
    graph = AsyncMock()
    graph.get_accessible_virtual_record_ids = AsyncMock(return_value={"v1": "r1"})
    graph.get_records_for_named_entities = AsyncMock(return_value={
        "hits": [{"recordId": "r1"}, {"recordId": "r-private"}], "truncated": False,
    })

    ok, body = await values.execute_find_records_by_value(_state(graph), kinds=["currency"], amount_min=10, currency="USD")

    assert ok
    assert json.loads(body)["records"] == [{"virtualRecordId": "v1", "recordId": "r1"}]
    assert graph.get_accessible_virtual_record_ids.await_args.kwargs == {"user_id": "u1", "org_id": "org", "filters": {}}
    query = graph.get_records_for_named_entities.await_args.args[2]
    assert (query.currency, query.amount_min) == ("USD", 10)


async def test_find_records_by_value_is_off_with_the_labs_flag(monkeypatch):
    monkeypatch.setattr(values, "_named_entities_enabled", AsyncMock(return_value=False))
    graph = AsyncMock()

    ok, message = await values.execute_find_records_by_value(_state(graph), kinds=["organization"])

    assert not ok
    assert message == "Named-entity lookup is not enabled."
    graph.get_records_for_named_entities.assert_not_called()
