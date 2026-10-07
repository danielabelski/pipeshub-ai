import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agents.actions.knowledge_graph.ops import values
from app.agents.actions.knowledge_graph.ops.entity_records import LOOKUP_FAILED_MSG
from app.exceptions.fastapi_responses import Status
from app.exceptions.graph_db_exceptions import PermissionVerificationUnavailableError
from app.modules.retrieval.entity_permissions import (
    EntityAccessContext,
    EntityAccessError,
)
from app.modules.retrieval.retrieval_service import (
    ENTITY_FILTER_NO_MATCH_MESSAGE,
    ENTITY_FILTER_UNAVAILABLE_MESSAGE,
    RetrievalService,
)


def _service() -> RetrievalService:
    svc = RetrievalService.__new__(RetrievalService)
    svc.logger = MagicMock()
    svc.graph_provider = AsyncMock()
    svc.vector_db_service = AsyncMock()
    return svc


def _hits(*rows: tuple[str, str, str], truncated: bool = False) -> dict:
    return {
        "hits": [{"recordId": r, "virtualRecordId": v, "connectorId": c} for r, v, c in rows],
        "truncated": truncated,
    }


async def test_entity_filters_narrow_the_record_id_search_without_widening_scope(monkeypatch):
    svc = _service()
    scope = AsyncMock(return_value=(None, {"v1": "r1", "v2": "r2"}, {"_key": "u1"}))
    monkeypatch.setattr(svc, "_resolve_search_scope", scope)
    monkeypatch.setattr(svc, "_execute_parallel_searches", AsyncMock(return_value=[]))
    svc.graph_provider.get_records_for_named_entities = AsyncMock(
        return_value=_hits(("r1", "v1", "a"), ("r-private", "v-private", "a")),
    )

    await svc.search_with_filters(
        ["q"], "u1", "org",
        filter_groups={"Departments": ["Legal"], "entityFilters": {"kinds": ["organization"], "name": "Acme"}},
    )

    assert scope.await_args.args == ("u1", "org", {"departments": ["Legal"]}, None)
    assert scope.await_args.kwargs == {}
    must = svc.vector_db_service.filter_collection.await_args.kwargs["must"]
    assert must == {"orgId": "org", "virtualRecordId": ["v1"]}


async def test_a_reused_filter_dict_keeps_its_entity_filter(monkeypatch):
    svc = _service()
    monkeypatch.setattr(svc, "_resolve_search_scope", AsyncMock(return_value=(None, {"v1": "r1"}, None)))
    monkeypatch.setattr(svc, "_execute_parallel_searches", AsyncMock(return_value=[]))
    svc.graph_provider.get_records_for_named_entities = AsyncMock(return_value=_hits(("r1", "v1", "a")))
    groups = {"entityFilters": {"entityIds": ["e1"]}, "Departments": ["Legal"]}

    for _ in range(2):
        await svc.search_with_filters(["q"], "u1", "org", filter_groups=groups)

    assert groups == {"entityFilters": {"entityIds": ["e1"]}, "Departments": ["Legal"]}
    assert svc.graph_provider.get_records_for_named_entities.await_count == 2


async def test_entity_filters_keep_container_scoping(monkeypatch):
    svc = _service()
    containers = MagicMock(is_empty=False)
    monkeypatch.setattr(svc, "_resolve_search_scope", AsyncMock(return_value=(containers, {}, {"_key": "u1"})))
    clauses = MagicMock(return_value=({"orgId": "org"}, {"connectorIds": ["a"]}))
    monkeypatch.setattr(svc, "_build_container_clauses", clauses)
    adjudicate = AsyncMock(return_value=([], {}, False))
    monkeypatch.setattr(svc, "_search_and_adjudicate", adjudicate)
    svc.graph_provider.get_records_for_named_entities = AsyncMock(
        return_value=_hits(("r1", "v1", "a"), ("r2", "v2", "b")),
    )

    await svc.search_with_filters(["q"], "u1", "org", filter_groups={"entityFilters": {"entityIds": ["e1"]}})

    assert clauses.call_args.args[2] == ["v1", "v2"]
    adjudicate.assert_awaited_once()


async def test_entity_filters_intersect_a_tools_record_ids(monkeypatch):
    svc = _service()
    monkeypatch.setattr(svc, "_resolve_search_scope", AsyncMock(return_value=(None, {"v1": "r1", "v2": "r2"}, None)))
    monkeypatch.setattr(svc, "_execute_parallel_searches", AsyncMock(return_value=[]))
    svc.graph_provider.get_records_for_named_entities = AsyncMock(
        return_value=_hits(("r1", "v1", "a"), ("r2", "v2", "a")),
    )

    await svc.search_with_filters(
        ["q"], "u1", "org", filter_groups={"entityFilters": {"entityIds": ["e1"]}},
        virtual_record_ids_from_tool=["v2", "v7"],
    )

    must = svc.vector_db_service.filter_collection.await_args.kwargs["must"]
    assert must["virtualRecordId"] == ["v2"]


@pytest.mark.parametrize(
    ("graph_reply", "message", "status"),
    [
        ({"return_value": _hits(truncated=True)}, "filter too broad, narrow it", Status.ERROR),
        ({"return_value": _hits()}, ENTITY_FILTER_NO_MATCH_MESSAGE, Status.EMPTY_RESPONSE),
        ({"side_effect": RuntimeError("graph down")}, ENTITY_FILTER_UNAVAILABLE_MESSAGE, Status.ERROR),
    ],
)
async def test_an_unusable_entity_filter_never_loads_the_users_scope(monkeypatch, graph_reply, message, status):
    svc = _service()
    scope = AsyncMock()
    monkeypatch.setattr(svc, "_resolve_search_scope", scope)
    svc.graph_provider.get_records_for_named_entities = AsyncMock(**graph_reply)

    response = await svc.search_with_filters(["q"], "u1", "org", filter_groups={"entityFilters": {"kinds": ["date"]}})

    assert (response["message"], response["status"]) == (message, status.value)
    scope.assert_not_awaited()
    svc.vector_db_service.filter_collection.assert_not_called()


def _context(*app_ids: str) -> EntityAccessContext:
    return EntityAccessContext(
        org_id="org", user_key="uk", app_level_app_ids=frozenset(app_ids),
        record_level_app_ids=frozenset(), record_group_ids=frozenset(), app_names={},
    )


def _state(graph) -> dict:
    return {"graph_provider": graph, "org_id": "org", "user_id": "u1", "config_service": object()}


@pytest.fixture
def tool_env(monkeypatch):
    monkeypatch.setattr(values, "_named_entities_enabled", AsyncMock(return_value=True))
    context = AsyncMock(return_value=_context("app-in-scope"))
    monkeypatch.setattr(values, "load_entity_access_context", context)
    return context


async def test_find_records_by_value_checks_only_in_scope_hits(tool_env):
    graph = AsyncMock()
    graph.get_records_for_named_entities = AsyncMock(return_value=_hits(
        ("r1", "v1", "app-in-scope"), ("r-denied", "v2", "app-in-scope"), ("r-other-app", "v3", "app-elsewhere"),
    ))
    graph.filter_accessible_record_ids = AsyncMock(return_value={"r1"})

    ok, body = await values.execute_find_records_by_value(_state(graph), kinds=["currency"], amount_min=10, currency="USD")

    assert ok
    assert json.loads(body)["records"] == [{"virtualRecordId": "v1", "recordId": "r1"}]
    assert graph.filter_accessible_record_ids.await_args.args == (["r1", "r-denied"], "u1", "org")
    graph.get_accessible_virtual_record_ids.assert_not_called()
    query = graph.get_records_for_named_entities.await_args.args[2]
    assert (query.currency, query.amount_min) == ("USD", 10)


async def test_find_records_by_value_rejects_more_readable_records_than_it_returns(tool_env):
    graph = AsyncMock()
    rows = [(f"r{i}", f"v{i}", "app-in-scope") for i in range(400)]
    graph.get_records_for_named_entities = AsyncMock(return_value=_hits(*rows))
    graph.filter_accessible_record_ids = AsyncMock(side_effect=lambda ids, *_: set(ids))

    ok, message = await values.execute_find_records_by_value(_state(graph), kinds=["organization"])

    assert (ok, message) == (False, values.TOO_MANY_RECORDS_MSG)
    assert graph.filter_accessible_record_ids.await_count == 1


async def test_find_records_by_value_returns_exactly_a_full_page_after_checking_every_batch(tool_env):
    graph = AsyncMock()
    rows = [(f"r{i}", f"v{i}", "app-in-scope") for i in range(300)]
    graph.get_records_for_named_entities = AsyncMock(return_value=_hits(*rows))
    readable = {f"r{i}" for i in range(0, 300, 6)}
    graph.filter_accessible_record_ids = AsyncMock(side_effect=lambda ids, *_: set(ids) & readable)

    ok, body = await values.execute_find_records_by_value(_state(graph), kinds=["organization"])

    payload = json.loads(body)
    assert ok and len(payload["records"]) == 50 and "message" not in payload
    assert graph.filter_accessible_record_ids.await_count == 2


@pytest.mark.parametrize(
    "failure",
    [
        {"context": EntityAccessError("no user")},
        {"lookup": RuntimeError("graph down")},
        {"permissions": PermissionVerificationUnavailableError("graph down")},
    ],
)
async def test_find_records_by_value_fails_when_access_cannot_be_resolved(tool_env, failure):
    graph = AsyncMock()
    graph.get_records_for_named_entities = AsyncMock(
        side_effect=failure.get("lookup"), return_value=_hits(("r1", "v1", "app-in-scope")),
    )
    graph.filter_accessible_record_ids = AsyncMock(side_effect=failure.get("permissions"), return_value={"r1"})
    if "context" in failure:
        tool_env.side_effect = failure["context"]

    ok, message = await values.execute_find_records_by_value(_state(graph), kinds=["organization"])

    assert (ok, message) == (False, LOOKUP_FAILED_MSG)


async def test_find_records_by_value_is_off_with_the_labs_flag(monkeypatch):
    monkeypatch.setattr(values, "_named_entities_enabled", AsyncMock(return_value=False))
    graph = AsyncMock()

    ok, message = await values.execute_find_records_by_value(_state(graph), kinds=["organization"])

    assert not ok
    assert message == "Named-entity lookup is not enabled."
    graph.get_records_for_named_entities.assert_not_called()
