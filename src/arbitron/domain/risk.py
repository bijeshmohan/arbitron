"""Risk decisions and the durable kill switch (plan §11)."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final
from uuid import UUID

from arbitron.domain._checks import require_text, require_utc
from arbitron.domain.enums import KillSwitchState
from arbitron.domain.errors import DomainValidationError, InvalidTransitionError


class RiskReason(StrEnum):
    """Reason codes attached to rejected risk decisions."""

    # Fail-closed system conditions (§11.4)
    KILL_SWITCH_NOT_ARMED = "kill_switch_not_armed"
    TRADING_DISABLED = "trading_disabled"
    SYSTEM_NOT_READY = "system_not_ready"
    RISK_STATE_UNAVAILABLE = "risk_state_unavailable"
    RECONCILIATION_UNRESOLVED = "reconciliation_unresolved"
    MARKET_DATA_STALE = "market_data_stale"
    VALUATION_UNAVAILABLE = "valuation_unavailable"
    EXCHANGE_ERROR_RATE = "exchange_error_rate"
    # Policy limits (§11.1)
    MARKET_NOT_ALLOWED = "market_not_allowed"
    OUTSIDE_SCHEDULE = "outside_schedule"
    MAX_ORDER_NOTIONAL = "max_order_notional"
    MAX_ASSET_EXPOSURE = "max_asset_exposure"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    MAX_OPEN_ORDERS = "max_open_orders"
    MAX_DAILY_LOSS = "max_daily_loss"
    MAX_DRAWDOWN = "max_drawdown"
    ORDER_FREQUENCY = "order_frequency"
    BALANCE_RESERVE = "balance_reserve"
    SPREAD_TOO_WIDE = "spread_too_wide"
    MARKET_ORDERS_DISALLOWED = "market_orders_disallowed"
    # Re-verified sizing invariants (§11.2)
    INSUFFICIENT_POSITION = "insufficient_position"
    PRECISION_VIOLATION = "precision_violation"
    BELOW_MINIMUM = "below_minimum"


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """Immutable outcome of evaluating one intent. Approved iff no reasons."""

    intent_id: UUID
    reasons: tuple[RiskReason, ...]
    policy_version: str
    decided_at: datetime
    # Evaluated limits and observed values, as exact decimal strings.
    observations: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        require_text("policy_version", self.policy_version)
        require_utc("decided_at", self.decided_at)
        if len(set(self.reasons)) != len(self.reasons):
            msg = "risk reasons must be unique"
            raise DomainValidationError(msg)
        object.__setattr__(self, "observations", MappingProxyType(dict(self.observations)))

    @property
    def approved(self) -> bool:
        return not self.reasons


KILL_SWITCH_TRANSITIONS: Final[Mapping[KillSwitchState, frozenset[KillSwitchState]]] = (
    MappingProxyType(
        {
            # Startup recovery may go straight to review (§14.1 step 9).
            KillSwitchState.ARMED: frozenset(
                {KillSwitchState.TRIPPED, KillSwitchState.RECOVERY_REVIEW}
            ),
            # Re-tripping while tripped records a new trigger.
            KillSwitchState.TRIPPED: frozenset(
                {KillSwitchState.TRIPPED, KillSwitchState.RECOVERY_REVIEW}
            ),
            # Only an operator acknowledgment re-arms; a new trigger re-trips.
            KillSwitchState.RECOVERY_REVIEW: frozenset(
                {KillSwitchState.ARMED, KillSwitchState.TRIPPED}
            ),
        }
    )
)

SYSTEM_ACTOR: Final = "system"


@dataclass(frozen=True, slots=True)
class KillSwitch:
    """Durable kill-switch state. Only ``ARMED`` permits new orders.

    There is deliberately no path from ``TRIPPED`` to ``ARMED`` that skips
    ``RECOVERY_REVIEW``, and only a named operator can re-arm.
    """

    state: KillSwitchState
    changed_at: datetime
    actor: str
    reason: str

    def __post_init__(self) -> None:
        require_utc("changed_at", self.changed_at)
        require_text("actor", self.actor)
        require_text("reason", self.reason)

    @property
    def allows_new_orders(self) -> bool:
        return self.state is KillSwitchState.ARMED

    def _to(
        self, target: KillSwitchState, *, at: datetime, actor: str, reason: str
    ) -> "KillSwitch":
        if target not in KILL_SWITCH_TRANSITIONS[self.state]:
            msg = f"kill switch: {self.state} -> {target} is not allowed"
            raise InvalidTransitionError(msg)
        require_utc("at", at)
        if at < self.changed_at:
            msg = f"kill switch: transition time {at} precedes last change {self.changed_at}"
            raise InvalidTransitionError(msg)
        return KillSwitch(state=target, changed_at=at, actor=actor, reason=reason)

    def trip(self, *, at: datetime, reason: str, actor: str = SYSTEM_ACTOR) -> "KillSwitch":
        return self._to(KillSwitchState.TRIPPED, at=at, actor=actor, reason=reason)

    def begin_review(self, *, at: datetime, reason: str, actor: str = SYSTEM_ACTOR) -> "KillSwitch":
        return self._to(KillSwitchState.RECOVERY_REVIEW, at=at, actor=actor, reason=reason)

    def rearm(self, *, at: datetime, operator: str, acknowledgement: str) -> "KillSwitch":
        if operator.strip() == SYSTEM_ACTOR:
            msg = "kill switch can only be re-armed by a named operator, not the system"
            raise InvalidTransitionError(msg)
        return self._to(KillSwitchState.ARMED, at=at, actor=operator, reason=acknowledgement)
