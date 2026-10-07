"""Display names and the deterministic norm_key each kind is stored under."""

from __future__ import annotations

import re
from decimal import Decimal

from app.modules.entity_resolution.normalizer import display_form, normalize_name
from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.values import (
    ContactValue,
    DateRangeValue,
    DurationValue,
    MoneyValue,
    NameValue,
    PercentValue,
    QuantityValue,
    TypedValue,
)

_LEGAL_SUFFIX = re.compile(
    r"\b(incorporated|inc|llc|l\.l\.c|ltd|limited|gmbh|corp|corporation|co)\b\.?$",
    re.IGNORECASE,
)


def canonical_name(surface: str, *, organization: bool = False) -> str:
    display = display_form(surface)
    if organization:
        stripped = _LEGAL_SUFFIX.sub("", display).strip(" ,.")
        if stripped:
            return display_form(stripped)
    return display


def _number(value: Decimal | float) -> str:
    # One key per quantity: "$1,250" and "USD 1,250.00" are the same entity.
    exact = value if isinstance(value, Decimal) else Decimal(repr(float(value)))
    return format(exact.normalize(), "f")


def name_value(surface: str, *, organization: bool = False) -> NameValue:
    return NameValue(canonical=canonical_name(surface, organization=organization))


def norm_key_for(kind: EntityKind, display: str, value: TypedValue | None) -> str:
    if isinstance(value, DateRangeValue):
        if value.ambiguous or value.start_ms is None:
            return f"date:unresolved:{value.timex or normalize_name(display)}"
        return f"date:{value.granularity}:{value.start_ms}:{value.end_ms}"
    if isinstance(value, DurationValue):
        return f"duration:{value.iso}"
    if isinstance(value, MoneyValue):
        return f"money:{value.currency}:{_number(value.amount)}"
    if isinstance(value, PercentValue):
        return f"pct:{_number(value.value)}"
    if isinstance(value, QuantityValue):
        return f"qty:{value.dimension}:{_number(value.si_value)}"
    if isinstance(value, ContactValue):
        return f"{value.scheme}:{value.canonical}"
    key = normalize_name(canonical_name(display, organization=kind is EntityKind.ORGANIZATION))
    return f"name:{kind.value}:{key}"
