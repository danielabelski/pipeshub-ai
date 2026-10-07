"""Domain entities persisted to the blob and projected onto the graph."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.values import TypedValue

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "ner-1"

Grounding = Literal["exact", "fuzzy"]
ExtractorName = Literal["pattern", "value", "agent", "single_call"]
TerminationReason = Literal[
    "deterministic",
    "finish_ok",
    "no_progress",
    "budget_turns",
    "budget_tokens",
    "timeout",
    "llm_error",
    "single_call",
    "empty",
]
ExtractionStatus = Literal["COMPLETED", "PARTIAL", "FAILED", "SKIPPED"]


class Mention(BaseModel):
    model_config = ConfigDict(extra="forbid")

    block_index: int
    block_id: str = ""
    char_start: int
    char_end: int
    surface: str
    grounding: Grounding = "exact"
    extractor: ExtractorName
    extractor_version: str = EXTRACTOR_VERSION
    evidence_score: float = 1.0


class NamedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    tags: list[str] = Field(default_factory=list)
    display_name: str
    norm_key: str
    value: TypedValue | None = None
    normalized_raw: str = ""
    mentions: list[Mention] = Field(default_factory=list)


class ExtractionStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    units: int = 0
    deterministic: int = 0
    submitted: int = 0
    accepted: int = 0
    rejected: int = 0
    dropped_cap: int = 0
    turns: int = 0
    tool_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0


class NamedEntityExtraction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    extractor_version: str = EXTRACTOR_VERSION
    strategy: str = "deterministic"
    termination_reason: TerminationReason = "deterministic"
    status: ExtractionStatus = "COMPLETED"
    entities: list[NamedEntity] = Field(default_factory=list)
    stats: ExtractionStats = Field(default_factory=ExtractionStats)
