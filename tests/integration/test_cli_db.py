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
