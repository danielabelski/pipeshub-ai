"""AQL and Cypher for named entities. Providers execute these; feature code does not."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.config.constants.arangodb import CollectionNames
from app.config.constants.neo4j import (
    EDGE_COLLECTION_TO_RELATIONSHIP,
    Neo4jLabel,
    Neo4jRelationshipType,
)
from app.modules.named_entities.keys import alias_partition

_COLLECTION = CollectionNames.NAMED_ENTITIES.value
_EDGE = CollectionNames.MENTIONS_ENTITY.value
_RECORDS = CollectionNames.RECORDS.value
_LABEL = Neo4jLabel.NAMED_ENTITIES.value
_REL = Neo4jRelationshipType.MENTIONS_ENTITY.value
_RECORD_LABEL = "Record"
_CAP = 5000
_ALIAS_CAP = 20

# Existing nodes are left untouched: writing a popular node from every record
# that mentions it would serialize those records on its lock.
CREATE_AQL = f"""
FOR doc IN @docs
  INSERT doc INTO {_COLLECTION} OPTIONS {{ overwriteMode: "ignore" }}
"""

CREATE_CYPHER = f"""
UNWIND $docs AS doc
MERGE (n:{_LABEL} {{id: doc.id}})
ON CREATE SET n += doc
"""

FIND_AQL = f"""
FOR n IN {_COLLECTION}
  FILTER n.orgId == @orgId AND n.kind == @kind
  FILTER n.normKey IN @keys OR LENGTH(INTERSECTION(NOT_NULL(n.normalizedAliases, []), @keys)) > 0
  RETURN n
"""

FIND_CYPHER = f"""
MATCH (n:{_LABEL} {{orgId: $orgId, kind: $kind}})
WHERE n.normKey IN $keys
RETURN n
UNION
MATCH (a:TaxonomyAlias {{orgId: $orgId, collection: $partition}})
WHERE a.normalized IN $keys
MATCH (a)-[:ALIAS_OF]->(n:{_LABEL})
WHERE n.orgId = $orgId AND n.kind = $kind
RETURN n
"""

ALIAS_AQL = f"""
FOR n IN {_COLLECTION}
  FILTER n._key == @key AND n.orgId == @orgId AND n.kind == @kind
  FILTER LENGTH(MINUS(@normalized, NOT_NULL(n.normalizedAliases, []))) > 0
  UPDATE n WITH {{
    aliases: SLICE(UNION_DISTINCT(NOT_NULL(n.aliases, []), @aliases), 0, @maxAliases),
    normalizedAliases: SLICE(UNION_DISTINCT(NOT_NULL(n.normalizedAliases, []), @normalized), 0, @maxAliases),
    updatedAtTimestamp: @now
  }} IN {_COLLECTION}
"""

ALIAS_CYPHER = f"""
MATCH (n:{_LABEL} {{id: $key}})
WHERE n.orgId = $orgId AND n.kind = $kind
SET n._aliasLock = true
WITH n, coalesce(n.aliases, []) AS displays, coalesce(n.normalizedAliases, []) AS normals
WITH n, displays, normals,
    [i IN range(0, size($normalized) - 1) WHERE NOT $normalized[i] IN normals] AS fresh
SET n.aliases = (displays + [i IN fresh | $aliases[i]])[0..$maxAliases],
    n.normalizedAliases = (normals + [i IN fresh | $normalized[i]])[0..$maxAliases],
    n.updatedAtTimestamp = $now
REMOVE n._aliasLock
WITH n
UNWIND [normalized IN $normalized WHERE normalized IN n.normalizedAliases] AS normalized
MERGE (a:TaxonomyAlias {{orgId: n.orgId, collection: $partition, normalized: normalized}})
MERGE (a)-[:ALIAS_OF]->(n)
RETURN count(a) AS aliases
"""

QUERY_AQL = f"""
FOR n IN {_COLLECTION}
  FILTER n.orgId == @orgId
  FILTER LENGTH(@kinds) == 0 OR n.kind IN @kinds
  FILTER @prefix == "" OR STARTS_WITH(LOWER(n.name), @prefix)
  FILTER @dateStart == null OR (n.startMs < @dateEnd AND n.endMs > @dateStart)
  FILTER @amountMin == null OR (n.currency == @currency AND n.amountFloat >= @amountMin AND n.amountFloat <= @amountMax)
  FILTER @quantityMin == null OR (n.dimension == @dimension AND n.siValue >= @quantityMin AND n.siValue <= @quantityMax)
  FILTER @percentMin == null OR (n.kind == "percentage" AND n.numericValue >= @percentMin AND n.numericValue <= @percentMax)
  SORT n._key
  LIMIT @skip, @limit
  RETURN n
"""

QUERY_CYPHER = f"""
MATCH (n:{_LABEL} {{orgId: $orgId}})
WHERE (size($kinds) = 0 OR n.kind IN $kinds)
  AND ($prefix = "" OR toLower(n.name) STARTS WITH $prefix)
  AND ($dateStart IS NULL OR (n.startMs < $dateEnd AND n.endMs > $dateStart))
  AND ($amountMin IS NULL OR (n.currency = $currency AND n.amountFloat >= $amountMin AND n.amountFloat <= $amountMax))
  AND ($quantityMin IS NULL OR (n.dimension = $dimension AND n.siValue >= $quantityMin AND n.siValue <= $quantityMax))
  AND ($percentMin IS NULL OR (n.kind = "percentage" AND n.numericValue >= $percentMin AND n.numericValue <= $percentMax))
RETURN n
ORDER BY n.id
SKIP $skip LIMIT $limit
"""

RECORDS_AQL = f"""
FOR e IN {_EDGE}
  FILTER e.orgId == @orgId
  FILTER (@useIds AND PARSE_IDENTIFIER(e._to).key IN @ids) OR (NOT @useIds AND PARSE_IDENTIFIER(e._to).key IN @matched)
  LET r = DOCUMENT(e._from)
  FILTER r != null AND r.isDeleted != true
  LIMIT @limit
  RETURN {{
    recordId: PARSE_IDENTIFIER(e._from).key,
    entityId: PARSE_IDENTIFIER(e._to).key,
    blockIndexes: e.blockIndexes
  }}
"""

RECORDS_CYPHER = f"""
MATCH (r:{_RECORD_LABEL})-[e:{_REL}]->(n:{_LABEL})
WHERE e.orgId = $orgId AND (($useIds AND n.id IN $ids) OR (NOT $useIds AND n.id IN $matched))
  AND coalesce(r.isDeleted, false) = false
RETURN r.id AS recordId, n.id AS entityId, e.blockIndexes AS blockIndexes
LIMIT $limit
"""

RECORD_ENTITIES_AQL = f"""
FOR e IN {_EDGE}
  FILTER e._from == CONCAT("{_RECORDS}/", @recordId)
  FOR n IN {_COLLECTION}
    FILTER n._id == e._to
    RETURN MERGE(n, {{ mentionCount: e.mentionCount, blockIndexes: e.blockIndexes }})
"""

RECORD_ENTITIES_CYPHER = f"""
MATCH (r:{_RECORD_LABEL} {{id: $recordId}})-[e:{_REL}]->(n:{_LABEL})
RETURN n {{.*, mentionCount: e.mentionCount, blockIndexes: e.blockIndexes}} AS entity
"""

COPY_AQL = f"""
FOR e IN {_EDGE}
  FILTER e._from == CONCAT("{_RECORDS}/", @source)
  LET target = CONCAT("{_RECORDS}/", @target)
  UPSERT {{ _from: target, _to: e._to }}
  INSERT MERGE(UNSET(e, "_key", "_id", "_rev", "_from"), {{ _from: target, _to: e._to }})
  UPDATE UNSET(e, "_key", "_id", "_rev", "_from", "_to")
  IN {_EDGE}
  RETURN 1
"""

COPY_CYPHER = f"""
MATCH (src:{_RECORD_LABEL} {{id: $source}})-[e:{_REL}]->(n:{_LABEL})
MATCH (dst:{_RECORD_LABEL} {{id: $target}})
MERGE (dst)-[c:{_REL}]->(n)
ON CREATE SET c = properties(e)
ON MATCH SET c += properties(e)
RETURN count(c) AS copied
"""

ORPHAN_AQL = f"""
FOR n IN {_COLLECTION}
  FILTER n.orgId == @orgId
  FILTER LENGTH(FOR e IN {_EDGE} FILTER e._to == n._id LIMIT 1 RETURN 1) == 0
  LIMIT @batch
  REMOVE n IN {_COLLECTION}
  RETURN OLD._key
"""

ORPHAN_CYPHER = f"""
MATCH (n:{_LABEL} {{orgId: $orgId}})
WHERE NOT (n)<-[:{_REL}]-()
WITH n LIMIT $batch
WITH n, n.id AS id
DETACH DELETE n
RETURN id
"""

STATUS_AQL = f"""
UPDATE @key WITH {{
  entityExtractionStatus: @status,
  lastEntityExtractionTimestamp: @ts,
  entityExtractionStrategy: @strategy
}} IN {_RECORDS}
"""

STATUS_CYPHER = f"""
MATCH (r:{_RECORD_LABEL} {{id: $key}})
SET r.entityExtractionStatus = $status,
    r.lastEntityExtractionTimestamp = $ts,
    r.entityExtractionStrategy = $strategy
RETURN r.id AS id
"""

MATCHED_KEYS_AQL = f"""
FOR n IN {_COLLECTION}
  FILTER n.orgId == @orgId
  FILTER LENGTH(@kinds) == 0 OR n.kind IN @kinds
  FILTER @prefix == "" OR STARTS_WITH(LOWER(n.name), @prefix)
  FILTER @dateStart == null OR (n.startMs < @dateEnd AND n.endMs > @dateStart)
  FILTER @amountMin == null OR (n.currency == @currency AND n.amountFloat >= @amountMin AND n.amountFloat <= @amountMax)
  FILTER @quantityMin == null OR (n.dimension == @dimension AND n.siValue >= @quantityMin AND n.siValue <= @quantityMax)
  FILTER @percentMin == null OR (n.kind == "percentage" AND n.numericValue >= @percentMin AND n.numericValue <= @percentMax)
  LIMIT @limit
  RETURN n._key
"""

MATCHED_KEYS_CYPHER = f"""
MATCH (n:{_LABEL} {{orgId: $orgId}})
WHERE (size($kinds) = 0 OR n.kind IN $kinds)
  AND ($prefix = "" OR toLower(n.name) STARTS WITH $prefix)
  AND ($dateStart IS NULL OR (n.startMs < $dateEnd AND n.endMs > $dateStart))
  AND ($amountMin IS NULL OR (n.currency = $currency AND n.amountFloat >= $amountMin AND n.amountFloat <= $amountMax))
  AND ($quantityMin IS NULL OR (n.dimension = $dimension AND n.siValue >= $quantityMin AND n.siValue <= $quantityMax))
  AND ($percentMin IS NULL OR (n.kind = "percentage" AND n.numericValue >= $percentMin AND n.numericValue <= $percentMax))
RETURN n.id AS id
LIMIT $limit
"""


def named_entity_neo4j_indexes() -> list[str]:
    label = _LABEL
    rel = EDGE_COLLECTION_TO_RELATIONSHIP[CollectionNames.MENTIONS_ENTITY.value]
    return [
        f"CREATE INDEX named_entity_org_kind_norm IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.kind, n.normKey)",
        f"CREATE INDEX named_entity_org_kind_start IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.kind, n.startMs)",
        f"CREATE INDEX named_entity_org_kind_end IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.kind, n.endMs)",
        f"CREATE INDEX named_entity_org_currency_amount IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.currency, n.amountFloat)",
        f"CREATE INDEX named_entity_org_dimension_si IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.dimension, n.siValue)",
        f"CREATE INDEX named_entity_org_kind_numeric IF NOT EXISTS FOR (n:{label}) ON (n.orgId, n.kind, n.numericValue)",
        f"CREATE INDEX mentions_entity_org IF NOT EXISTS FOR ()-[r:{rel}]-() ON (r.orgId)",
    ]


async def ensure_named_entity_indexes(http_client) -> None:
    col = _COLLECTION
    await http_client.ensure_persistent_index(col, ["orgId", "kind", "normKey"])
    await http_client.ensure_persistent_index(col, ["orgId", "kind", "normalizedAliases[*]"])
    await http_client.ensure_persistent_index(col, ["orgId", "kind", "startMs"], sparse=True)
    await http_client.ensure_persistent_index(col, ["orgId", "kind", "endMs"], sparse=True)
    await http_client.ensure_persistent_index(col, ["orgId", "currency", "amountFloat"], sparse=True)
    await http_client.ensure_persistent_index(col, ["orgId", "dimension", "siValue"], sparse=True)
    await http_client.ensure_persistent_index(col, ["orgId", "kind", "numericValue"], sparse=True)


def _bind(query: NamedEntityQueryLike, *, skip: int, limit: int) -> dict[str, Any]:
    return {
        "kinds": list(query.kinds or []),
        "prefix": (query.name_prefix or "").strip().lower(),
        "dateStart": query.date_start_ms,
        "dateEnd": query.date_end_ms,
        "amountMin": query.amount_min,
        "amountMax": query.amount_max,
        "currency": query.currency or "",
        "quantityMin": query.quantity_min,
        "quantityMax": query.quantity_max,
        "dimension": query.dimension or "",
        "percentMin": query.percent_min,
        "percentMax": query.percent_max,
        "skip": skip,
        "limit": limit,
    }


@dataclass
class NamedEntityQuery:
    kinds: list[str] | None = None
    name_prefix: str = ""
    date_start_ms: int | None = None
    date_end_ms: int | None = None
    amount_min: float | None = None
    amount_max: float | None = None
    currency: str = ""
    quantity_min: float | None = None
    quantity_max: float | None = None
    dimension: str = ""
    percent_min: float | None = None
    percent_max: float | None = None
    limit: int = 50
    cursor: str = ""


NamedEntityQueryLike = NamedEntityQuery


def arango_node(doc: dict[str, Any]) -> dict[str, Any]:
    stored = dict(doc)
    stored["_key"] = doc["id"]
    stored.pop("id", None)
    return stored


def neo4j_node(doc: dict[str, Any]) -> dict[str, Any]:
    return dict(doc)


async def _no_retry(write):
    return await write()


_AQL_BIND = re.compile(r"@(\w+)")


class NamedEntityGraph:
    def __init__(self, execute, dialect: str, retry=None) -> None:
        self._raw_execute = execute
        self._dialect = dialect
        # ArangoDB fails concurrent writes to one key with errorNum 1200 instead of waiting.
        self._retry = retry or _no_retry

    async def _execute(self, statement: str, binds: dict[str, Any]):
        if self._dialect == "arango":
            # AQL rejects a bind variable the statement does not declare (errorNum 1552).
            declared = set(_AQL_BIND.findall(statement))
            binds = {name: value for name, value in binds.items() if name in declared}
        return await self._raw_execute(statement, binds)

    async def _write(self, statement: str, binds: dict[str, Any]):
        return await self._retry(lambda: self._execute(statement, binds))

    async def create_if_absent(self, docs: list[dict[str, Any]]) -> None:
        if not docs:
            return
        if self._dialect == "arango":
            payload = [arango_node(doc) for doc in docs]
            await self._write(CREATE_AQL, {"docs": payload})
            return
        await self._write(CREATE_CYPHER, {"docs": [neo4j_node(doc) for doc in docs]})

    async def find(self, org_id: str, kind: str, norm_keys: list[str]) -> list[dict]:
        if not norm_keys:
            return []
        if self._dialect == "arango":
            rows = await self._execute(FIND_AQL, {"orgId": org_id, "kind": kind, "keys": norm_keys})
            return rows or []
        rows = await self._execute(
            FIND_CYPHER,
            {"orgId": org_id, "kind": kind, "keys": norm_keys, "partition": alias_partition(kind)},
        )
        return _unwrap(rows)

    async def add_aliases(
        self, org_id: str, kind: str, key: str, aliases: list[str], normalized: list[str], now: int
    ) -> None:
        if not aliases:
            return
        binds = {
            "orgId": org_id,
            "kind": kind,
            "key": key,
            "aliases": aliases[:_ALIAS_CAP],
            "normalized": normalized[:_ALIAS_CAP],
            "maxAliases": _ALIAS_CAP,
            "now": now,
            "partition": alias_partition(kind),
        }
        query = ALIAS_AQL if self._dialect == "arango" else ALIAS_CYPHER
        await self._write(query, binds)

    async def query(self, org_id: str, query: NamedEntityQueryLike) -> dict[str, Any]:
        skip = int(query.cursor or 0)
        limit = max(1, min(int(query.limit or 50), 200))
        binds = _bind(query, skip=skip, limit=limit)
        binds["orgId"] = org_id
        statement = QUERY_AQL if self._dialect == "arango" else QUERY_CYPHER
        rows = await self._execute(statement, binds) or []
        entities = rows if self._dialect == "arango" else _unwrap(rows)
        cursor = str(skip + limit) if len(entities) == limit else ""
        return {"entities": entities, "cursor": cursor}

    async def records_for(
        self, org_id: str, entity_ids: list[str] | None, query: NamedEntityQueryLike | None, limit: int
    ) -> dict[str, Any]:
        cap = min(max(1, limit), _CAP)
        fetch = cap + 1
        matched: list[str] = []
        use_ids = bool(entity_ids)
        if not use_ids and query is not None:
            binds = _bind(query, skip=0, limit=fetch)
            binds["orgId"] = org_id
            statement = MATCHED_KEYS_AQL if self._dialect == "arango" else MATCHED_KEYS_CYPHER
            rows = await self._execute(statement, binds) or []
            matched = [row if isinstance(row, str) else (row.get("_key") or row.get("id") or "") for row in rows]
            matched = [item for item in matched if item]
            if len(matched) > cap:
                return {"hits": [], "truncated": True}
        binds = {
            "orgId": org_id,
            "ids": entity_ids or [],
            "matched": matched,
            "useIds": use_ids,
            "limit": fetch,
        }
        statement = RECORDS_AQL if self._dialect == "arango" else RECORDS_CYPHER
        rows = await self._execute(statement, binds) or []
        truncated = len(rows) > cap
        return {"hits": rows[:cap], "truncated": truncated}

    async def entities_for_record(self, record_id: str) -> list[dict]:
        if self._dialect == "arango":
            return await self._execute(RECORD_ENTITIES_AQL, {"recordId": record_id}) or []
        rows = await self._execute(RECORD_ENTITIES_CYPHER, {"recordId": record_id}) or []
        return [row.get("entity", row) for row in rows]

    async def copy_mentions(self, source_key: str, target_key: str) -> int:
        if self._dialect == "arango":
            rows = await self._execute(COPY_AQL, {"source": source_key, "target": target_key}) or []
            return len(rows)
        rows = await self._execute(COPY_CYPHER, {"source": source_key, "target": target_key}) or []
        if rows and isinstance(rows[0], dict):
            return int(rows[0].get("copied") or 0)
        return len(rows)

    async def delete_orphans(self, org_id: str, batch_size: int) -> list[str]:
        binds = {"orgId": org_id, "batch": max(1, batch_size)}
        if self._dialect == "arango":
            rows = await self._write(ORPHAN_AQL, binds) or []
            return [row if isinstance(row, str) else str(row) for row in rows]
        rows = await self._write(ORPHAN_CYPHER, binds) or []
        return [row if isinstance(row, str) else str(row.get("id") or "") for row in rows]

    async def set_status(self, key: str, status: str, ts: int, strategy: str) -> None:
        binds = {"key": key, "status": status, "ts": ts, "strategy": strategy}
        query = STATUS_AQL if self._dialect == "arango" else STATUS_CYPHER
        await self._execute(query, binds)


class NamedEntityGraphMixin:
    """Provider methods. ``_ner_dialect`` is ``arango`` or ``neo4j``."""

    _ner_dialect: str = ""

    def _named_entity_graph(self) -> NamedEntityGraph:
        execute = self.execute_query  # type: ignore[attr-defined]
        retry_conflicts = getattr(self, "_retry_write_conflicts", None)
        retry = (lambda write: retry_conflicts(write, None)) if retry_conflicts else None
        return NamedEntityGraph(execute, self._ner_dialect, retry)

    async def create_named_entities_if_absent(self, nodes: list[dict]) -> None:
        await self._named_entity_graph().create_if_absent(nodes)

    async def find_named_entities(self, org_id: str, kind: str, norm_keys: list[str]) -> list[dict]:
        return await self._named_entity_graph().find(org_id, kind, norm_keys)

    async def add_named_entity_aliases(
        self, org_id: str, kind: str, key: str, aliases: list[str], normalized: list[str]
    ) -> None:
        from app.utils.time_conversion import get_epoch_timestamp_in_ms

        await self._named_entity_graph().add_aliases(
            org_id, kind, key, aliases, normalized, get_epoch_timestamp_in_ms()
        )

    async def query_named_entities(self, org_id: str, query) -> dict:
        return await self._named_entity_graph().query(org_id, query)

    async def get_records_for_named_entities(
        self, org_id: str, entity_ids: list[str] | None = None, query=None, limit: int = 5000
    ) -> dict:
        return await self._named_entity_graph().records_for(org_id, entity_ids, query, limit)

    async def get_named_entities_for_record(self, record_id: str) -> list[dict]:
        return await self._named_entity_graph().entities_for_record(record_id)

    async def copy_named_entity_mentions(self, source_key: str, target_key: str) -> int:
        return await self._named_entity_graph().copy_mentions(source_key, target_key)

    async def delete_orphan_named_entities(self, org_id: str, batch_size: int = 200) -> list[str]:
        return await self._named_entity_graph().delete_orphans(org_id, batch_size)

    async def set_entity_extraction_status(self, record_id: str, status: str, strategy: str) -> None:
        from app.utils.time_conversion import get_epoch_timestamp_in_ms

        await self._named_entity_graph().set_status(
            record_id, status, get_epoch_timestamp_in_ms(), strategy
        )


def _unwrap(rows: list | None) -> list[dict]:
    out = []
    for row in rows or []:
        if isinstance(row, dict) and "n" in row and isinstance(row["n"], dict):
            out.append(row["n"])
        elif isinstance(row, dict):
            out.append(row)
    return out
