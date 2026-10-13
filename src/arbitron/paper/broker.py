"""Paper venue: a simulated exchange behind ``ExecutionPort`` and ``AccountPort``.

It behaves like an independent venue, not like part of the bot:

- Its state (balances, orders, trades, funding) lives in ``paper_*`` tables,
  separate from the bot's own order/fill/position records, and every
  submission is committed in the venue's own transaction. A crash between
  the venue accepting an order and the bot recording the response therefore
  produces exactly the ambiguity a real venue does, and reconciliation must
  resolve it the same way.
- It enforces venue rules: active market, lot step, price tick, minimum
  amount and notional, funds reserved at the limit price for BUYs, free base
  for SELLs, and unique client order IDs per account.
- Prices come from the real market-data port (Binance public ticker); fills
  come from the shared ``FillModel`` (documented in ``simulation/fill_model``).

All results are simulated. Nothing here touches a real account.
"""

import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal, localcontext

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.fills import ExchangeTrade, Fee
from arbitron.domain.market import Market
from arbitron.domain.money import FINANCIAL_CONTEXT, is_multiple_of_step
from arbitron.domain.orders import ExchangeOrder, OrderIntent
from arbitron.domain.portfolio import Balance
from arbitron.persistence.tables import (
    PaperAccountRow,
    PaperBalanceRow,
    PaperFundingRow,
    PaperOrderRow,
    PaperTradeRow,
)
from arbitron.ports.clock import Clock
from arbitron.ports.errors import (
    InsufficientFunds,
    InvalidOrder,
    MarketUnavailable,
    UnknownExchangeError,
)
from arbitron.ports.market_data import MarketDataPort
from arbitron.simulation.fill_model import FillModel, UnsupportedOrderError
from arbitron.simulation.settlement import settlement_deltas

FILLED, EXPIRED = "FILLED", "EXPIRED"


async def paper_balances(
    session_factory: async_sessionmaker[AsyncSession], account: str, *, observed_at: datetime
) -> Mapping[str, Balance]:
    """A paper account's balances (empty if the account does not exist yet)."""
    async with session_factory() as session:
        rows = await session.scalars(
            select(PaperBalanceRow).where(PaperBalanceRow.account == account)
        )
        return {
            row.asset: Balance(row.asset, row.free, row.used, row.free + row.used, observed_at)
            for row in rows
        }


class PaperBroker:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        account: str,
        exchange_id: str,
        quote_currency: str,
        market_data: MarketDataPort,
        markets: Callable[[], Mapping[str, Market]],
        fill_model: FillModel,
        clock: Clock,
    ) -> None:
        self._sessions = session_factory
        self.account = account
        self._exchange_id = exchange_id
        self._quote = quote_currency
        self._market_data = market_data
        self._markets = markets
        self._model = fill_model
        self._clock = clock

    # --- account -------------------------------------------------------------

    async def ensure_funded(self, starting_balance: Decimal) -> bool:
        """Create and fund the paper account once. Returns True if it was created now."""
        async with self._sessions() as session, session.begin():
            created = await session.scalar(
                insert(PaperAccountRow)
                .values(
                    account=self.account,
                    quote_currency=self._quote,
                    starting_balance=starting_balance,
                    created_at=self._clock.now(),
                )
                .on_conflict_do_nothing()
                .returning(PaperAccountRow.account)
            )
            if created is None:
                existing = await session.get(PaperAccountRow, self.account)
                if existing is not None and existing.quote_currency != self._quote:
                    msg = (
                        f"paper account {self.account!r} holds {existing.quote_currency}, "
                        f"config uses {self._quote}"
                    )
                    raise InvalidOrder(msg)
                return False
            session.add(
                PaperBalanceRow(
                    account=self.account, asset=self._quote, free=starting_balance, used=Decimal(0)
                )
            )
            session.add(
                PaperFundingRow(
                    account=self.account,
                    asset=self._quote,
                    amount=starting_balance,
                    timestamp=self._clock.now(),
                    note="initial paper funding",
                )
            )
        return True

    async def fetch_balances(self) -> Mapping[str, Balance]:
        return await paper_balances(self._sessions, self.account, observed_at=self._clock.now())

    # --- orders --------------------------------------------------------------

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        market = self._markets().get(intent.symbol)
        if market is None or not market.active:
            msg = f"{intent.symbol} is not tradeable"
            raise MarketUnavailable(msg)
        self._validate_filters(intent, market)
        # Price is fetched before anything is written: a failure here means
        # the venue never accepted the order.
        ticker = await self._market_data.fetch_ticker(intent.symbol)
        try:
            fill = self._model.fill(
                side=intent.side,
                order_type=intent.order_type,
                time_in_force=intent.time_in_force,
                amount=intent.amount,
                limit_price=intent.limit_price,
                ticker=ticker,
                market=market,
            )
        except UnsupportedOrderError as exc:
            raise InvalidOrder(str(exc)) from exc

        now = self._clock.now()
        venue_order_id = f"paper-{uuid.uuid4().hex[:24]}"
        async with self._sessions() as session, session.begin():
            duplicate = await session.scalar(
                select(PaperOrderRow.venue_order_id).where(
                    PaperOrderRow.account == self.account,
                    PaperOrderRow.client_order_id == intent.client_order_id,
                )
            )
            if duplicate is not None:
                msg = f"duplicate client order id {intent.client_order_id}"
                raise UnknownExchangeError(msg)
            base = await self._balance_for_update(session, market.base)
            quote = await self._balance_for_update(session, market.quote)
            self._check_funds(intent, base, quote)
            if fill.is_fill and fill.price is not None:
                base_delta, quote_delta = settlement_deltas(
                    side=intent.side, filled=fill.filled, price=fill.price, fee=fill.fee
                )
                with localcontext(FINANCIAL_CONTEXT):
                    base.free += base_delta
                    quote.free += quote_delta
            row = PaperOrderRow(
                venue_order_id=venue_order_id,
                account=self.account,
                client_order_id=intent.client_order_id,
                symbol=intent.symbol,
                side=intent.side.value,
                order_type=intent.order_type.value,
                time_in_force=intent.time_in_force.value if intent.time_in_force else None,
                amount=intent.amount,
                limit_price=intent.limit_price,
                raw_status=FILLED if fill.filled == intent.amount else EXPIRED,
                filled=fill.filled,
                average_price=fill.price,
                fee=fill.fee,
                fee_currency=fill.fee_currency,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            await session.flush()
            if fill.is_fill and fill.price is not None:
                session.add(
                    PaperTradeRow(
                        trade_id=f"pt-{uuid.uuid4().hex}",
                        venue_order_id=venue_order_id,
                        account=self.account,
                        symbol=intent.symbol,
                        side=intent.side.value,
                        amount=fill.filled,
                        price=fill.price,
                        fee=fill.fee,
                        fee_currency=fill.fee_currency,
                        timestamp=now,
                    )
                )
            # Returned only after the venue transaction commits on block exit.
            return self._to_exchange_order(row)

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        # Paper orders are IOC and final on submission; there is never
        # anything left to cancel. Report the order's final state.
        return await self.fetch_order(exchange_order_id, symbol)

    async def fetch_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        async with self._sessions() as session:
            row = await session.get(PaperOrderRow, exchange_order_id)
            if row is None or row.account != self.account or row.symbol != symbol:
                return None
            return self._to_exchange_order(row)

    async def fetch_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> ExchangeOrder | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(PaperOrderRow).where(
                    PaperOrderRow.account == self.account,
                    PaperOrderRow.client_order_id == client_order_id,
                    PaperOrderRow.symbol == symbol,
                )
            )
            return self._to_exchange_order(row) if row is not None else None

    async def fetch_open_orders(self, symbol: str | None = None) -> Sequence[ExchangeOrder]:
        return []  # IOC orders never rest on the paper book

    async def fetch_trades(
        self, symbol: str, *, since: datetime | None = None, limit: int | None = None
    ) -> Sequence[ExchangeTrade]:
        async with self._sessions() as session:
            stmt = (
                select(PaperTradeRow)
                .where(PaperTradeRow.account == self.account, PaperTradeRow.symbol == symbol)
                .order_by(PaperTradeRow.timestamp, PaperTradeRow.trade_id)
            )
            if since is not None:
                stmt = stmt.where(PaperTradeRow.timestamp >= since)
            if limit is not None:
                stmt = stmt.limit(limit)
            rows = await session.scalars(stmt)
            return [
                ExchangeTrade(
                    exchange_id=self._exchange_id,
                    trade_id=row.trade_id,
                    exchange_order_id=row.venue_order_id,
                    symbol=row.symbol,
                    side=Side(row.side),
                    amount=row.amount,
                    price=row.price,
                    fee=Fee(row.fee, row.fee_currency),
                    timestamp=row.timestamp,
                )
                for row in rows
            ]

    async def close(self) -> None:
        return None

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def _validate_filters(intent: OrderIntent, market: Market) -> None:
        price = intent.limit_price
        if intent.order_type is not OrderType.LIMIT or price is None:
            msg = "paper venue accepts limit orders only"
            raise InvalidOrder(msg)
        if intent.time_in_force is not TimeInForce.IOC:
            msg = "paper venue accepts IOC orders only"
            raise InvalidOrder(msg)
        if not is_multiple_of_step(intent.amount, market.amount_step):
            msg = f"LOT_SIZE: {intent.amount} is not a multiple of {market.amount_step}"
            raise InvalidOrder(msg)
        if not is_multiple_of_step(price, market.price_step):
            msg = f"PRICE_FILTER: {price} is not a multiple of {market.price_step}"
            raise InvalidOrder(msg)
        with localcontext(FINANCIAL_CONTEXT):
            if intent.amount < market.min_amount or intent.amount * price < market.min_cost:
                msg = "NOTIONAL: order is below the market minimums"
                raise InvalidOrder(msg)

    async def _balance_for_update(self, session: AsyncSession, asset: str) -> PaperBalanceRow:
        row = await session.scalar(
            select(PaperBalanceRow)
            .where(PaperBalanceRow.account == self.account, PaperBalanceRow.asset == asset)
            .with_for_update()
        )
        if row is None:
            row = PaperBalanceRow(
                account=self.account, asset=asset, free=Decimal(0), used=Decimal(0)
            )
            session.add(row)
        return row

    @staticmethod
    def _check_funds(intent: OrderIntent, base: PaperBalanceRow, quote: PaperBalanceRow) -> None:
        with localcontext(FINANCIAL_CONTEXT):
            if intent.side is Side.BUY:
                required = intent.amount * (intent.limit_price or Decimal(0))
                if quote.free < required:
                    msg = f"insufficient {quote.asset}: need {required}, free {quote.free}"
                    raise InsufficientFunds(msg)
            elif base.free < intent.amount:
                msg = f"insufficient {base.asset}: need {intent.amount}, free {base.free}"
                raise InsufficientFunds(msg)

    def _to_exchange_order(self, row: PaperOrderRow) -> ExchangeOrder:
        if row.raw_status == FILLED:
            status = OrderStatus.FILLED
        elif row.filled > 0:
            status = OrderStatus.PARTIALLY_FILLED_CLOSED
        else:
            status = OrderStatus.EXPIRED
        return ExchangeOrder(
            exchange_id=self._exchange_id,
            exchange_order_id=row.venue_order_id,
            client_order_id=row.client_order_id,
            symbol=row.symbol,
            side=Side(row.side),
            order_type=OrderType(row.order_type),
            status=status,
            raw_status=row.raw_status,
            amount=row.amount,
            filled=row.filled,
            remaining=row.amount - row.filled,
            average_price=row.average_price,
            fee=Fee(row.fee, row.fee_currency) if row.filled > 0 else None,
            timestamp=row.updated_at,
        )
