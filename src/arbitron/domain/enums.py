from enum import StrEnum


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class OrderType(StrEnum):
    MARKET = "market"
    LIMIT = "limit"


class TimeInForce(StrEnum):
    GTC = "gtc"  # good till canceled
    IOC = "ioc"  # immediate or cancel
    FOK = "fok"  # fill or kill


class MarketType(StrEnum):
    SPOT = "spot"


class OrderStatus(StrEnum):
    """Internal normalized order status (plan §6.2).

    Venue-specific raw statuses are preserved separately on ``ExchangeOrder``.
    """

    # Non-terminal
    CREATED = "created"
    RISK_APPROVED = "risk_approved"
    SUBMITTING = "submitting"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    CANCEL_REQUESTED = "cancel_requested"
    SUBMISSION_UNKNOWN = "submission_unknown"
    RECONCILIATION_REQUIRED = "reconciliation_required"
    # Terminal
    RISK_REJECTED = "risk_rejected"
    EXCHANGE_REJECTED = "exchange_rejected"
    NOT_PLACED = "not_placed"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    PARTIALLY_FILLED_CLOSED = "partially_filled_closed"


class KillSwitchState(StrEnum):
    ARMED = "armed"  # normal: trading permitted, switch ready to trip
    TRIPPED = "tripped"  # new orders blocked
    RECOVERY_REVIEW = "recovery_review"  # blocked pending operator acknowledgment


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
