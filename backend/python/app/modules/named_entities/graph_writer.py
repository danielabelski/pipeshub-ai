"""Create named-entity nodes outside the record transaction, then reconcile edges inside it."""

from __future__ import annotations

import logging
from typing import Any

from app.config.constants.arangodb import CollectionNames
from app.models.entities import EntityRecord, EntityType, EntityTypeCategory
from app.modules.named_entities.domain.kinds import spec_for
from app.modules.named_entities.domain.models import EXTRACTOR_VERSION, NamedEntity
from app.modules.named_entities.domain.values import (
    DateRangeValue,
    DurationValue,
    MoneyValue,
    PercentValue,
    QuantityValue,
)
from app.modules.named_entities.resolution import ResolvedEntity
from app.modules.transformers.edge_reconciler import EdgeReconciler
from app.utils.time_conversion import get_epoch_timestamp_in_ms

logger = logging.getLogger(__name__)

_EDGE = CollectionNames.MENTIONS_ENTITY.value
_NODES = CollectionNames.NAMED_ENTITIES.value


def _value_fields(entity: NamedEntity) -> dict[str, Any]:
    value = entity.value
    if isinstance(value, DateRangeValue) and not value.ambiguous and value.start_ms is not None:
        return {
            "startMs": value.start_ms,
            "endMs": value.end_ms,
            "granularity": value.granularity,
            "timex": value.timex,
        }
    if isinstance(value, MoneyValue):
        return {
            "amount": str(value.amount),
            "amountFloat": value.amount_float,
            "currency": value.currency,
            "numericValue": value.amount_float,
        }
    if isinstance(value, QuantityValue):
        return {
            "numericValue": float(value.value),
            "unit": value.unit,
            "dimension": value.dimension,
            "siValue": value.si_value,
        }
    if isinstance(value, PercentValue):
        return {"numericValue": float(value.value)}
    if isinstance(value, DurationValue):
        return {"durationIso": value.iso, "durationSeconds": value.seconds}
    return {}


def node_document(org_id: str, resolved: ResolvedEntity, now: int) -> dict[str, Any]:
    entity = resolved.entity
    spec = spec_for(entity.kind)
    doc = {
        "id": resolved.graph_key,
        "orgId": org_id,
        "kind": entity.kind.value,
        "category": spec.category.value if spec else "",
        "tags": list(entity.tags)[:8],
        "name": entity.display_name[:256],
        "normKey": entity.norm_key,
        "aliases": [entity.display_name[:256]],
        "normalizedAliases": [entity.norm_key],
        "schemaVersion": 1,
        "createdAtTimestamp": now,
        "updatedAtTimestamp": now,
    }
    doc.update(_value_fields(entity))
    return doc


def edge_payload(org_id: str, entity: NamedEntity, now: int) -> dict[str, Any]:
    mentions = entity.mentions
    names = []
    for mention in mentions:
        if mention.surface not in names:
            names.append(mention.surface[:256])
        if len(names) >= 10:
            break
    blocks = []
    for mention in mentions:
        if mention.block_index not in blocks:
            blocks.append(mention.block_index)
        if len(blocks) >= 50:
            break
    extractors = sorted({mention.extractor for mention in mentions})
    evidence = max((mention.evidence_score for mention in mentions), default=0.0)
    return {
        "orgId": org_id,
        "updatedAtTimestamp": now,
        "mentionCount": len(mentions),
        "maxEvidence": evidence,
        "extractors": extractors,
        "extractedNames": names,
        "blockIndexes": blocks,
        "extractorVersion": EXTRACTOR_VERSION,
    }


class NamedEntityGraphWriter:
    def __init__(self, graph_provider, graph_data_store, logger_=None) -> None:
        self._graph = graph_provider
        self._store = graph_data_store
        self._logger = logger_ or logger

    async def write(
        self, org_id: str, record_id: str, resolved: list[ResolvedEntity], *, status: str, strategy: str
    ) -> list[EntityRecord]:
        now = get_epoch_timestamp_in_ms()
        docs = [node_document(org_id, item, now) for item in resolved]
        if docs:
            await self._graph.create_named_entities_if_absent(docs)
            for item, doc in zip(resolved, docs, strict=True):
                if item.merged_into:
                    await self._graph.add_named_entity_aliases(
                        org_id,
                        item.entity.kind.value,
                        item.graph_key,
                        [item.entity.display_name],
                        [item.entity.norm_key],
                    )
                del doc
        await self._reconcile(org_id, record_id, resolved, status, strategy, now)
        return _embeddable(org_id, resolved)

    async def clear_for_record(self, record_id: str, *, strategy: str = "") -> None:
        await self._reconcile("", record_id, [], "SKIPPED", strategy, get_epoch_timestamp_in_ms())

    async def _reconcile(
        self, org_id: str, record_id: str, resolved: list[ResolvedEntity], status: str, strategy: str, now: int
    ) -> None:
        new_tos = {f"{_NODES}/{item.graph_key}": item.entity.display_name for item in resolved}
        properties = {
            f"{_NODES}/{item.graph_key}": edge_payload(org_id, item.entity, now) for item in resolved
        }
        record_from = f"{CollectionNames.RECORDS.value}/{record_id}"
        async with self._store.transaction() as tx:
            await EdgeReconciler(self._logger).reconcile(
                tx,
                record_id=record_id,
                record_from=record_from,
                edge_collection=_EDGE,
                new_tos=new_tos,
                label="named entity",
                edge_properties=properties,
                update_existing=True,
            )
            await tx.batch_update_nodes(
                [{
                    "id": record_id,
                    "entityExtractionStatus": status,
                    "lastEntityExtractionTimestamp": now,
                    "entityExtractionStrategy": strategy,
                }],
                CollectionNames.RECORDS.value,
            )


def _embeddable(org_id: str, resolved: list[ResolvedEntity]) -> list[EntityRecord]:
    records = []
    for item in resolved:
        spec = spec_for(item.entity.kind)
        if spec is None or not spec.embeddable or spec.is_pii:
            continue
        records.append(
            EntityRecord(
                entity_id=item.graph_key,
                entity_type=EntityType.NAMED_ENTITY,
                name=item.entity.display_name,
                org_id=org_id,
                kind=item.entity.kind.value,
                type_category=EntityTypeCategory.PREDEFINED,
            )
        )
    return records
