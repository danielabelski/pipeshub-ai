"""Turn a surface form into a typed value. Failure returns None, never a guess."""

from __future__ import annotations

from app.modules.named_entities.domain.kinds import EntityKind, EntitySource, spec_for
from app.modules.named_entities.domain.values import TypedValue
from app.modules.named_entities.normalizers.contacts import normalize_contact
from app.modules.named_entities.normalizers.dates import (
    NormalizationContext,
    normalize_temporal,
)
from app.modules.named_entities.normalizers.money import normalize_money
from app.modules.named_entities.normalizers.names import name_value
from app.modules.named_entities.normalizers.quantities import (
    normalize_age,
    normalize_percent,
    normalize_quantity,
)

_TEMPORAL = {EntityKind.DATE, EntityKind.DATE_RANGE, EntityKind.DATE_TIME, EntityKind.DURATION}


def normalize_value(
    kind: EntityKind, surface: str, ctx: NormalizationContext, hint: str = ""
) -> TypedValue | None:
    text = (hint or surface or "").strip()
    if kind in _TEMPORAL:
        parsed = normalize_temporal(kind, text, ctx)
        if parsed is None and hint and hint != surface:
            parsed = normalize_temporal(kind, surface, ctx)
        return parsed
    if kind is EntityKind.CURRENCY:
        return normalize_money(text) or normalize_money(surface)
    if kind is EntityKind.PERCENTAGE:
        return normalize_percent(text) or normalize_percent(surface)
    if kind is EntityKind.AGE:
        return normalize_age(surface)
    if kind is EntityKind.DIMENSION:
        return normalize_quantity(surface)
    spec = spec_for(kind)
    if spec is not None and spec.source is EntitySource.PATTERN:
        return normalize_contact(kind, surface)
    if spec is not None and spec.source is EntitySource.SEMANTIC:
        return name_value(surface, organization=kind is EntityKind.ORGANIZATION)
    return None
