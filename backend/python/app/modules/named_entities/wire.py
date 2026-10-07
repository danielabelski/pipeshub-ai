"""The schema the model fills. Flat, required, no unions.

Provider structured-output support diverges on ``anyOf``, nullable fields
and numeric constraints. This shape stays inside the intersection: strings,
one inline enum, integers, and ``extra=forbid``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.named_entities.domain.kinds import EntityKind

WireKind = Literal[
    "person",
    "person_type",
    "organization",
    "event",
    "product",
    "skill",
    "location",
    "city",
    "state",
    "country_region",
    "address",
    "date",
    "date_range",
    "date_time",
    "duration",
    "currency",
    "percentage",
    "age",
    "dimension",
    "email",
    "url",
    "phone",
    "ip",
]


class WireEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(description="Exact surface form copied verbatim from the block. Never paraphrase.")
    block: int = Field(description="n from the [B<n>] tag where the text appears.")
    kind: WireKind
    normalized: str = Field(
        description=(
            "Normalization hint or ''. date/date_time: ISO 8601. date_range: start/end ISO interval. "
            "duration: ISO 8601 (P3M). currency: '<ISO4217> <amount>' e.g. 'USD 1250.00'. "
            "percentage: a fraction such as 0.125. age: '<n> year'. dimension: '<n> <unit>'."
        )
    )


class EntitySubmission(BaseModel):
    """Entities for the blocks just read. May be called more than once."""

    model_config = ConfigDict(extra="forbid")

    entities: list[WireEntity] = Field(
        description="In order of appearance; at most 100 per call."
    )


def parse_wire_kind(raw: str) -> EntityKind | None:
    try:
        return EntityKind(str(raw).casefold())
    except ValueError:
        return None
