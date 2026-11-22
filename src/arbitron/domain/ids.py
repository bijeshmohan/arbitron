"""Deterministic identifiers.

Identifiers derived from stable business keys make replay reproducible and
make duplicate processing collapse onto the same row instead of creating a
second one.
"""

import re
from uuid import UUID, uuid5

from arbitron.domain.errors import DomainValidationError

ARBITRON_NAMESPACE = UUID("3d617b38-9aa6-476b-94be-0678f7b758cf")

CLIENT_ORDER_ID_PREFIX = "arb"
CLIENT_ORDER_ID_LENGTH = 32
_CLIENT_ORDER_ID_RE = re.compile(rf"^{CLIENT_ORDER_ID_PREFIX}[0-9a-f]{{29}}$")


def deterministic_id(kind: str, key: str) -> UUID:
    """UUIDv5 scoped by entity kind, so equal keys of different kinds never collide."""
    return uuid5(ARBITRON_NAMESPACE, f"{kind}:{key}")


def client_order_id_for(intent_id: UUID) -> str:
    """Canonical client order ID: ``arb`` + 29 lowercase hex chars (32 chars total).

    Alphanumeric and at most 32 characters, which fits the common venue limits.
    Venue adapters may map it to a venue-specific form, but must do so
    deterministically.
    """
    return f"{CLIENT_ORDER_ID_PREFIX}{intent_id.hex[:29]}"


def is_canonical_client_order_id(value: str) -> bool:
    return _CLIENT_ORDER_ID_RE.match(value) is not None


_HEX128_PREFIX = "0x000"
_HEX128_RE = re.compile(r"^0x000[0-9a-f]{29}$")


def to_hex128_client_id(client_order_id: str) -> str:
    """Our client order ID as a 128-bit hex value (``0x`` + 32 hex), e.g. a Hyperliquid ``cloid``.

    Reversible without a stored mapping: ``arb`` becomes ``000``.
    """
    if not is_canonical_client_order_id(client_order_id):
        msg = f"not a canonical client order ID: {client_order_id!r}"
        raise DomainValidationError(msg)
    return _HEX128_PREFIX + client_order_id[len(CLIENT_ORDER_ID_PREFIX) :]


def from_hex128_client_id(value: str) -> str | None:
    """The client order ID a 128-bit venue ID encodes, or ``None`` if it isn't one of ours."""
    if _HEX128_RE.match(value) is None:
        return None
    return CLIENT_ORDER_ID_PREFIX + value[len(_HEX128_PREFIX) :]
