"""Indexing stage. Never raises: a failure here must not make the record unsearchable."""

from __future__ import annotations

import logging
import time

from app.modules.entity_resolution.models import ResolutionMode
from app.modules.named_entities.domain.config import load_named_entity_config
from app.modules.named_entities.domain.models import NamedEntityExtraction
from app.modules.named_entities.extractor import (
    NamedEntityExtractor,
    NamedEntityRequest,
    indexing_llm,
)
from app.modules.named_entities.graph_writer import NamedEntityGraphWriter
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.resolution import NamedEntityResolver
from app.telemetry.modules.named_entity_metrics import observe_stage, record_run

logger = logging.getLogger(__name__)


def _reference_ms(record) -> int | None:
    return getattr(record, "source_updated_at", None) or getattr(record, "source_created_at", None)


async def extract_named_entities(ctx, *, config_service, client=None, enabled: bool) -> NamedEntityExtraction:
    record = ctx.record
    if not enabled:
        return NamedEntityExtraction(status="SKIPPED", termination_reason="empty", strategy="deterministic")
    blocks = getattr(getattr(record, "block_containers", None), "blocks", None) or []
    if not blocks:
        return NamedEntityExtraction(status="SKIPPED", termination_reason="empty", strategy="deterministic")
    config = await load_named_entity_config(config_service)
    if client is not None:
        return await client.extract_entities(
            block_container=record.block_containers,
            org_id=record.org_id,
            record_name=record.record_name,
            record_type=getattr(record.record_type, "value", str(record.record_type)),
            reference_time_ms=_reference_ms(record),
            tz=config.tz,
            enabled_kinds=[kind.value for kind in config.enabled()],
            budgets=config.budgets.model_dump(),
        )
    llm = transport = None
    provider = model = "indexing"
    try:
        llm, transport, provider, model = await indexing_llm(config_service)
    except Exception:
        logger.warning("Named-entity model unavailable; keeping deterministic spans")
    request = NamedEntityRequest(
        blocks=blocks,
        org_id=record.org_id,
        record_name=record.record_name or "",
        record_type=getattr(record.record_type, "value", "") or "",
        reference_time_ms=_reference_ms(record),
        tz=config.tz,
        enabled=config.enabled(),
        budgets=config.budgets,
        llm=llm,
        transport=transport,
        provider_name=provider,
        model_name=model,
    )
    return await NamedEntityExtractor().extract(request)


def attach_named_entities(record, extraction: NamedEntityExtraction) -> None:
    metadata = getattr(record, "semantic_metadata", None)
    if metadata is None:
        return
    metadata.named_entities = extraction.model_dump(mode="json")


async def persist_named_entities(
    ctx,
    extraction: NamedEntityExtraction,
    *,
    graph_provider,
    graph_data_store,
    entity_vector_store=None,
    enabled: bool,
    llm=None,
) -> None:
    record = ctx.record
    writer = NamedEntityGraphWriter(graph_provider, graph_data_store, logger)
    if not enabled:
        # The flag is off by default; skip the write for records that never had entities.
        if await graph_provider.get_named_entities_for_record(record.id):
            await writer.clear_for_record(record.id)
        return
    started = time.perf_counter()
    outcome = "written"
    try:
        if extraction.status == "FAILED":
            # A transient failure must not erase the entities from the last good run.
            outcome = "failed"
            await graph_provider.set_entity_extraction_status(record.id, "FAILED", extraction.strategy)
            return
        resolver = NamedEntityResolver(graph_provider, llm=llm, mode=ResolutionMode.SHADOW)
        resolved = await resolver.resolve(record.org_id, extraction.entities)
        embeddable = await writer.write(
            record.org_id,
            record.id,
            resolved,
            status=extraction.status,
            strategy=extraction.strategy,
        )
        if entity_vector_store and embeddable:
            await entity_vector_store.upsert_entities_batch(embeddable)
    except Exception as exc:
        outcome = "failed"
        logger.warning(
            "named_entities persist failed record=%s entities=%d error=%s",
            getattr(record, "id", ""),
            len(extraction.entities),
            type(exc).__name__,
        )
        try:
            await graph_provider.set_entity_extraction_status(record.id, "FAILED", extraction.strategy)
        except Exception as status_exc:
            logger.warning("named_entities status write failed error=%s", type(status_exc).__name__)
    finally:
        elapsed = time.perf_counter() - started
        observe_stage(extraction.strategy, elapsed)
        record_run(extraction, outcome=outcome)
        log_run(record, extraction, outcome=outcome, seconds=elapsed)


def log_run(record, extraction: NamedEntityExtraction, *, outcome: str, seconds: float) -> None:
    """One line per record. Counts only: no surfaces, names or values."""
    by_kind: dict[str, int] = {}
    for entity in extraction.entities:
        by_kind[entity.kind.value] = by_kind.get(entity.kind.value, 0) + 1
    stats = extraction.stats
    logger.info(
        "named_entities record=%s outcome=%s status=%s strategy=%s reason=%s entities=%d "
        "kinds=%s deterministic=%d accepted=%d rejected=%d dropped=%d turns=%d tool_calls=%d seconds=%.2f",
        getattr(record, "id", ""),
        outcome,
        extraction.status,
        extraction.strategy,
        extraction.termination_reason,
        len(extraction.entities),
        ",".join(f"{kind}:{n}" for kind, n in sorted(by_kind.items())),
        stats.deterministic,
        stats.accepted,
        stats.rejected,
        stats.dropped_cap,
        stats.turns,
        stats.tool_calls,
        seconds,
    )


def normalization_context(record, tz: str) -> NormalizationContext:
    return NormalizationContext(reference_time_ms=_reference_ms(record), tz=tz or "UTC")
