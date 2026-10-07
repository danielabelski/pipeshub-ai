"""Named-entity stage wiring: never raises, gated by the flag, safe on failure."""

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.entities import EntityType
from app.modules.named_entities import stage
from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.models import (
    Mention,
    NamedEntity,
    NamedEntityExtraction,
)
from app.modules.transformers.pipeline import IndexingPipeline
from app.modules.transformers.sink_orchestrator import SinkOrchestrator

_SECRET_NAME = "Zebulon Quartermaine"


def _record():
    return SimpleNamespace(id="rec-1", org_id="org-1", semantic_metadata=None)


def _extraction(status="COMPLETED") -> NamedEntityExtraction:
    entity = NamedEntity(
        kind=EntityKind.PERSON,
        display_name=_SECRET_NAME,
        norm_key="zebulon quartermaine",
        mentions=[Mention(block_index=0, char_start=0, char_end=20, surface=_SECRET_NAME, extractor="agent")],
    )
    return NamedEntityExtraction(strategy="agent", termination_reason="finish_ok", status=status, entities=[entity])


def _store():
    tx = MagicMock()
    tx.batch_update_nodes = AsyncMock()
    store = MagicMock()

    @asynccontextmanager
    async def transaction():
        yield tx

    store.transaction = transaction
    store.tx = tx
    return store


def _sink(graph=None) -> SinkOrchestrator:
    sink = SinkOrchestrator.__new__(SinkOrchestrator)
    sink.logger = logging.getLogger("test")
    sink.config_service = MagicMock()
    sink.graph_provider = graph or MagicMock()
    sink.graphdb = MagicMock()
    sink.entity_vector_store = None
    return sink


async def test_pipeline_runs_extraction_concurrently_with_classify():
    order: list[str] = []
    released = asyncio.Event()

    async def extract(ctx):
        order.append("ner-start")
        released.set()
        return _extraction()

    async def classify(ctx):
        await asyncio.wait_for(released.wait(), timeout=1)
        order.append("classify-done")
        ctx.record.semantic_metadata = MagicMock(summary="")

    doc_extraction = MagicMock(apply=AsyncMock(side_effect=classify))
    sink = MagicMock()
    sink.extract_named_entities = AsyncMock(side_effect=extract)
    sink.resolve_entities = AsyncMock()
    sink.attach_named_entities = MagicMock(side_effect=lambda *a: order.append("attach"))
    sink.blob_storage.apply = AsyncMock(side_effect=lambda ctx: order.append("blob"))
    sink.enrich = AsyncMock(side_effect=lambda ctx: order.append("enrich"))
    sink.persist_named_entities = AsyncMock(side_effect=lambda *a: order.append("persist"))
    ctx = MagicMock()
    await IndexingPipeline(doc_extraction, sink)._enrich(ctx)
    assert order == ["ner-start", "classify-done", "attach", "blob", "enrich", "persist"]


async def test_pipeline_cancels_extraction_when_classify_fails():
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def extract(ctx):
        started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    async def classify(ctx):
        await started.wait()
        raise RuntimeError("classify down")

    sink = MagicMock()
    sink.extract_named_entities = AsyncMock(side_effect=extract)
    pipeline = IndexingPipeline(MagicMock(apply=AsyncMock(side_effect=classify)), sink)
    with pytest.raises(RuntimeError):
        await pipeline._enrich(MagicMock())
    await asyncio.wait_for(cancelled.wait(), timeout=1)


async def test_extraction_failure_returns_failed_and_does_not_raise(caplog):
    sink = _sink()
    ctx = SimpleNamespace(record=_record())
    with patch(
        "app.services.featureflag.platform_settings.is_named_entity_extraction_enabled",
        AsyncMock(return_value=True),
    ), patch.object(stage, "extract_named_entities", AsyncMock(side_effect=RuntimeError(_SECRET_NAME))):
        result = await sink.extract_named_entities(ctx)
    assert result.status == "FAILED"
    assert _SECRET_NAME not in caplog.text


async def test_flag_off_clears_only_records_that_had_entities():
    store = _store()
    graph = MagicMock(get_named_entities_for_record=AsyncMock(return_value=[]))
    ctx = SimpleNamespace(record=_record())
    await stage.persist_named_entities(ctx, _extraction(), graph_provider=graph, graph_data_store=store, enabled=False)
    store.tx.batch_update_nodes.assert_not_awaited()

    graph.get_named_entities_for_record = AsyncMock(return_value=[{"id": "e1"}])
    with patch("app.modules.named_entities.graph_writer.EdgeReconciler") as reconciler:
        reconciler.return_value.reconcile = AsyncMock()
        await stage.persist_named_entities(ctx, _extraction(), graph_provider=graph, graph_data_store=store, enabled=False)
    assert reconciler.return_value.reconcile.await_args.kwargs["new_tos"] == {}
    update = store.tx.batch_update_nodes.await_args.args[0][0]
    assert update["entityExtractionStatus"] == "SKIPPED"


async def test_failed_extraction_keeps_previous_edges():
    store = _store()
    graph = MagicMock(set_entity_extraction_status=AsyncMock())
    ctx = SimpleNamespace(record=_record())
    with patch("app.modules.named_entities.graph_writer.EdgeReconciler") as reconciler:
        await stage.persist_named_entities(
            ctx, _extraction("FAILED"), graph_provider=graph, graph_data_store=store, enabled=True,
        )
    reconciler.assert_not_called()
    graph.set_entity_extraction_status.assert_awaited_once_with("rec-1", "FAILED", "agent")


async def test_persist_failure_marks_failed_and_logs_counts_only(caplog):
    caplog.set_level(logging.INFO)
    graph = MagicMock(
        find_named_entities=AsyncMock(return_value=[]),
        create_named_entities_if_absent=AsyncMock(side_effect=RuntimeError(f"bind {_SECRET_NAME}")),
        set_entity_extraction_status=AsyncMock(),
    )
    ctx = SimpleNamespace(record=_record())
    await stage.persist_named_entities(ctx, _extraction(), graph_provider=graph, graph_data_store=_store(), enabled=True)
    graph.set_entity_extraction_status.assert_awaited_once_with("rec-1", "FAILED", "agent")
    assert "named_entities record=rec-1 outcome=failed" in caplog.text
    assert _SECRET_NAME not in caplog.text


async def test_duplicate_sync_projects_named_entities_but_not_pii_or_foreign_rows():
    graph = MagicMock(
        get_taxonomy_entities_for_record=AsyncMock(return_value=[]),
        get_named_entities_for_record=AsyncMock(return_value=[
            {"id": "n1", "kind": "organization", "name": "Acme", "orgId": "org-1"},
            {"id": "n2", "kind": "email", "name": "a@b.co", "orgId": "org-1"},
            {"id": "n3", "kind": "organization", "name": "Other", "orgId": "org-2"},
        ]),
        get_record_group_by_id=AsyncMock(return_value=None),
    )
    sink = _sink(graph)
    sink.entity_vector_store = MagicMock(
        upsert_entities_batch=AsyncMock(),
        replace_entities_batch=AsyncMock(),
        upsert_entities_membership=AsyncMock(),
    )
    await sink.sync_entities_for_duplicate({"_key": "dup-1", "orgId": "org-1"})
    projected = [
        entity
        for call in sink.entity_vector_store.mock_calls
        for arg in call.args
        if isinstance(arg, list)
        for entity in arg
        if getattr(entity, "entity_type", None) is EntityType.NAMED_ENTITY
    ]
    assert [(entity.entity_id, entity.kind) for entity in projected] == [("n1", "organization")]
