"""Canonical forms for emails, URLs, phones and IP addresses."""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlsplit, urlunsplit

from app.modules.named_entities.domain.kinds import EntityKind
from app.modules.named_entities.domain.values import ContactValue

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_contact(kind: EntityKind, surface: str) -> ContactValue | None:
    text = (surface or "").strip()
    if kind is EntityKind.EMAIL:
        if not _EMAIL.match(text):
            return None
        local, _, domain = text.rpartition("@")
        return ContactValue(scheme="email", canonical=f"{local}@{domain.casefold()}")
    if kind is EntityKind.URL:
        parsed = urlsplit(text if "://" in text else f"https://{text}")
        if not parsed.netloc:
            return None
        canonical = urlunsplit((
            (parsed.scheme or "https").casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/") or "",
            "",
            "",
        ))
        return ContactValue(scheme="url", canonical=canonical)
    if kind is EntityKind.PHONE:
        digits = re.sub(r"\D", "", text)
        if not (10 <= len(digits) <= 15):
            return None
        try:
            import phonenumbers
            parsed = phonenumbers.parse(text, "US")
            if phonenumbers.is_possible_number(parsed):
                canonical = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
                return ContactValue(scheme="phone", canonical=canonical)
        except Exception:
            pass
        canonical = digits if text.strip().startswith("+") else f"+{digits}"
        return ContactValue(scheme="phone", canonical=canonical)
    if kind is EntityKind.IP:
        try:
            canonical = str(ipaddress.ip_interface(text))
        except ValueError:
            return None
        return ContactValue(scheme="ip", canonical=canonical)
    return None
