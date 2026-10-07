"""Resolve overlapping spans and collapse one record's mentions onto entities."""

from __future__ import annotations

from app.modules.named_entities.domain.kinds import EntityKind, spec_for
from app.modules.named_entities.domain.models import Mention, NamedEntity
from app.modules.named_entities.mentions import RawMention
from app.modules.named_entities.normalizers import normalize_value
from app.modules.named_entities.normalizers.dates import NormalizationContext
from app.modules.named_entities.normalizers.names import canonical_name, norm_key_for
from app.modules.named_entities.recognizers.pattern import is_suppressed_secret

_PRIORITY = {"pattern": 0, "value": 1, "agent": 2, "single_call": 3}


def _overlaps(left: RawMention, right: RawMention) -> bool:
    return (
        left.block_index == right.block_index
        and left.char_start < right.char_end
        and right.char_start < left.char_end
    )


def resolve_overlaps(mentions: list[RawMention]) -> list[RawMention]:
    """Pattern beats value beats the model. A longer span wins inside one source."""
    ordered = sorted(
        mentions,
        key=lambda item: (
            _PRIORITY.get(item.extractor, 9),
            -(item.char_end - item.char_start),
            item.char_start,
        ),
    )
    kept: list[RawMention] = []
    for mention in ordered:
        if any(_overlaps(mention, existing) for existing in kept):
            continue
        kept.append(mention)
    return kept


def mentions_to_entities(
    mentions: list[RawMention], ctx: NormalizationContext
) -> list[NamedEntity]:
    grouped: dict[tuple[EntityKind, str], NamedEntity] = {}
    for raw in resolve_overlaps(mentions):
        if is_suppressed_secret(raw.surface):
            continue
        spec = spec_for(raw.kind)
        if spec is None:
            continue
        value = normalize_value(raw.kind, raw.surface, ctx, raw.normalized_hint)
        display = canonical_name(raw.surface, organization=raw.kind is EntityKind.ORGANIZATION)
        key = norm_key_for(raw.kind, display, value)
        entity = grouped.get((raw.kind, key))
        mention = Mention(
            block_index=raw.block_index,
            block_id=raw.block_id,
            char_start=raw.char_start,
            char_end=raw.char_end,
            surface=raw.surface[:256],
            grounding=raw.grounding,
            extractor=raw.extractor,
            evidence_score=raw.evidence_score,
        )
        if entity is None:
            grouped[(raw.kind, key)] = NamedEntity(
                kind=raw.kind,
                tags=list(spec.tags),
                display_name=display[:256] or raw.surface[:256],
                norm_key=key,
                value=value,
                normalized_raw=raw.normalized_hint[:256],
                mentions=[mention],
            )
        else:
            entity.mentions.append(mention)
    return list(grouped.values())
