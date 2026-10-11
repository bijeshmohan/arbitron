"""Validated configuration.

Non-secret configuration comes from a TOML file checked into (or alongside) the
repository; secrets come only from the environment. The two never mix: a TOML
file cannot carry credentials, and the environment cannot override trading
policy.

Validation fails closed: unknown keys, floats for financial values, internally
inconsistent limits, and any attempt to select live mode are rejected.
"""

import hashlib
import json
import re
import tomllib
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict


def _reject_float(value: object) -> object:
    # TOML floats are binary floating point; accepting them would silently
    # introduce rounding error into limits. Require strings or integers.
    if isinstance(value, float):
        msg = "financial values must be quoted strings or integers, not floats"
        raise ValueError(msg)
    return value


PositiveDecimal = Annotated[
    Decimal,
    BeforeValidator(_reject_float),
    Field(gt=0, allow_inf_nan=False),
]

_SYMBOL_RE = re.compile(r"^[A-Z0-9]+/[A-Z0-9]+$")
_TIMEFRAME_RE = re.compile(r"^[1-9][0-9]*[mhdw]$")


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    SANDBOX = "sandbox"
    LIVE = "live"


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AppSection(_Section):
    mode: Mode
    environment: Environment
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _utc_only(cls, value: str) -> str:
        if value != "UTC":
            msg = "only 'UTC' is supported; all timestamps are UTC"
            raise ValueError(msg)
        return value


class ExchangeSection(_Section):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    sandbox: bool
    spot_only: bool
    symbols: tuple[str, ...] = Field(min_length=1)
    request_timeout_ms: int = Field(ge=1_000, le=60_000)

    @field_validator("spot_only")
    @classmethod
    def _spot_required(cls, value: bool) -> bool:
        if not value:
            msg = "only spot markets are supported in this release"
            raise ValueError(msg)
        return value

    @field_validator("symbols")
    @classmethod
    def _valid_symbols(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for symbol in value:
            if not _SYMBOL_RE.match(symbol):
                msg = f"invalid unified spot symbol {symbol!r}; expected e.g. 'BTC/USDT'"
                raise ValueError(msg)
        if len(set(value)) != len(value):
            msg = "symbols must be unique"
            raise ValueError(msg)
        return value


class StrategySection(_Section):
    id: str = Field(min_length=1, pattern=r"^[a-z0-9_]+$")
    version: str = Field(min_length=1)
    timeframe: str
    enabled: bool

    @field_validator("timeframe")
    @classmethod
    def _valid_timeframe(cls, value: str) -> str:
        if not _TIMEFRAME_RE.match(value):
            msg = f"invalid timeframe {value!r}; expected e.g. '1m', '5m', '1h', '1d'"
            raise ValueError(msg)
        return value


class RiskSection(_Section):
    max_order_notional: PositiveDecimal
    max_asset_exposure: PositiveDecimal
    max_total_exposure: PositiveDecimal
    max_open_orders: int = Field(ge=1)
    max_daily_loss: PositiveDecimal
    max_drawdown_pct: PositiveDecimal = Field(le=100)
    max_market_data_age_seconds: int = Field(gt=0)

    @model_validator(mode="after")
    def _consistent_limits(self) -> Self:
        if self.max_order_notional > self.max_asset_exposure:
            msg = "max_order_notional must not exceed max_asset_exposure"
            raise ValueError(msg)
        if self.max_asset_exposure > self.max_total_exposure:
            msg = "max_asset_exposure must not exceed max_total_exposure"
            raise ValueError(msg)
        return self


class ExecutionSection(_Section):
    allow_market_orders: bool
    submission_timeout_seconds: int = Field(gt=0)
    reconciliation_interval_seconds: int = Field(gt=0)


class LiveSection(_Section):
    enabled: bool = False
    acknowledgement: bool = False


class ArbitronConfig(_Section):
    app: AppSection
    exchange: ExchangeSection
    strategy: StrategySection
    risk: RiskSection
    execution: ExecutionSection
    live: LiveSection = LiveSection()

    @model_validator(mode="after")
    def _mode_gates(self) -> Self:
        if self.app.mode is Mode.LIVE:
            # Live gates (plan §18) are not implemented yet; until they are,
            # live mode cannot be selected at all.
            msg = "live mode is not available in this build"
            raise ValueError(msg)
        if self.live.enabled or self.live.acknowledgement:
            msg = "[live] flags must be false unless app.mode is 'live'"
            raise ValueError(msg)
        if (self.app.mode is Mode.SANDBOX) != self.exchange.sandbox:
            msg = "exchange.sandbox must be true if and only if app.mode is 'sandbox'"
            raise ValueError(msg)
        return self

    def config_hash(self) -> str:
        """Stable SHA-256 of the canonical configuration, for run provenance."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def load_config(path: Path) -> ArbitronConfig:
    """Load and validate a TOML config file.

    Raises ``OSError`` if the file cannot be read, ``tomllib.TOMLDecodeError``
    on malformed TOML, and ``pydantic.ValidationError`` on invalid content.
    """
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    return ArbitronConfig.model_validate(data)


class Secrets(BaseSettings):
    """Credentials, read only from the environment (or a local ``.env``)."""

    model_config = SettingsConfigDict(
        env_prefix="ARBITRON_",
        env_file=".env",
        env_ignore_empty=True,
        extra="ignore",
        frozen=True,
    )

    exchange_api_key: SecretStr | None = None
    exchange_api_secret: SecretStr | None = None
    exchange_api_password: SecretStr | None = None
    # postgresql+psycopg://user:password@host:5432/dbname
    database_url: SecretStr | None = None

    @property
    def has_exchange_credentials(self) -> bool:
        return self.exchange_api_key is not None and self.exchange_api_secret is not None
