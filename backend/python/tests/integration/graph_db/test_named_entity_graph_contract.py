"""Named-entity graph methods against a real Neo4j and a real ArangoDB.

One parameterized contract: every case must return the same answer on both
backends. Writes go through the production NamedEntityGraphWriter, so the
strict Arango schemas and the Neo4j MERGE paths are exercised as shipped.

  docker compose -f deployment/docker-compose/docker-compose.integration.graph-db.yml \\
    up -d --wait neo4j-graph-it arango-graph-it
  cd backend/python && pytest tests/integration/graph_db/test_named_entity_graph_contract.py -m integration
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import pytest

from app.config.constants.arangodb import CollectionNames
from app.connectors.core.base.data_store.graph_data_store import GraphDataStore
from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.models import Mention, NamedEntity
from app.modules.named_entities.domain.values import DateRangeValue, MoneyValue
from app.modules.named_entities.graph_ops import NamedEntityQuery
from app.modules.named_entities.graph_writer import (
    NamedEntityGraphWriter,
    node_document,
)
from app.modules.named_entities.keys import named_entity_key
from app.modules.named_entities.resolution import ResolvedEntity
from app.utils.time_conversion import get_epoch_timestamp_in_ms
from tests.integration.real_graph import (
    backend_unavailable,
    connect_arango,
    connect_neo4j,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from app.services.graph_db.interface.graph_db_provider import IGraphDBProvider

pytestmark = [pytest.mark.integration, pytest.mark.timeout(300)]

logger = logging.getLogger("named-entity-graph-it")
ARANGO_DB = "named_entity_graph_it"
NODES = CollectionNames.NAMED_ENTITIES.value
EDGES = CollectionNames.MENTIONS_ENTITY.value
RECORDS = CollectionNames.RECORDS.value


@contextlib.asynccontextmanager
async def _backend(name: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[IGraphDBProvider]:
    try:
        provider = await (connect_neo4j(logger, monkeypatch) if name == "neo4j" else connect_arango(logger, ARANGO_DB))
    except Exception as exc:
        backend_unavailable(name, exc)
    try:
        yield provider
    finally:
        await provider.disconnect()


@pytest.fixture(params=["neo4j", "arango"])
async def graph(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[tuple[IGraphDBProvider, str, str]]:
    org_id = f"org-ne-{uuid.uuid4().hex[:10]}"
    async with _backend(request.param, monkeypatch) as provider:
        try:
            yield provider, org_id, request.param
        finally:
            await _cleanup(provider, org_id, request.param)


async def _cleanup(provider: IGraphDBProvider, org_id: str, backend: str) -> None:
    if backend == "neo4j":
        await provider.execute_query(
            "MATCH (n) WHERE n.orgId = $org DETACH DELETE n", {"org": org_id},
        )
        return
    await provider.execute_query(
        f"FOR e IN {EDGES} FILTER e.orgId == @org REMOVE e IN {EDGES}", {"org": org_id},
    )
    for collection in (NODES, RECORDS):
        await provider.execute_query(
            f"FOR d IN {collection} FILTER d.orgId == @org REMOVE d IN {collection}", {"org": org_id},
        )


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def _mention(surface: str) -> Mention:
    return Mention(block_index=0, char_start=0, char_end=len(surface), surface=surface, extractor="value")


def _quarter(org_id: str) -> ResolvedEntity:
    entity = NamedEntity(
        kind=EntityKind.DATE_RANGE,
        display_name="Q3 FY25",
        norm_key="2025-07-01/2025-10-01",
        value=DateRangeValue(start_ms=_ms(2025, 7, 1), end_ms=_ms(2025, 10, 1), granularity="quarter", timex="2025-Q3"),
        mentions=[_mention("Q3 FY25")],
    )
    return ResolvedEntity(entity=entity, graph_key=named_entity_key(org_id, entity.kind.value, entity.norm_key))


def _amount(org_id: str, amount: str, currency: str = "USD") -> ResolvedEntity:
    value = Decimal(amount)
    entity = NamedEntity(
        kind=EntityKind.CURRENCY,
        display_name=f"{currency} {amount}",
        norm_key=f"{currency}:{amount}",
        value=MoneyValue(amount=value, amount_float=float(value), currency=currency),
        mentions=[_mention(f"{currency} {amount}")],
    )
    return ResolvedEntity(entity=entity, graph_key=named_entity_key(org_id, entity.kind.value, entity.norm_key))


def _org(org_id: str, name: str) -> ResolvedEntity:
    entity = NamedEntity(kind=EntityKind.ORGANIZATION, display_name=name, norm_key=name.casefold(), mentions=[_mention(name)])
    return ResolvedEntity(entity=entity, graph_key=named_entity_key(org_id, entity.kind.value, entity.norm_key))


async def _records(provider: IGraphDBProvider, org_id: str, count: int) -> list[str]:
    ids = [f"{org_id}-rec-{i}" for i in range(count)]
    await provider.batch_upsert_nodes([
        {
            "id": record_id, "orgId": org_id, "recordName": f"Doc {i}",
            "externalRecordId": record_id, "recordType": "FILE", "origin": "CONNECTOR",
            "connectorId": f"{org_id}-conn", "createdAtTimestamp": get_epoch_timestamp_in_ms(),
        }
        for i, record_id in enumerate(ids)
    ], RECORDS)
    return ids


def _writer(provider: IGraphDBProvider) -> NamedEntityGraphWriter:
    return NamedEntityGraphWriter(provider, GraphDataStore(logger, provider), logger)


async def test_schema_creates_named_entity_indexes(graph) -> None:
    provider, _, backend = graph
    if backend == "neo4j":
        rows = await provider.execute_query("SHOW INDEXES YIELD name RETURN name", {})
        names = {row["name"] for row in rows or []}
        assert {"named_entity_org_kind_norm", "named_entity_org_kind_start", "mentions_entity_org"} <= names
        return
    indexes = await provider.http_client.get_indexes(NODES)
    fields = [tuple(index.get("fields", [])) for index in indexes]
    assert ("orgId", "kind", "normKey") in fields
    sparse = {tuple(index["fields"]) for index in indexes if index.get("sparse")}
    assert ("orgId", "kind", "startMs") in sparse


async def test_concurrent_create_if_absent_converges_on_one_node(graph) -> None:
    provider, org_id, _ = graph
    resolved = _org(org_id, "Acme")
    now = get_epoch_timestamp_in_ms()
    await asyncio.gather(*(
        provider.create_named_entities_if_absent([node_document(org_id, resolved, now + i)]) for i in range(6)
    ))
    found = await provider.find_named_entities(org_id, "organization", ["acme"])
    assert len(found) == 1


async def test_aliases_are_partitioned_per_kind(graph) -> None:
    provider, org_id, _ = graph
    org = _org(org_id, "Acme")
    await provider.create_named_entities_if_absent([node_document(org_id, org, get_epoch_timestamp_in_ms())])
    await provider.add_named_entity_aliases(org_id, "organization", org.graph_key, ["ACME Corp"], ["acme corp"])
    assert len(await provider.find_named_entities(org_id, "organization", ["acme corp"])) == 1
    assert await provider.find_named_entities(org_id, "product", ["acme corp"]) == []
    assert await provider.find_named_entities(f"{org_id}-other", "organization", ["acme corp"]) == []


async def test_range_and_overlap_queries_return_the_same_records(graph) -> None:
    provider, org_id, _ = graph
    first, second = await _records(provider, org_id, 2)
    writer = _writer(provider)
    await writer.write(org_id, first, [_quarter(org_id), _amount(org_id, "1250")], status="COMPLETED", strategy="agent")
    await writer.write(org_id, second, [_amount(org_id, "90000"), _amount(org_id, "1250", "EUR")], status="COMPLETED", strategy="agent")

    august = NamedEntityQuery(kinds=["date_range"], date_start_ms=_ms(2025, 8, 1), date_end_ms=_ms(2025, 8, 2))
    hits = await provider.get_records_for_named_entities(org_id, None, august)
    assert {hit["recordId"] for hit in hits["hits"]} == {first}
    assert hits["truncated"] is False

    after = NamedEntityQuery(kinds=["date_range"], date_start_ms=_ms(2025, 10, 1), date_end_ms=_ms(2025, 11, 1))
    assert (await provider.get_records_for_named_entities(org_id, None, after))["hits"] == []

    usd = NamedEntityQuery(kinds=["currency"], amount_min=1000, amount_max=100_000, currency="USD")
    hits = await provider.get_records_for_named_entities(org_id, None, usd)
    assert {hit["recordId"] for hit in hits["hits"]} == {first, second}

    page = await provider.query_named_entities(org_id, NamedEntityQuery(kinds=["currency"], currency="EUR", amount_min=0, amount_max=10_000))
    assert [entity["currency"] for entity in page["entities"]] == ["EUR"]


async def test_name_filter_narrows_and_an_oversized_match_is_truncated(graph) -> None:
    provider, org_id, _ = graph
    first, second = await _records(provider, org_id, 2)
    writer = _writer(provider)
    await writer.write(org_id, first, [_org(org_id, "Acme Robotics")], status="COMPLETED", strategy="agent")
    await writer.write(org_id, second, [_org(org_id, "Globex")], status="COMPLETED", strategy="agent")

    by_name = NamedEntityQuery(kinds=["organization"], name_prefix="acme")
    hits = await provider.get_records_for_named_entities(org_id, None, by_name)
    assert {hit["recordId"] for hit in hits["hits"]} == {first}
    page = await provider.query_named_entities(org_id, NamedEntityQuery(name_prefix="ACME"))
    assert [entity["name"] for entity in page["entities"]] == ["Acme Robotics"]

    everything = NamedEntityQuery(kinds=["organization"])
    capped = await provider.get_records_for_named_entities(org_id, None, everything, limit=1)
    assert capped == {"hits": [], "truncated": True}


async def test_writer_reconciles_and_status_is_independent_of_extraction(graph) -> None:
    provider, org_id, _ = graph
    (record,) = await _records(provider, org_id, 1)
    writer = _writer(provider)
    await writer.write(org_id, record, [_org(org_id, "Acme"), _org(org_id, "Globex")], status="COMPLETED", strategy="agent")
    await writer.write(org_id, record, [_org(org_id, "Acme")], status="PARTIAL", strategy="single_call")
    names = sorted(row["name"] for row in await provider.get_named_entities_for_record(record))
    assert names == ["Acme"]
    doc = await provider.get_document(record, RECORDS)
    assert doc["entityExtractionStatus"] == "PARTIAL"
    assert doc["entityExtractionStrategy"] == "single_call"
    assert "extractionStatus" not in doc or doc["extractionStatus"] != "PARTIAL"


async def test_copy_then_clear_then_orphan_cleanup(graph) -> None:
    provider, org_id, _ = graph
    source, target = await _records(provider, org_id, 2)
    writer = _writer(provider)
    await writer.write(org_id, source, [_org(org_id, "Acme"), _quarter(org_id)], status="COMPLETED", strategy="agent")

    copied = await provider.copy_named_entity_mentions(source, target)
    assert copied == 2
    assert await provider.copy_named_entity_mentions(source, target) == 2
    rows = await provider.get_named_entities_for_record(target)
    assert sorted(row["kind"] for row in rows) == ["date_range", "organization"]
    assert all(row.get("mentionCount") == 1 for row in rows)

    await writer.clear_for_record(source)
    assert await provider.get_named_entities_for_record(source) == []
    assert await provider.delete_orphan_named_entities(org_id) == []

    await writer.clear_for_record(target)
    removed = await provider.delete_orphan_named_entities(org_id)
    assert len(removed) == 2
    assert await provider.find_named_entities(org_id, "organization", ["acme"]) == []
