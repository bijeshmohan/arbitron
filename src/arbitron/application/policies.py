"""Build the pure sizing and risk policies from validated configuration."""

import hashlib
import json
from datetime import timedelta
from decimal import Decimal

from arbitron.config.settings import ArbitronConfig
from arbitron.risk.manager import RiskPolicy
from arbitron.sizing.sizer import SizingPolicy

_HUNDRED = Decimal(100)


def risk_policy_version(config: ArbitronConfig) -> str:
    """``risk-<hash>`` over every value the risk gate reads.

    Any change to a limit changes the version, so each persisted decision
    records exactly which limits it was judged against.
    """
    relevant = {
        "risk": config.risk.model_dump(mode="json"),
        "sizing": config.sizing.model_dump(mode="json"),
        "symbols": list(config.exchange.symbols),
        "allow_market_orders": config.execution.allow_market_orders,
    }
    digest = hashlib.sha256(
        json.dumps(relevant, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"risk-{digest[:16]}"


def risk_policy(config: ArbitronConfig) -> RiskPolicy:
    risk, sizing = config.risk, config.sizing
    return RiskPolicy(
        version=risk_policy_version(config),
        allowed_symbols=frozenset(config.exchange.symbols),
        max_order_notional=risk.max_order_notional,
        max_asset_exposure=risk.max_asset_exposure,
        max_total_exposure=risk.max_total_exposure,
        max_open_orders=risk.max_open_orders,
        max_orders_per_hour=risk.max_orders_per_hour,
        max_daily_loss=risk.max_daily_loss,
        max_drawdown_pct=risk.max_drawdown_pct,
        max_spread_pct=risk.max_spread_pct,
        max_slippage_pct=sizing.max_slippage_pct,
        max_ticker_age=timedelta(seconds=risk.max_ticker_age_seconds),
        min_quote_reserve=risk.min_quote_reserve,
        fee_rate=sizing.assumed_fee_pct / _HUNDRED,
        allow_market_orders=config.execution.allow_market_orders,
    )


def sizing_policy(config: ArbitronConfig) -> SizingPolicy:
    return SizingPolicy(
        entry_notional=config.sizing.entry_notional,
        max_slippage_pct=config.sizing.max_slippage_pct,
        max_ticker_age=timedelta(seconds=config.risk.max_ticker_age_seconds),
    )
