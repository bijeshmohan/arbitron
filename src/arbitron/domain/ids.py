"""Deterministic identifiers.

Identifiers derived from stable business keys make replay reproducible and
make duplicate processing collapse onto the same row instead of creating a
second one.
"""

import re
from uuid import UUID, uuid5

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
