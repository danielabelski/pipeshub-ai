from datetime import UTC, datetime

import pytest

from app.modules.named_entities.domain.kinds import EntityKind, default_enabled_kinds
from app.modules.named_entities.normalizers.dates import (
    NormalizationContext,
    normalize_temporal,
)
from app.modules.named_entities.normalizers.money import normalize_money
from app.modules.named_entities.normalizers.quantities import (
    normalize_percent,
    normalize_quantity,
)
from app.modules.named_entities.recognizers.pattern import (
    PatternRecognizer,
    is_suppressed_secret,
)
from app.modules.named_entities.recognizers.values import ValueCandidateRecognizer
from app.modules.named_entities.text import TextUnit


def _ms(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=UTC).timestamp() * 1000)


def test_quarter_is_half_open():
    value = normalize_temporal(EntityKind.DATE_RANGE, "Q3 FY25", NormalizationContext(tz="UTC"))
    assert value is not None
    assert value.start_ms == _ms(2025, 7, 1)
    assert value.end_ms == _ms(2025, 10, 1)
    assert value.ambiguous is False


def test_day_month_without_year_has_no_range():
    value = normalize_temporal(EntityKind.DATE, "12 April", NormalizationContext(tz="UTC"))
    assert value is not None
    assert value.ambiguous is True
    assert value.start_ms is None
    assert value.end_ms is None


def test_next_friday_is_anchored_to_the_record():
    # Thursday 1 Jan 2026. The next Friday is 2 Jan.
    reference = _ms(2026, 1, 1)
    value = normalize_temporal(
        EntityKind.DATE,
        "next Friday",
        NormalizationContext(reference_time_ms=reference, tz="UTC"),
    )
    assert value is not None
    assert value.anchored is True
    assert value.start_ms == _ms(2026, 1, 2)


@pytest.mark.parametrize(
    ("surface", "granularity", "start", "end"),
    [
        # Tuesday 10 Mar 2026.
        ("last week", "week", (2026, 3, 2), (2026, 3, 9)),
        ("this month", "month", (2026, 3, 1), (2026, 4, 1)),
        ("next quarter", "quarter", (2026, 4, 1), (2026, 7, 1)),
        ("this year", "year", (2026, 1, 1), (2027, 1, 1)),
        ("FY2025", "year", (2025, 1, 1), (2026, 1, 1)),
        ("15 May 2026", "day", (2026, 5, 15), (2026, 5, 16)),
        ("May 15 2026", "day", (2026, 5, 15), (2026, 5, 16)),
    ],
)
def test_periods_cover_the_whole_period(surface, granularity, start, end):
    ctx = NormalizationContext(reference_time_ms=_ms(2026, 3, 10), tz="UTC")
    value = normalize_temporal(EntityKind.DATE_RANGE, surface, ctx)
    assert value is not None
    assert (value.granularity, value.start_ms, value.end_ms) == (granularity, _ms(*start), _ms(*end))


@pytest.mark.parametrize(
    ("text", "surface", "kind"),
    [
        ("Revenue grew 12.5% to plan.", "12.5%", EntityKind.PERCENTAGE),
        ("Rate is 0.75 percent now.", "0.75 percent", EntityKind.PERCENTAGE),
        ("Ship by 15 May 2026, please.", "15 May 2026", EntityKind.DATE),
        ("Plan for FY2025 is set.", "FY2025", EntityKind.DATE_RANGE),
        ("It costs about 800 dollars.", "800 dollars", EntityKind.CURRENCY),
    ],
)
def test_value_recognizer_proposes_the_whole_span(text, surface, kind):
    mentions = ValueCandidateRecognizer(default_enabled_kinds()).recognize([TextUnit(0, "b", text)])
    assert [(m.surface, m.kind) for m in mentions] == [(surface, kind)]


@pytest.mark.parametrize(
    ("left", "right"),
    [("$1,250", "USD 1,250.00"), ("12.5%", "12.50%"), ("5 km", "5000 m")],
)
def test_equal_values_share_one_key(left, right):
    from app.modules.named_entities.mentions import RawMention
    from app.modules.named_entities.merge import mentions_to_entities

    def key(surface: str) -> str:
        kind = ValueCandidateRecognizer(default_enabled_kinds()).recognize([TextUnit(0, "b", surface)])[0].kind
        raw = RawMention(kind=kind, surface=surface, block_index=0, block_id="b", char_start=0,
                         char_end=len(surface), extractor="value")
        return mentions_to_entities([raw], NormalizationContext())[0].norm_key

    assert key(left) == key(right)


def test_version_is_not_money():
    assert normalize_money("v1.2") is None
    assert normalize_money("1.2") is None


def test_scaled_dollar_amount_is_usd_and_ambiguous():
    value = normalize_money("$1.2M")
    assert value is not None
    assert value.currency == "USD"
    assert value.amount_float == 1_200_000
    assert value.currency_ambiguous is True


@pytest.mark.parametrize(
    ("surface", "amount", "currency", "ambiguous"),
    [
        ("EUR 1.250,00", 1250, "EUR", False),
        ("1,250.00 USD", 1250, "USD", False),
        ("€1.250,50", 1250.5, "EUR", False),
        ("₹ 5,000", 5000, "INR", False),
        ("CHF 99.50", 99.5, "CHF", False),
        ("¥300", 300, "JPY", True),
        ("$2.5bn", 2_500_000_000, "USD", True),
    ],
)
def test_money_table(surface, amount, currency, ambiguous):
    value = normalize_money(surface)
    assert value is not None
    assert value.amount_float == amount
    assert value.currency == currency
    assert value.currency_ambiguous is ambiguous


def test_unknown_code_is_not_money():
    assert normalize_money("ABC 100") is None


@pytest.mark.parametrize(
    "text",
    ["Budget is EUR 1.250,00 total", "We owe 1,250.00 USD", "Paid $1.2M upfront", "Fee ₹ 5,000"],
)
def test_money_recognizer_proposes_the_whole_span(text):
    mentions = ValueCandidateRecognizer(frozenset({EntityKind.CURRENCY})).recognize(
        [TextUnit(block_index=0, block_id="b", text=text)]
    )
    assert len(mentions) == 1
    assert normalize_money(mentions[0].surface) is not None


def test_money_recognizer_does_not_join_separate_numbers():
    mentions = ValueCandidateRecognizer(frozenset({EntityKind.CURRENCY})).recognize(
        [TextUnit(block_index=0, block_id="b", text="$5 10 items")]
    )
    assert [m.surface for m in mentions] == ["$5"]


def test_celsius_converts_to_kelvin():
    value = normalize_quantity("20 °C")
    assert value is not None
    assert value.si_unit == "K"
    assert abs(value.si_value - 293.15) < 1e-9


def test_basis_points_are_a_fraction():
    value = normalize_percent("300 bps")
    assert value is not None
    assert float(value.value) == 0.03


def test_kilometres_convert_to_metres():
    value = normalize_quantity("5 km")
    assert value is not None
    assert value.si_unit == "m"
    assert value.si_value == 5000


@pytest.mark.parametrize(
    "secret",
    ["GB82 WEST 1234 5698 7654 32", "DE89370400440532013000", "078-05-1120", "AB 12 34 56 C", "ABCDE1234F"],
)
def test_ibans_and_national_ids_are_suppressed(secret):
    assert is_suppressed_secret(f"ref {secret} end") is True
    mentions = PatternRecognizer(default_enabled_kinds() | {EntityKind.PHONE}).recognize(
        [TextUnit(block_index=0, block_id="b", text=f"ref {secret} end")]
    )
    assert mentions == []


def test_url_span_excludes_trailing_punctuation():
    text = "see https://example.com/a)."
    (mention,) = PatternRecognizer(frozenset({EntityKind.URL})).recognize(
        [TextUnit(block_index=0, block_id="b", text=text)]
    )
    assert text[mention.char_start : mention.char_end] == mention.surface == "https://example.com/a"


def test_card_number_is_suppressed():
    card = "4111111111111111"
    assert is_suppressed_secret(card) is True
    mentions = PatternRecognizer(default_enabled_kinds()).recognize(
        [TextUnit(block_index=0, block_id="b", text=f"pay {card} now")]
    )
    assert all(card not in mention.surface for mention in mentions)
