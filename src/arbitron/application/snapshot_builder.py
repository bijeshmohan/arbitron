"""Assemble the ``TradingSnapshot`` that sizing and risk read (plan §11).

Sources:

- venue (paper or live) via ports: fresh tickers and balances;
- database: marks (last closed candle closes), open orders, orders in the
  last hour, kill switch, equity history;
- the market-data monitor: market metadata; quality reports: data health.

Each build also records an ``EquitySnapshot`` (valuation per plan §11.1.1).
The daily-loss baseline is the first snapshot of the current UTC day; the
drawdown high-water mark is the highest snapshot ever. If equity cannot be
valued, nothing is recorded and both stay ``None``, so risk rejects entries.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from arbitron.domain.enums import OrderStatus
from arbitron.domain.market import Market, Ticker, Timeframe
from arbitron.domain.money import canonical_str
from arbitron.domain.portfolio import EquitySnapshot
from arbitron.domain.snapshot import TradingSnapshot
from arbitron.market.freshness import DataQualityReport
from arbitron.ports.account import AccountPort
from arbitron.ports.clock import Clock
from arbitron.ports.market_data import MarketDataPort
from arbitron.ports.repositories import UnitOfWork
from arbitron.risk.exposure import ValuationUnavailableError, valuation

UNRESOLVED_STATUSES = frozenset(
    {OrderStatus.SUBMISSION_UNKNOWN, OrderStatus.RECONCILIATION_REQUIRED}
)


def start_of_utc_day(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


class SnapshotBuilder:
    def __init__(
        self,
        *,
        exchange_id: str,
        quote_currency: str,
        symbols: Sequence[str],
        timeframe: Timeframe,
        market_data: MarketDataPort,
        account: AccountPort,
        markets: Callable[[], Mapping[str, Market]],
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        trading_enabled: bool,
    ) -> None:
        self._exchange_id = exchange_id
        self._quote = quote_currency
        self._symbols = tuple(symbols)
        self._timeframe = timeframe
        self._market_data = market_data
        self._account = account
        self._markets = markets
        self._uow_factory = uow_factory
        self._clock = clock
        self._trading_enabled = trading_enabled

    async def build(
        self,
        quality: Mapping[str, DataQualityReport],
        *,
        system_ready: bool,
        reconciliation_clean: bool = True,
    ) -> TradingSnapshot:
        """``reconciliation_clean``: the latest reconciliation run found nothing
        pending or discrepant. Unresolved orders also make it false."""
        tickers: dict[str, Ticker] = {}
        for symbol in self._symbols:
            tickers[symbol] = await self._market_data.fetch_ticker(symbol)
        balances = await self._account.fetch_balances()
        now = self._clock.now()

        async with self._uow_factory() as tx:
            marks: dict[str, Decimal] = {}
            for symbol in self._symbols:
                latest = await tx.candles.latest_closed(self._exchange_id, symbol, self._timeframe)
                if latest is not None:
                    marks[symbol] = latest.close
            open_orders = tuple(await tx.orders.list_non_terminal(self._exchange_id))
            orders_last_hour = await tx.orders.count_created_since(
                self._exchange_id, now - timedelta(hours=1)
            )
            kill_switch = await tx.kill_switch.get()

        markets = {s: m for s, m in self._markets().items() if s in self._symbols}
        snapshot = TradingSnapshot(
            as_of=now,
            quote_currency=self._quote,
            markets=markets,
            marks=marks,
            tickers=tickers,
            balances=balances,
            open_orders=open_orders,
            orders_last_hour=orders_last_hour,
            day_start_equity=None,
            peak_equity=None,
            market_data_ok={s: s in quality and quality[s].ok for s in self._symbols},
            kill_switch=kill_switch,
            trading_enabled=self._trading_enabled,
            system_ready=system_ready,
            reconciliation_clean=reconciliation_clean
            and not any(o.status in UNRESOLVED_STATUSES for o in open_orders),
            exchange_healthy=True,  # tickers and balances were just fetched successfully
        )
        return await self._with_equity_history(snapshot)

    async def _with_equity_history(self, snapshot: TradingSnapshot) -> TradingSnapshot:
        try:
            valued = valuation(snapshot)
        except ValuationUnavailableError as exc:
            return replace(snapshot, notes={**snapshot.notes, "valuation_error": str(exc)})
        components: dict[str, str] = {}
        quote = snapshot.quote_balance()
        if quote is not None:
            components[self._quote] = canonical_str(quote.total)
        for symbol, market in snapshot.markets.items():
            held = snapshot.held_quantity(symbol)
            mark = snapshot.marks.get(symbol)
            if held and mark is not None:
                components[market.base] = canonical_str(held * mark)
        record = EquitySnapshot(
            taken_at=snapshot.as_of,
            quote_currency=self._quote,
            equity=valued.equity,
            components=components,
            unvalued_assets=valued.unvalued_assets,
        )
        async with self._uow_factory() as tx:
            await tx.equity_snapshots.add(record)
            day_start = await tx.equity_snapshots.first_since(
                self._quote, start_of_utc_day(snapshot.as_of)
            )
            peak = await tx.equity_snapshots.peak(self._quote)
            await tx.commit()
        return replace(
            snapshot,
            day_start_equity=day_start.equity if day_start is not None else valued.equity,
            peak_equity=peak if peak is not None else valued.equity,
            notes={**snapshot.notes, "equity": canonical_str(valued.equity)},
        )
