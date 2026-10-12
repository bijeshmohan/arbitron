from pathlib import Path

import pytest

from arbitron.cli import EXIT_OK, main


def test_db_status_reports_current_schema(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    assert main(["db", "upgrade"]) == EXIT_OK
    assert main(["db", "status"]) == EXIT_OK
    assert "schema is current" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("lookback", "fail", "exit_code", "expected"),
    [
        ("20", False, 0, "quality=OK"),
        ("500", False, 3, "incomplete_lookback"),
        ("20", True, 4, "venue request failed"),
    ],
)
def test_market_sync_end_to_end(
    database_url: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    lookback: str,
    fail: bool,
    exit_code: int,
    expected: str,
) -> None:
    from arbitron.clocks import ManualClock  # noqa: PLC0415
    from arbitron.exchange import ccxt_adapter  # noqa: PLC0415
    from arbitron.ports.errors import ExchangeUnavailable  # noqa: PLC0415
    from tests.integration.test_market_ingestion import NOW, FakeVenue, history  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    clock = ManualClock(NOW)
    venue = FakeVenue(clock, history(200))
    if fail:
        venue.fail_next = [ExchangeUnavailable("maintenance")]

    def fake_factory(_config: object, _clock: object) -> FakeVenue:
        return venue

    monkeypatch.setattr(ccxt_adapter, "create_market_data", fake_factory)
    monkeypatch.setattr("arbitron.clocks.SystemClock", lambda: clock)
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    code = main(["market", "sync", str(EXAMPLE_PAPER), "--lookback", lookback])

    captured = capsys.readouterr()
    assert code == exit_code, captured
    assert expected in captured.out + captured.err


def test_strategy_run_and_replay_end_to_end(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbitron.clocks import ManualClock  # noqa: PLC0415
    from arbitron.exchange import ccxt_adapter  # noqa: PLC0415
    from tests.integration.test_market_ingestion import NOW, FakeVenue, history  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    clock = ManualClock(NOW)
    venue = FakeVenue(clock, history(200))

    def fake_factory(_config: object, _clock: object) -> FakeVenue:
        return venue

    monkeypatch.setattr(ccxt_adapter, "create_market_data", fake_factory)
    monkeypatch.setattr("arbitron.clocks.SystemClock", lambda: clock)
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    # The example backfills 1000 candles; the fake venue has 100 closed, enough for lookback 31.
    assert main(["strategy", "run", str(EXAMPLE_PAPER)]) == EXIT_OK
    run_out = capsys.readouterr().out
    assert "quality=OK" in run_out
    assert "sma_cross v1 evaluated=1" in run_out

    assert main(["strategy", "replay", str(EXAMPLE_PAPER)]) == EXIT_OK
    replay_out = capsys.readouterr().out
    assert "replay sma_cross v1" in replay_out
    assert "nothing was written" in replay_out


def test_killswitch_cli_lifecycle(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbitron.cli import EXIT_INVALID_STATE  # noqa: PLC0415

    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    assert main(["killswitch", "status"]) == EXIT_OK
    assert "recovery_review (new orders BLOCKED)" in capsys.readouterr().out

    assert (
        main(["killswitch", "arm", "--operator", "bijesh", "--ack", "paper limits reviewed"])
        == EXIT_OK
    )
    assert "armed (new orders ALLOWED)" in capsys.readouterr().out

    assert main(["killswitch", "trip", "--operator", "bijesh", "--reason", "drill"]) == EXIT_OK
    assert "tripped (new orders BLOCKED)" in capsys.readouterr().out

    assert (
        main(["killswitch", "arm", "--operator", "bijesh", "--ack", "too soon"])
        == EXIT_INVALID_STATE
    )
    assert "not allowed" in capsys.readouterr().err

    assert (
        main(["killswitch", "review", "--operator", "bijesh", "--reason", "drill over"]) == EXIT_OK
    )
    capsys.readouterr()
    assert main(["killswitch", "arm", "--operator", "system", "--ack", "x"]) == EXIT_INVALID_STATE
    assert "named operator" in capsys.readouterr().err


def test_paper_run_and_status_end_to_end(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbitron.clocks import ManualClock  # noqa: PLC0415
    from arbitron.exchange import ccxt_adapter  # noqa: PLC0415
    from tests.integration.test_market_ingestion import NOW, FakeVenue, history  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    clock = ManualClock(NOW)
    venue = FakeVenue(clock, history(200))

    def fake_factory(_config: object, _clock: object) -> FakeVenue:
        return venue

    monkeypatch.setattr(ccxt_adapter, "create_market_data", fake_factory)
    monkeypatch.setattr("arbitron.clocks.SystemClock", lambda: clock)
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    assert main(["paper", "run", str(EXAMPLE_PAPER)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "funded with 1000 USDT" in out
    assert "equity: 1000 USDT" in out

    assert main(["paper", "status", str(EXAMPLE_PAPER)]) == EXIT_OK
    status = capsys.readouterr().out
    assert "balance USDT: total=1000" in status
    assert "kill switch: recovery_review" in status


def test_paper_run_refuses_other_modes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbitron.cli import EXIT_INVALID_CONFIG  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    sandbox = tmp_path / "sandbox.toml"
    sandbox.write_text(
        EXAMPLE_PAPER.read_text()
        .replace('mode = "paper"', 'mode = "sandbox"')
        .replace("sandbox = false", "sandbox = true")
    )

    assert main(["paper", "run", str(sandbox)]) == EXIT_INVALID_CONFIG
    assert "needs app.mode = 'paper'" in capsys.readouterr().err


def test_orders_cli_lists_and_guards_resolution(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arbitron.cli import EXIT_INVALID_STATE  # noqa: PLC0415
    from arbitron.clocks import ManualClock  # noqa: PLC0415
    from arbitron.exchange import ccxt_adapter  # noqa: PLC0415
    from tests.integration.test_market_ingestion import NOW, FakeVenue, history  # noqa: PLC0415
    from tests.unit.test_cli import EXAMPLE_PAPER  # noqa: PLC0415

    clock = ManualClock(NOW)
    venue = FakeVenue(clock, history(200))

    def fake_factory(_config: object, _clock: object) -> FakeVenue:
        return venue

    monkeypatch.setattr(ccxt_adapter, "create_market_data", fake_factory)
    monkeypatch.setattr("arbitron.clocks.SystemClock", lambda: clock)
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    assert main(["orders", "unresolved", str(EXAMPLE_PAPER)]) == EXIT_OK
    assert "no unresolved orders" in capsys.readouterr().out

    missing = "00000000-0000-0000-0000-000000000000"
    code = main(
        [
            "orders",
            "resolve",
            str(EXAMPLE_PAPER),
            missing,
            "--as",
            "not_placed",
            "--operator",
            "bijesh",
            "--note",
            "x",
        ]
    )
    assert code == EXIT_INVALID_STATE
    assert "no order" in capsys.readouterr().err
