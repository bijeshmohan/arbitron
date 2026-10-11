"""PostgreSQL schema (SQLAlchemy 2.x typed declarative mappings).

Row classes are persistence-only; repositories map them to and from domain
objects. Enumerations are stored as text guarded by CHECK constraints (easier
to evolve than PostgreSQL enum types). Financial values use NUMERIC(38, 18).

Tables for later phases (markets, balances, positions, snapshots, bot
instances, strategy configs/runs, heartbeats, risk events) are added by the
migrations of the phase that first uses them.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    Numeric,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from arbitron.domain.enums import (
    KillSwitchState,
    OrderStatus,
    OrderType,
    Severity,
    Side,
    TimeInForce,
)
from arbitron.domain.risk import RiskReason

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

Money = Numeric(38, 18, asdecimal=True)
Timestamp = DateTime(timezone=True)


def _in(column: str, enum: type[StrEnum]) -> str:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return f"{column} IN ({values})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CandleRow(Base):
    __tablename__ = "candles"
    __table_args__ = (
        CheckConstraint("open > 0 AND high > 0 AND low > 0 AND close > 0", name="positive_prices"),
        CheckConstraint("low <= LEAST(open, close) AND GREATEST(open, close) <= high", name="ohlc"),
        CheckConstraint("volume >= 0", name="non_negative_volume"),
    )

    exchange_id: Mapped[str] = mapped_column(Text, primary_key=True)
    symbol: Mapped[str] = mapped_column(Text, primary_key=True)
    timeframe: Mapped[str] = mapped_column(Text, primary_key=True)
    open_time: Mapped[datetime] = mapped_column(Timestamp, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Money)
    high: Mapped[Decimal] = mapped_column(Money)
    low: Mapped[Decimal] = mapped_column(Money)
    close: Mapped[Decimal] = mapped_column(Money)
    volume: Mapped[Decimal] = mapped_column(Money)
    is_closed: Mapped[bool] = mapped_column(Boolean)
    ingested_at: Mapped[datetime] = mapped_column(Timestamp)


class SignalRow(Base):
    __tablename__ = "signals"
    __table_args__ = (CheckConstraint(_in("side", Side), name="side"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    strategy_id: Mapped[str] = mapped_column(Text)
    strategy_version: Mapped[str] = mapped_column(Text)
    exchange_id: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    timeframe: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    source_candle_time: Mapped[datetime] = mapped_column(Timestamp)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    reason_code: Mapped[str] = mapped_column(Text)
    explanation: Mapped[str] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("exchange_id", "client_order_id"),
        UniqueConstraint("exchange_id", "exchange_order_id"),
        CheckConstraint(_in("status", OrderStatus), name="status"),
        CheckConstraint(_in("side", Side), name="side"),
        CheckConstraint(_in("order_type", OrderType), name="order_type"),
        CheckConstraint(
            f"time_in_force IS NULL OR {_in('time_in_force', TimeInForce)}", name="time_in_force"
        ),
        CheckConstraint("amount > 0", name="positive_amount"),
        CheckConstraint("filled_amount >= 0 AND filled_amount <= amount", name="filled_range"),
        CheckConstraint(
            "(order_type = 'limit' AND limit_price > 0) "
            "OR (order_type = 'market' AND limit_price IS NULL)",
            name="limit_price",
        ),
        CheckConstraint("attempt >= 1", name="attempt"),
        Index(None, "exchange_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    signal_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("signals.id"))
    strategy_id: Mapped[str] = mapped_column(Text)
    exchange_id: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    order_type: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money)
    limit_price: Mapped[Decimal | None] = mapped_column(Money)
    time_in_force: Mapped[str | None] = mapped_column(Text)
    intent_created_at: Mapped[datetime] = mapped_column(Timestamp)
    sizing_policy: Mapped[str] = mapped_column(Text)
    sizing_inputs: Mapped[dict[str, Any]] = mapped_column(JSONB)
    attempt: Mapped[int] = mapped_column(Integer)
    client_order_id: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    exchange_order_id: Mapped[str | None] = mapped_column(Text)
    filled_amount: Mapped[Decimal] = mapped_column(Money)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Money)
    updated_at: Mapped[datetime] = mapped_column(Timestamp)


class OrderEventRow(Base):
    """Append-only (UPDATE/DELETE blocked by trigger)."""

    __tablename__ = "order_events"
    __table_args__ = (
        CheckConstraint(
            f"from_status IS NULL OR {_in('from_status', OrderStatus)}", name="from_status"
        ),
        CheckConstraint(_in("to_status", OrderStatus), name="to_status"),
        Index(None, "order_id", "id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id"))
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(Timestamp)
    correlation_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


class FillRow(Base):
    """Append-only (UPDATE/DELETE blocked by trigger)."""

    __tablename__ = "fills"
    __table_args__ = (
        UniqueConstraint("exchange_id", "trade_id"),
        CheckConstraint(_in("side", Side), name="side"),
        CheckConstraint("amount > 0 AND price > 0", name="positive_amount_price"),
        CheckConstraint(
            "(fee_amount IS NULL AND fee_currency IS NULL) "
            "OR (fee_amount >= 0 AND fee_currency IS NOT NULL)",
            name="fee",
        ),
        Index(None, "order_id", "timestamp"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    dedupe_key: Mapped[str] = mapped_column(Text, unique=True)
    order_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id"))
    exchange_id: Mapped[str] = mapped_column(Text)
    trade_id: Mapped[str | None] = mapped_column(Text)
    exchange_order_id: Mapped[str] = mapped_column(Text)
    symbol: Mapped[str] = mapped_column(Text)
    side: Mapped[str] = mapped_column(Text)
    amount: Mapped[Decimal] = mapped_column(Money)
    price: Mapped[Decimal] = mapped_column(Money)
    fee_amount: Mapped[Decimal | None] = mapped_column(Money)
    fee_currency: Mapped[str | None] = mapped_column(Text)
    timestamp: Mapped[datetime] = mapped_column(Timestamp)


class RiskDecisionRow(Base):
    """Append-only (UPDATE/DELETE blocked by trigger). One decision per intent."""

    __tablename__ = "risk_decisions"
    __table_args__ = (
        CheckConstraint("approved = (cardinality(reasons) = 0)", name="approved_iff_no_reasons"),
        CheckConstraint(
            f"reasons <@ ARRAY[{', '.join(repr(r.value) for r in RiskReason)}]::text[]",
            name="known_reasons",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    intent_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("orders.id"), unique=True)
    approved: Mapped[bool] = mapped_column(Boolean)
    reasons: Mapped[list[str]] = mapped_column(ARRAY(Text))
    policy_version: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[datetime] = mapped_column(Timestamp)
    observations: Mapped[dict[str, Any]] = mapped_column(JSONB)


class KillSwitchRow(Base):
    """Current kill-switch state, one row per scope. History is in kill_switch_events."""

    __tablename__ = "kill_switch"
    __table_args__ = (CheckConstraint(_in("state", KillSwitchState), name="state"),)

    scope: Mapped[str] = mapped_column(Text, primary_key=True)
    state: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(Timestamp)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


class KillSwitchEventRow(Base):
    """Append-only (UPDATE/DELETE blocked by trigger)."""

    __tablename__ = "kill_switch_events"
    __table_args__ = (
        CheckConstraint(
            f"from_state IS NULL OR {_in('from_state', KillSwitchState)}", name="from_state"
        ),
        CheckConstraint(_in("to_state", KillSwitchState), name="to_state"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    scope: Mapped[str] = mapped_column(Text, ForeignKey("kill_switch.scope"))
    from_state: Mapped[str | None] = mapped_column(Text)
    to_state: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(Timestamp)
    actor: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)


class SystemEventRow(Base):
    """Append-only (UPDATE/DELETE blocked by trigger)."""

    __tablename__ = "system_events"
    __table_args__ = (
        CheckConstraint(_in("severity", Severity), name="severity"),
        Index(None, "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    event_type: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(Text)
    component: Mapped[str] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(Timestamp)
    correlation_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


APPEND_ONLY_TABLES = (
    OrderEventRow.__tablename__,
    FillRow.__tablename__,
    RiskDecisionRow.__tablename__,
    KillSwitchEventRow.__tablename__,
    SystemEventRow.__tablename__,
)
