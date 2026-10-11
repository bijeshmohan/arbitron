from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from uuid import UUID

from arbitron.domain._checks import require_text, require_utc
from arbitron.domain.enums import OrderStatus, Severity
from arbitron.domain.errors import DomainValidationError

# Event payloads must already be sanitized: scalars only, no secrets, no raw
# exchange responses.
type PayloadValue = str | int | bool | None
type Payload = Mapping[str, PayloadValue]


def _freeze(payload: Mapping[str, object]) -> Payload:
    frozen: dict[str, PayloadValue] = {}
    for key, value in payload.items():
        if not isinstance(value, str | int | bool | None):
            msg = f"payload[{key!r}] must be str, int, bool, or None; got {type(value).__name__}"
            raise DomainValidationError(msg)
        frozen[key] = value
    return MappingProxyType(frozen)


@dataclass(frozen=True, slots=True)
class OrderEvent:
    """Append-only record of one order lifecycle transition."""

    order_id: UUID
    from_status: OrderStatus | None  # None for the creating event
    to_status: OrderStatus
    occurred_at: datetime
    correlation_id: str
    payload: Payload = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_utc("occurred_at", self.occurred_at)
        require_text("correlation_id", self.correlation_id)
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class SystemEvent:
    event_type: str
    severity: Severity
    component: str
    occurred_at: datetime
    correlation_id: str
    payload: Payload = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_text("event_type", self.event_type)
        require_text("component", self.component)
        require_utc("occurred_at", self.occurred_at)
        require_text("correlation_id", self.correlation_id)
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True, slots=True)
class Alert:
    """An operator notification. Always states what the system already did."""

    severity: Severity
    title: str
    message: str
    action_taken: str
    remediation: str
    dedupe_key: str
    occurred_at: datetime

    def __post_init__(self) -> None:
        for name in ("title", "message", "action_taken", "remediation", "dedupe_key"):
            require_text(name, getattr(self, name))
        require_utc("occurred_at", self.occurred_at)
