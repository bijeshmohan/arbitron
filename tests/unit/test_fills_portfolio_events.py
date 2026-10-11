from decimal import Decimal

import pytest

from arbitron.domain.enums import OrderStatus, Severity
from arbitron.domain.errors import DomainValidationError
from arbitron.domain.events import Alert, OrderEvent, SystemEvent
from arbitron.domain.fills import Fee, Fill
from arbitron.domain.portfolio import Balance, PortfolioView
from arbitron.domain.positions import Position
from tests.factories import T0, make_intent, make_trade


class TestFills:
    def test_dedupe_key_uses_trade_id(self) -> None:
        assert make_trade().dedupe_key == "binance:trade:t-1"

    def test_fallback_dedupe_key_is_stable_across_representations(self) -> None:
        a = make_trade(trade_id=None, amount=Decimal("0.0040"))
        b = make_trade(trade_id=None, amount=Decimal("0.004"))

        assert a.dedupe_key == b.dedupe_key
        assert a.dedupe_key.startswith("binance:order:ex-1:")

    def test_fill_dedupes_like_its_trade(self) -> None:
        fill = Fill(order_id=make_intent().id, trade=make_trade())

        assert fill.dedupe_key == make_trade().dedupe_key

    @pytest.mark.parametrize("field", ["amount", "price"])
    def test_amount_and_price_must_be_positive(self, field: str) -> None:
        with pytest.raises(DomainValidationError):
            make_trade(**{field: Decimal(0)})

    def test_negative_fee_is_refused(self) -> None:
        with pytest.raises(DomainValidationError):
            Fee(Decimal("-0.1"), "USDT")


class TestPositionsAndBalances:
    def test_position_cannot_be_short(self) -> None:
        with pytest.raises(DomainValidationError, match="must not be negative"):
            Position("binance", "BTC/USDT", Decimal("-0.1"), Decimal(0), Decimal(0), T0)

    def test_flat_position_has_zero_cost(self) -> None:
        with pytest.raises(DomainValidationError, match="flat position"):
            Position("binance", "BTC/USDT", Decimal(0), Decimal(100), Decimal(0), T0)

    def test_realized_pnl_may_be_negative(self) -> None:
        position = Position("binance", "BTC/USDT", Decimal(0), Decimal(0), Decimal(-5), T0)

        assert position.is_flat

    def test_balance_total_must_equal_free_plus_used(self) -> None:
        with pytest.raises(DomainValidationError, match="!= total"):
            Balance("USDT", Decimal(10), Decimal(5), Decimal(16), T0)

    def test_portfolio_view_lookups_default_to_zero(self) -> None:
        view = PortfolioView(
            as_of=T0,
            balances={"USDT": Balance("USDT", Decimal(90), Decimal(10), Decimal(100), T0)},
            positions={
                "BTC/USDT": Position(
                    "binance", "BTC/USDT", Decimal("0.01"), Decimal(50000), Decimal(0), T0
                )
            },
        )

        assert view.free_balance("USDT") == Decimal(90)
        assert view.free_balance("BTC") == 0
        assert view.position_quantity("BTC/USDT") == Decimal("0.01")
        assert view.position_quantity("ETH/USDT") == 0

    def test_portfolio_view_rejects_mismatched_keys(self) -> None:
        with pytest.raises(DomainValidationError, match="keyed"):
            PortfolioView(
                as_of=T0,
                balances={"BTC": Balance("USDT", Decimal(1), Decimal(0), Decimal(1), T0)},
            )

    def test_portfolio_view_is_read_only(self) -> None:
        view = PortfolioView(as_of=T0)

        with pytest.raises(TypeError):
            view.balances["USDT"] = Balance(  # type: ignore[index]
                "USDT", Decimal(1), Decimal(0), Decimal(1), T0
            )


class TestEvents:
    def test_payload_accepts_scalars_and_is_read_only(self) -> None:
        event = SystemEvent(
            "candle_gap", Severity.WARNING, "market", T0, "corr-1", {"missing": 2, "ok": False}
        )

        assert event.payload["missing"] == 2
        with pytest.raises(TypeError):
            event.payload["missing"] = 3  # type: ignore[index]

    @pytest.mark.parametrize("value", [Decimal(1), 1.5, {"nested": "x"}, b"raw"])
    def test_payload_rejects_non_scalars(self, value: object) -> None:
        with pytest.raises(DomainValidationError, match="payload"):
            OrderEvent(
                make_intent().id,
                None,
                OrderStatus.CREATED,
                T0,
                "corr-1",
                {"value": value},  # type: ignore[dict-item]
            )

    def test_alert_requires_action_taken(self) -> None:
        with pytest.raises(DomainValidationError, match="action_taken"):
            Alert(Severity.CRITICAL, "t", "m", "", "r", "k", T0)
