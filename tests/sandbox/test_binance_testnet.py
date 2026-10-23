"""Order lifecycle and reconciliation against the Binance spot testnet.

See docs/sandbox-qualification.md for what is verified and what the sandbox
cannot show.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from arbitron.application.kill_switch import KillSwitchService
from arbitron.application.policies import risk_policy, sizing_policy
from arbitron.application.risk_gate import apply_risk_decision
from arbitron.application.sandbox_qualification import SandboxQualification
from arbitron.clocks import SystemClock
from arbitron.config.settings import ArbitronConfig
from arbitron.domain.enums import OrderStatus, OrderType, Side, TimeInForce
from arbitron.domain.events import OrderEvent
from arbitron.domain.market import Market
from arbitron.domain.money import round_down_to_step, round_up_to_step
from arbitron.domain.orders import Order, OrderIntent
from arbitron.domain.risk import RiskDecision
from arbitron.domain.signals import Signal
from arbitron.exchange.ccxt_adapter import CcxtMarketData
from arbitron.exchange.ccxt_trading import CcxtTrading
from arbitron.execution.order_manager import OrderManager
from arbitron.execution.reconciler import Reconciler
from tests.factories import SMA_CONFIG, make_signal
from tests.integration.conftest import UowFactory

S = OrderStatus
SYMBOL = "BTC/USDT"


async def test_order_lifecycle(
    sandbox_config: ArbitronConfig, trading: CcxtTrading, market_data: CcxtMarketData
) -> None:
    qualification = SandboxQualification(
        market_data=market_data,
        trading=trading,
        clock=SystemClock(),
        symbol=SYMBOL,
        max_clock_skew=timedelta(milliseconds=sandbox_config.market.max_clock_skew_ms),
    )

    report = await qualification.order_lifecycle()

    failures = [f"{r.name}: {r.detail}" for r in report.results if not r.ok]
    assert not failures


# --- reconciliation ------------------------------------------------------------


def unique_signal(now: datetime) -> Signal:
    """A signal (hence client order ID) never used before, on this or any earlier run."""
    candle = datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * (uuid.uuid4().int % 10**9))
    return make_signal(source_candle_time=candle, created_at=now)


async def marketable_buy(market_data: CcxtMarketData, market: Market) -> tuple[Signal, OrderIntent]:
    ticker = await market_data.fetch_ticker(SYMBOL)
    assert ticker.ask is not None
    price = round_up_to_step(ticker.ask * Decimal("1.001"), market.price_step)
    amount = round_up_to_step(market.min_cost * 2 / price, market.amount_step)
    now = datetime.now(UTC)
    signal = unique_signal(now)
    return signal, OrderIntent(
        signal_id=signal.id,
        strategy_id="sma_cross",
        exchange_id="binance",
        symbol=SYMBOL,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        amount=amount,
        limit_price=price,
        time_in_force=TimeInForce.IOC,
        created_at=now,
        sizing_policy="sandbox-test",
    )


async def stranded_unknown(uow: UowFactory, signal: Signal, intent: OrderIntent) -> Order:
    """Record the order as SUBMISSION_UNKNOWN, as a lost venue response leaves it."""
    now = intent.created_at
    order = Order.new(intent)
    async with uow() as tx:
        await tx.strategy_configs.add_if_absent(SMA_CONFIG, created_at=now)
        await tx.signals.add_if_absent(signal)
        await tx.orders.add_if_absent(order, OrderEvent(order.id, None, S.CREATED, now, "c"))
        approved = await apply_risk_decision(
            tx, order, RiskDecision(order.id, (), "t", now), correlation_id="c"
        )
        submitting = approved.transition(S.SUBMITTING, at=now)
        unknown = submitting.transition(S.SUBMISSION_UNKNOWN, at=now)
        for before, after in ((approved, submitting), (submitting, unknown)):
            await tx.orders.apply_transition(
                before, after, OrderEvent(order.id, before.status, after.status, now, "c")
            )
        await tx.commit()
    return unknown


async def reconciler_for(
    config: ArbitronConfig, uow: UowFactory, trading: CcxtTrading, markets: dict[str, Market]
) -> Reconciler:
    clock = SystemClock()
    kill_switch = KillSwitchService(uow, clock)
    await kill_switch.ensure_initialized()
    manager = OrderManager(
        execution=trading,
        uow_factory=uow,
        clock=clock,
        sizing_policy=sizing_policy(config),
        risk_policy=risk_policy(config),
        submission_timeout=timedelta(seconds=15),
    )
    return Reconciler(
        execution=trading,
        account=trading,
        order_manager=manager,
        kill_switch=kill_switch,
        uow_factory=uow,
        clock=clock,
        markets=lambda: markets,
        exchange_id="binance",
        symbols=(SYMBOL,),
        settle=timedelta(0),
        max_age=timedelta(minutes=15),
    )


async def only_order(uow: UowFactory, order_id: uuid.UUID) -> Order:
    async with uow() as tx:
        order = await tx.orders.get(order_id)
    assert order is not None
    return order


async def test_lost_response_is_recovered_by_client_id(
    sandbox_config: ArbitronConfig,
    uow: UowFactory,
    trading: CcxtTrading,
    market_data: CcxtMarketData,
) -> None:
    markets = dict(await market_data.load_markets())
    market = markets[SYMBOL]
    signal, intent = await marketable_buy(market_data, market)
    stranded = await stranded_unknown(uow, signal, intent)
    sent = await trading.submit_order(intent)  # the venue accepts; the bot never hears
    reconciler = await reconciler_for(sandbox_config, uow, trading, markets)

    report = await reconciler.run()

    order = await only_order(uow, stranded.id)
    assert order.status is S.FILLED
    assert order.exchange_order_id == sent.exchange_order_id
    assert order.filled_amount == intent.amount
    async with uow() as tx:
        fills = await tx.fills.list_for_order(order.id)
        position = await tx.positions.get("binance", SYMBOL)
    assert sum((f.trade.amount for f in fills), Decimal(0)) == intent.amount
    assert all(f.trade.trade_id for f in fills)
    assert position is not None
    assert position.quantity == intent.amount
    assert f"{stranded.id}: {S.SUBMISSION_UNKNOWN} -> {S.FILLED}" in report.resolved
    # The sandbox account holds pre-funded BTC and trades the bot did not make
    # (earlier runs); both are reported, never silently adopted. Nothing else is.
    unexpected = [
        d
        for d in report.discrepancies
        if not d.startswith(f"{SYMBOL}: venue holds") and "belongs to unknown order" not in d
    ]
    assert not unexpected
    assert not any(sent.exchange_order_id in d for d in report.discrepancies)

    ticker = await market_data.fetch_ticker(SYMBOL)
    assert ticker.bid is not None
    await trading.submit_order(  # sell it back to keep the sandbox balance stable
        OrderIntent(
            signal_id=uuid.uuid4(),
            strategy_id="sandbox-test",
            exchange_id="binance",
            symbol=SYMBOL,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            amount=intent.amount,
            limit_price=round_down_to_step(ticker.bid * Decimal("0.999"), market.price_step),
            time_in_force=TimeInForce.IOC,
            created_at=datetime.now(UTC),
            sizing_policy="sandbox-test",
        )
    )


async def test_never_sent_order_becomes_not_placed(
    sandbox_config: ArbitronConfig,
    uow: UowFactory,
    trading: CcxtTrading,
    market_data: CcxtMarketData,
) -> None:
    markets = dict(await market_data.load_markets())
    signal, intent = await marketable_buy(market_data, markets[SYMBOL])
    stranded = await stranded_unknown(uow, signal, intent)  # never reached the venue
    reconciler = await reconciler_for(sandbox_config, uow, trading, markets)

    await reconciler.run()  # first not-found lookup
    after_first = await only_order(uow, stranded.id)
    await reconciler.run()  # second consecutive miss after the settle window

    assert after_first.status is S.SUBMISSION_UNKNOWN
    assert (await only_order(uow, stranded.id)).status is S.NOT_PLACED
