"""Money spans. A bare '$' is USD and flagged ambiguous. No FX conversion."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from app.modules.named_entities.domain.values import MoneyValue

# Active ISO-4217 codes. A code outside this set is not a currency, so
# "ABC 100" stays unrecognized instead of inventing a currency.
ISO_4217: frozenset[str] = frozenset({
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN", "BAM", "BBD",
    "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BRL", "BSD", "BTN", "BWP", "BYN",
    "BZD", "CAD", "CDF", "CHF", "CLP", "CNY", "COP", "CRC", "CUP", "CVE", "CZK", "DJF",
    "DKK", "DOP", "DZD", "EGP", "ERN", "ETB", "EUR", "FJD", "FKP", "GBP", "GEL", "GHS",
    "GIP", "GMD", "GNF", "GTQ", "GYD", "HKD", "HNL", "HTG", "HUF", "IDR", "ILS", "INR",
    "IQD", "IRR", "ISK", "JMD", "JOD", "JPY", "KES", "KGS", "KHR", "KMF", "KPW", "KRW",
    "KWD", "KYD", "KZT", "LAK", "LBP", "LKR", "LRD", "LSL", "LYD", "MAD", "MDL", "MGA",
    "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR", "MWK", "MXN", "MYR", "MZN", "NAD",
    "NGN", "NIO", "NOK", "NPR", "NZD", "OMR", "PAB", "PEN", "PGK", "PHP", "PKR", "PLN",
    "PYG", "QAR", "RON", "RSD", "RUB", "RWF", "SAR", "SBD", "SCR", "SDG", "SEK", "SGD",
    "SHP", "SLE", "SOS", "SRD", "SSP", "STN", "SYP", "SZL", "THB", "TJS", "TMT", "TND",
    "TOP", "TRY", "TTD", "TWD", "TZS", "UAH", "UGX", "USD", "UYU", "UZS", "VES", "VND",
    "VUV", "WST", "XAF", "XCD", "XOF", "XPF", "YER", "ZAR", "ZMW", "ZWL",
})
# '$' and '¥' are shared by several currencies; the default is flagged ambiguous.
SYMBOLS: dict[str, tuple[str, bool]] = {
    "$": ("USD", True),
    "US$": ("USD", False),
    "€": ("EUR", False),
    "£": ("GBP", False),
    "¥": ("JPY", True),
    "₹": ("INR", False),
    "₩": ("KRW", False),
    "₽": ("RUB", False),
    "₺": ("TRY", False),
    "₪": ("ILS", False),
    "₫": ("VND", False),
    "₱": ("PHP", False),
    "R$": ("BRL", False),
    "C$": ("CAD", False),
    "A$": ("AUD", False),
}
# "pounds" is left out: it is as often a weight as a currency.
WORDS: dict[str, tuple[str, bool]] = {
    "dollar": ("USD", True),
    "euro": ("EUR", False),
    "yen": ("JPY", False),
    "rupee": ("INR", True),
}
_WORD = re.compile(r"\b(?P<word>dollar|euro|yen|rupee)s?\b", re.IGNORECASE)
_SCALES = {
    "k": Decimal("1000"),
    "m": Decimal("1000000"),
    "mm": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "bn": Decimal("1000000000"),
}
_SCALE = re.compile(r"(?<=\d)\s?(?P<scale>bn|mm|[kmb])(?=\s|$|[A-Z]{3}\b)", re.IGNORECASE)
_VERSION = re.compile(r"v?\d+(?:\.\d+)+", re.IGNORECASE)
_CODE = re.compile(r"\b(?P<code>[A-Z]{3})\b")


def _currency(text: str, parsed: str | None) -> tuple[str, bool] | None:
    code = _CODE.search(text)
    if code and code.group("code") in ISO_4217:
        return code.group("code"), False
    if parsed:
        upper = parsed.strip().upper()
        if upper in ISO_4217:
            return upper, False
        if parsed.strip() in SYMBOLS:
            return SYMBOLS[parsed.strip()]
    for symbol in sorted(SYMBOLS, key=len, reverse=True):
        if symbol in text:
            return SYMBOLS[symbol]
    word = _WORD.search(text)
    if word:
        return WORDS[word.group("word").casefold()]
    return None


def _amount(text: str) -> tuple[Decimal | None, str | None]:
    try:
        from price_parser import Price
    except ImportError:
        match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
        if not match:
            return None, None
        try:
            return Decimal(match.group(0).replace(",", "")), None
        except InvalidOperation:
            return None, None
    price = Price.fromstring(text)
    return price.amount, price.currency


def normalize_money(surface: str) -> MoneyValue | None:
    text = (surface or "").strip()
    if not text or _VERSION.fullmatch(text):
        return None
    scale = Decimal("1")
    scale_match = _SCALE.search(text)
    if scale_match:
        scale = _SCALES[scale_match.group("scale").casefold()]
        text = text[: scale_match.start()] + text[scale_match.end():]
    amount, parsed_currency = _amount(text)
    if amount is None:
        return None
    currency = _currency(text, parsed_currency)
    if currency is None:
        return None
    code, ambiguous = currency
    amount = amount * scale
    return MoneyValue(
        amount=amount,
        amount_float=float(amount),
        currency=code,
        currency_ambiguous=ambiguous,
    )
