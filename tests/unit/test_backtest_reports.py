"""Report rendering, data fingerprints, and CLI parameter parsing."""

import json
import uuid
from dataclasses import dataclass
from datetime import timedelta

import pytest

from arbitron.application.backtesting import fingerprint
from arbitron.backtest.report import CAVEAT, sweep_table, to_json, to_markdown
from arbitron.cli import _parameter_value  # pyright: ignore[reportPrivateUsage]
from arbitron.domain.experiments import ExperimentRecord
from tests.factories import T0, make_candle


def record(**metrics: str | int | None) -> ExperimentRecord:
    return ExperimentRecord(
        id=uuid.UUID(int=1),
        created_at=T0,
        label="run",
        exchange_id="binance",
        symbols=("BTC/USDT",),
        timeframe="5m",
        start=T0,
        end=T0 + timedelta(days=1),
        strategy_id="sma_cross",
        strategy_version="1",
        config_hash="c" * 64,
        risk_policy_version="risk-x",
        code_version="0.1.0+gabc",
        data_fingerprint="f" * 64,
        candle_count=288,
        assumptions={"fill_timing": "next candle open", "slippage_bps": "5"},
        parameters={"fast": 10, "slow": 30},
        metrics={"total_return_pct": "-1.5", "trades": 4, "halted_at": None, **metrics},
    )


class TestReports:
    def test_markdown_states_assumptions_versions_and_caveat(self) -> None:
        text = to_markdown(record())

        assert CAVEAT in text
        assert "fill timing: next candle open" in text
        assert "fast=10, slow=30" in text
        assert "risk-x" in text
        assert "0.1.0+gabc" in text
        assert "| Total return % | -1.5 |" in text
        assert "Kill switch halted" not in text

    def test_markdown_flags_a_halt(self) -> None:
        text = to_markdown(
            record(halted_at="2026-01-01T12:00:00+00:00", halt_reason="max_daily_loss")
        )

        assert "Kill switch halted trading at 2026-01-01T12:00:00+00:00" in text

    def test_json_is_complete_and_parseable(self) -> None:
        data = json.loads(to_json(record()))

        assert data["strategy"]["parameters"] == {"fast": 10, "slow": 30}
        assert data["data"]["fingerprint"] == "f" * 64
        assert data["metrics"]["trades"] == 4
        assert data["caveat"] == CAVEAT

    def test_sweep_table_with_invalid_row(self) -> None:
        @dataclass
        class Row:
            parameters: str
            in_sample: ExperimentRecord | None
            out_of_sample: ExperimentRecord | None
            error: str | None = None

        table = sweep_table(
            [Row("fast=5", record(), record()), Row("fast=40", None, None, "fast >= slow")]
        )

        assert "| fast=5 | -1.5 |" in table
        assert "invalid: fast >= slow" in table
        assert "in-sample columns only" in table


class TestFingerprint:
    def test_stable_and_sensitive(self) -> None:
        candles = [
            make_candle(),
            make_candle(
                open_time=T0 + timedelta(minutes=5), ingested_at=T0 + timedelta(minutes=10)
            ),
        ]

        assert fingerprint({"BTC/USDT": candles}) == fingerprint({"BTC/USDT": list(candles)})
        changed = [
            candles[0],
            make_candle(
                open_time=T0 + timedelta(minutes=5),
                ingested_at=T0 + timedelta(minutes=10),
                close=candles[1].close + 1,
            ),
        ]
        assert fingerprint({"BTC/USDT": changed}) != fingerprint({"BTC/USDT": candles})

    def test_independent_of_decimal_exponent(self) -> None:
        from decimal import Decimal  # noqa: PLC0415

        plain = [make_candle()]
        padded = [make_candle(close=Decimal("105.000000000000000000"))]

        assert fingerprint({"BTC/USDT": plain}) == fingerprint({"BTC/USDT": padded})


@pytest.mark.parametrize(
    ("text", "value"),
    [("10", 10), ("true", True), ("False", False), ("0.5", "0.5"), ("abc", "abc")],
)
def test_parameter_values_never_become_floats(text: str, value: object) -> None:
    assert _parameter_value(text) == value
    assert not isinstance(_parameter_value(text), float)
