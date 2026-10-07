import logging

import pytest

from app.modules.entity_resolution.models import ResolutionMode
from app.modules.named_entities.domain.config import NamedEntityBudgets
from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.models import NamedEntity
from app.modules.named_entities.graph_ops import (
    QUERY_AQL,
    NamedEntityGraph,
    NamedEntityQuery,
)
from app.modules.named_entities.graph_writer import NamedEntityGraphWriter
from app.modules.named_entities.keys import named_entity_key
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.resolution import NamedEntityResolver, ResolvedEntity
from app.modules.named_entities.strategies.base import (
    ExtractionContext,
    ExtractionDocument,
)
from app.modules.named_entities.strategies.selector import ExtractionStrategySelector
from app.modules.retrieval.entity_filters import (
    EntityFilterResolver,
    EntityFilterUnavailableError,
)


def _org(name: str) -> NamedEntity:
    return NamedEntity(kind=EntityKind.ORGANIZATION, display_name=name, norm_key=f"name:organization:{name.casefold()}")


def test_keys_include_org_and_kind():
    left = named_entity_key("org-a", "organization", "name:organization:acme")
    right = named_entity_key("org-b", "organization", "name:organization:acme")
    other = named_entity_key("org-a", "product", "name:organization:acme")
    assert left != right
    assert left != other


class _Graph:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    async def find_named_entities(self, org_id, kind, keys):
        self.calls.append((org_id, kind, list(keys)))
        return [row for row in self.rows if row.get("kind", kind) == kind and row.get("orgId", org_id) == org_id]


async def test_shadow_records_a_match_without_changing_the_key():
    existing = "existing-key"
    graph = _Graph([{"normKey": "name:organization:acme", "_key": existing, "orgId": "org", "kind": "organization"}])
    resolved = await NamedEntityResolver(graph, mode=ResolutionMode.SHADOW).resolve("org", [_org("Acme")])
    assert resolved[0].graph_key == named_entity_key("org", "organization", "name:organization:acme")
    assert resolved[0].shadow_target == existing
    assert resolved[0].graph_key != existing


async def test_invalid_candidate_id_stays_new(monkeypatch):
    async def _select(self, ambiguous):
        return {"Acme": "forged-id"}

    monkeypatch.setattr(NamedEntityResolver, "_select", _select)

    async def search(org_id, kind, name):
        return [{"id": "real-id", "name": "Completely Different", "orgId": org_id, "kind": kind}]

    resolved = await NamedEntityResolver(
        _Graph([]), vector_search=search, llm=object(), mode=ResolutionMode.APPLY,
    ).resolve("org", [_org("Acme")])
    own = named_entity_key("org", "organization", "name:organization:acme")
    assert resolved[0].graph_key == own


async def test_value_kinds_do_not_ask_the_graph():
    graph = _Graph([])
    entity = NamedEntity(kind=EntityKind.CURRENCY, display_name="$1", norm_key="money:USD:1")
    resolved = await NamedEntityResolver(graph).resolve("org", [entity])
    assert resolved[0].graph_key == named_entity_key("org", "currency", "money:USD:1")
    assert graph.calls == []


class _Records:
    def __init__(self, payload):
        self.payload = payload

    async def get_records_for_named_entities(self, org_id, entity_ids, query, limit=5000):
        self.args = (entity_ids, query)
        return self.payload


async def test_entity_ids_and_a_typed_range_are_both_applied():
    graph = _Records({"hits": [], "truncated": False})
    await EntityFilterResolver(graph).match(
        "org", {"entityIds": ["e1"], "amount": {"min": 10, "max": 20, "currency": "USD"}},
    )
    ids, query = graph.args
    assert ids == ["e1"]
    assert (query.amount_min, query.amount_max, query.currency) == (10, 20, "USD")


async def test_records_for_narrows_the_typed_match_to_the_given_ids():
    calls: list[tuple[str, dict]] = []

    async def execute(statement, binds, **_):
        calls.append((statement, dict(binds)))
        return ["e1"] if "RETURN n._key" in statement else []

    graph = NamedEntityGraph(execute, "arango")
    await graph.records_for("org", ["e1", "e2"], NamedEntityQuery(kinds=["currency"]), limit=10)

    (matched_sql, matched_binds), (_, record_binds) = calls
    assert "n._key IN @ids" in matched_sql and matched_binds["ids"] == ["e1", "e2"]
    assert (record_binds["useIds"], record_binds["matched"]) == (False, ["e1"])


async def test_entity_filter_returns_the_mentioning_records_and_grants_nothing():
    graph = _Records({"hits": [
        {"recordId": "r1", "virtualRecordId": "v1", "connectorId": "app-1"},
        {"recordId": "r2", "virtualRecordId": "v1", "connectorId": "app-2"},
        {"recordId": "r3", "virtualRecordId": "v3", "connectorId": "app-1"},
    ], "truncated": False})
    match = await EntityFilterResolver(graph).match("org", {"entityIds": ["e1"]})
    assert match.error is None
    assert [hit.record_id for hit in match.hits] == ["r1", "r2", "r3"]
    assert match.virtual_record_ids == ["v1", "v3"]


async def test_truncated_entity_filter_is_an_error():
    graph = _Records({"hits": [], "truncated": True})
    match = await EntityFilterResolver(graph).match("org", {"entityIds": ["e1"]})
    assert match.error == "filter too broad, narrow it"
    assert match.hits == ()


async def test_an_entity_filter_without_a_constraint_narrows_nothing():
    graph = _Records({"hits": [], "truncated": False})
    match = await EntityFilterResolver(graph).match("org", {"kinds": []})
    assert not match.constrained


async def test_a_failed_entity_lookup_is_not_an_empty_match():
    class _Down:
        async def get_records_for_named_entities(self, *args, **kwargs):
            raise RuntimeError("graph down")

    with pytest.raises(EntityFilterUnavailableError):
        await EntityFilterResolver(_Down()).match("org", {"entityIds": ["e1"]})


def test_selector_failure_ladder():
    selector = ExtractionStrategySelector()
    doc = ExtractionDocument(units=[])
    ctx = ExtractionContext(
        org_id="org",
        norm=NormalizationContext(),
        enabled=frozenset({EntityKind.EMAIL}),
        budgets=NamedEntityBudgets(),
    )
    assert selector.choose(doc, ctx) == "single_call"
    from app.modules.named_entities.text import TextUnit

    doc = ExtractionDocument(units=[TextUnit(0, "b", "a@b.co")])
    assert selector.choose(doc, ctx) == "deterministic"
    ctx.enabled = frozenset({EntityKind.ORGANIZATION})
    ctx.under_pressure = True
    assert selector.choose(doc, ctx) == "single_call"
    ctx.under_pressure = False
    ctx.supports_tools = False
    assert selector.choose(doc, ctx) == "single_call"
    ctx.supports_tools = True
    assert selector.choose(doc, ctx) == "agent"


async def test_query_binds_are_parameters():
    seen = {}

    async def execute(query, binds):
        seen["query"] = query
        seen["binds"] = binds
        return []

    await NamedEntityGraph(execute, "arango").query(
        "org-1",
        NamedEntityQuery(kinds=["date"], date_start_ms=1, date_end_ms=2, name_prefix="Q"),
    )
    assert seen["query"] == QUERY_AQL
    assert seen["binds"]["orgId"] == "org-1"
    assert seen["binds"]["kinds"] == ["date"]
    assert seen["binds"]["dateStart"] == 1
    assert "org-1" not in seen["query"]


class _Tx:
    def __init__(self, existing):
        self.existing = existing
        self.created = []
        self.deleted = []
        self.updates = []

    async def get_edges_from_node_with_target_name(self, record_from, edge_collection, raise_on_error=True):
        return self.existing

    async def batch_create_edges(self, edges, collection):
        self.created.extend(edges)

    async def batch_delete_edges(self, edges, collection):
        self.deleted.extend(edges)
        return len(edges)

    async def batch_update_nodes(self, docs, collection):
        self.updates.append(docs)


class _Store:
    def __init__(self, tx):
        self.tx = tx

    def transaction(self):
        return self

    async def __aenter__(self):
        return self.tx

    async def __aexit__(self, *args):
        return False


class _Nodes:
    def __init__(self):
        self.created = []

    async def create_named_entities_if_absent(self, docs):
        self.created.extend(docs)

    async def add_named_entity_aliases(self, *args):
        return None


async def test_writer_updates_entity_status_only():
    tx = _Tx([{"_to": "namedEntities/old", "name": "Old", "createdAtTimestamp": 1}])
    graph = _Nodes()
    writer = NamedEntityGraphWriter(graph, _Store(tx), logging.getLogger("test"))
    entity = _org("Acme")
    resolved = ResolvedEntity(entity=entity, graph_key="new-key")
    embedded = await writer.write("org", "rec", [resolved], status="COMPLETED", strategy="agent")
    assert embedded and embedded[0].kind == "organization"
    status = tx.updates[0][0]
    assert status["entityExtractionStatus"] == "COMPLETED"
    assert "extractionStatus" not in status
    assert tx.deleted

    tx.updates.clear()
    await writer.clear_for_record("rec")
    assert tx.updates[-1][0]["entityExtractionStatus"] == "SKIPPED"
