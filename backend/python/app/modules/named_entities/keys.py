"""Deterministic identity of a named entity: uuid5(org, kind, norm_key)."""

from __future__ import annotations

import uuid

NAMED_ENTITY_NAMESPACE = uuid.UUID("c3a1e7b2-4d58-4f0a-9b6e-2a7d8c1f0e55")


def named_entity_key(org_id: str, kind: str, norm_key: str) -> str:
    return str(uuid.uuid5(NAMED_ENTITY_NAMESPACE, f"{org_id}:{kind}:{norm_key}"))


def alias_partition(kind: str) -> str:
    """Neo4j TaxonomyAlias ``collection`` value, unique per kind."""
    return f"namedEntities:{kind}"
