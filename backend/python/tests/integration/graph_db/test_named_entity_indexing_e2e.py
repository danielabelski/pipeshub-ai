"""Indexing to query on a real Neo4j and a real ArangoDB, with a scripted model.

A record's blocks go through the production extraction stage (deterministic
recognizers plus the agent loop) and persist stage, then the agent tool reads
them back. Only the model and the user's accessible-record map are fakes.

  docker compose -f deployment/docker-compose/docker-compose.integration.graph-db.yml \\
    up -d --wait neo4j-graph-it arango-graph-it
  cd backend/python && pytest tests/integration/graph_db/test_named_entity_indexing_e2e.py -m integration
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.agent_loop_lib.core.messages import AssistantMessage, ToolCall
from app.agent_loop_lib.core.responses import StopReason
from app.agents.actions.knowledge_graph.ops import values
from app.connectors.core.base.data_store.graph_data_store import GraphDataStore
from app.models.blocks import Block, BlocksContainer, BlockType
from app.modules.named_entities import stage
from app.modules.named_entities.strategies.agent.transport import SlottedTransport
from tests.integration.graph_db.test_named_entity_graph_contract import (  # noqa: F401
    _ms,
    _records,
    graph,
)
from tests.unit.agents.adapter.support.scripted_transport import (
    ScriptedStep,
    ScriptedTransport,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

_TEXT = "Acme Robotics signed a $25,000 order on March 3, 2026. Card 4111 1111 1111 1111 was declined."


def _call(name: str, arguments: dict, call_id: str) -> ScriptedStep:
    return ScriptedStep(
        message=AssistantMessage(content="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)]),
        stop_reason=StopReason.TOOL_USE,
    )


def _model() -> ScriptedTransport:
    return ScriptedTransport([
        _call("submit_entities", {
            "entities": [{"text": "Acme Robotics", "block": 0, "kind": "organization", "normalized": ""}],
        }, "1"),
        _call("finish", {"reason": "done"}, "2"),
    ])


def _record(record_id: str, org_id: str) -> SimpleNamespace:
    block = Block(type=BlockType.TEXT, data=_TEXT, index=0)
    return SimpleNamespace(
        id=record_id,
        org_id=org_id,
        record_name="Order confirmation",
        record_type=SimpleNamespace(value="FILE"),
        block_containers=BlocksContainer(blocks=[block], block_groups=[]),
        source_updated_at=_ms(2026, 3, 10),
        semantic_metadata=None,
    )


class _UserGraph:
    """The real provider, seen by a user who can read only ``readable``."""

    def __init__(self, provider, readable: dict[str, str]) -> None:
        self._provider = provider
        self._readable = readable

    async def get_accessible_virtual_record_ids(self, **_):
        return dict(self._readable)

    def __getattr__(self, name):
        return getattr(self._provider, name)


async def _index(provider, record) -> None:
    ctx = SimpleNamespace(record=record)
    with patch.object(stage, "indexing_llm", AsyncMock(return_value=(None, SlottedTransport(_model()), "fake", "fake-agent"))):
        extraction = await stage.extract_named_entities(ctx, config_service=None, enabled=True)
    assert (extraction.strategy, extraction.status) == ("agent", "COMPLETED")
    await stage.persist_named_entities(
        ctx, extraction, graph_provider=provider, graph_data_store=GraphDataStore(stage.logger, provider), enabled=True,
    )


async def test_indexed_values_are_queryable_and_only_for_readable_records(graph) -> None:  # noqa: F811
    provider, org_id, _ = graph
    readable, private = await _records(provider, org_id, 2)
    await _index(provider, _record(readable, org_id))
    await _index(provider, _record(private, org_id))

    stored = await provider.get_named_entities_for_record(readable)
    kinds = {row["kind"] for row in stored}
    assert {"organization", "currency", "date"} <= kinds
    assert not any("4111" in json.dumps(row, default=str) for row in stored)
    doc = await provider.get_document(readable, "records")
    assert (doc["entityExtractionStatus"], doc["entityExtractionStrategy"]) == ("COMPLETED", "agent")

    state = {"graph_provider": _UserGraph(provider, {"v-readable": readable}), "org_id": org_id, "user_id": "u"}
    with patch.object(values, "_named_entities_enabled", AsyncMock(return_value=True)):
        for query in (
            {"amount_min": 20_000, "amount_max": 30_000, "currency": "USD"},
            {"date_from": _ms(2026, 3, 1), "date_to": _ms(2026, 3, 31)},
            {"kinds": ["organization"], "name": "acme"},
        ):
            ok, body = await values.execute_find_records_by_value(state, **query)
            assert ok, body
            assert json.loads(body)["records"] == [{"virtualRecordId": "v-readable", "recordId": readable}], query

        ok, body = await values.execute_find_records_by_value(state, amount_min=1, amount_max=100, currency="USD")
        assert ok and json.loads(body)["records"] == []
