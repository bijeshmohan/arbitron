from pathlib import Path

import pytest

from arbitron import __version__
from arbitron.cli import EXIT_DATABASE_ERROR, EXIT_INVALID_CONFIG, EXIT_OK, main

EXAMPLE_PAPER = Path(__file__).parents[2] / "configs" / "example.paper.toml"


@pytest.fixture(autouse=True)
def no_ambient_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Isolate from the developer's environment and any local .env file.
    monkeypatch.chdir(tmp_path)
    for name in ("KEY", "SECRET", "PASSWORD"):
        monkeypatch.delenv(f"ARBITRON_EXCHANGE_API_{name}", raising=False)


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    assert "check-config" in capsys.readouterr().out


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["--version"])

    assert __version__ in capsys.readouterr().out


def test_command_is_required() -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([])

    assert exc_info.value.code == 2


def test_check_config_accepts_example(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", str(EXAMPLE_PAPER)]) == EXIT_OK

    out = capsys.readouterr().out
    assert "config OK" in out
    assert "mode:        paper" in out
    assert "credentials: absent" in out


def test_check_config_reports_invalid_field(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE_PAPER.read_text().replace('timezone = "UTC"', 'timezone = "EST"'))

    assert main(["check-config", str(bad)]) == EXIT_INVALID_CONFIG

    err = capsys.readouterr().err
    assert "app.timezone" in err


def test_check_config_reports_malformed_toml(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text("[app\nmode = ")

    assert main(["check-config", str(bad)]) == EXIT_INVALID_CONFIG
    assert "not valid TOML" in capsys.readouterr().err


def test_check_config_reports_missing_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check-config", str(tmp_path / "missing.toml")]) == EXIT_INVALID_CONFIG
    assert "cannot read" in capsys.readouterr().err


def test_check_config_never_prints_secret_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_KEY", "key-value-123")
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_SECRET", "secret-value-456")

    assert main(["check-config", str(EXAMPLE_PAPER)]) == EXIT_OK

    captured = capsys.readouterr()
    assert "credentials: present" in captured.out
    assert "key-value-123" not in captured.out + captured.err
    assert "secret-value-456" not in captured.out + captured.err


@pytest.mark.parametrize("subcommand", ["upgrade", "status"])
def test_db_commands_require_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], subcommand: str
) -> None:
    monkeypatch.delenv("ARBITRON_DATABASE_URL", raising=False)

    assert main(["db", subcommand]) == EXIT_DATABASE_ERROR
    assert "ARBITRON_DATABASE_URL is not set" in capsys.readouterr().err


def test_db_status_never_prints_the_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Port 1 refuses connections immediately.
    monkeypatch.setenv(
        "ARBITRON_DATABASE_URL", "postgresql+psycopg://user:hunter2-secret@127.0.0.1:1/db"
    )

    assert main(["db", "status"]) == EXIT_DATABASE_ERROR
    captured = capsys.readouterr()
    assert "schema check failed" in captured.err
    assert "hunter2-secret" not in captured.out + captured.err


def test_market_sync_rejects_invalid_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["market", "sync", str(tmp_path / "missing.toml")]) == EXIT_INVALID_CONFIG
    assert "cannot read" in capsys.readouterr().err


def test_market_sync_requires_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ARBITRON_DATABASE_URL", raising=False)

    assert main(["market", "sync", str(EXAMPLE_PAPER)]) == EXIT_DATABASE_ERROR
    assert "ARBITRON_DATABASE_URL is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('version = "1"', 'version = "2"'),
        ("slow = 30", "slow = 5"),
        ('id = "sma_cross"', 'id = "nope"'),
    ],
)
def test_check_config_rejects_bad_strategy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], replacement: str, message: str
) -> None:
    bad = tmp_path / "bad.toml"
    bad.write_text(EXAMPLE_PAPER.read_text().replace(replacement, message))

    assert main(["check-config", str(bad)]) == EXIT_INVALID_CONFIG
    assert "error:" in capsys.readouterr().err


def test_check_config_reports_strategy_details(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check-config", str(EXAMPLE_PAPER)]) == EXIT_OK

    out = capsys.readouterr().out
    assert "lookback=31" in out
    assert "strategy config hash:" in out


def test_replay_rejects_unconfigured_symbol(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBITRON_DATABASE_URL", "postgresql+psycopg://u:p@127.0.0.1:1/db")

    code = main(["strategy", "replay", str(EXAMPLE_PAPER), "--symbol", "ETH/USDT"])

    assert code == EXIT_INVALID_CONFIG
    assert "not in the configured symbols" in capsys.readouterr().err


def test_replay_rejects_bad_timestamp(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["strategy", "replay", str(EXAMPLE_PAPER), "--start", "yesterday"])
    assert "invalid ISO-8601" in capsys.readouterr().err


def test_alerts_test_via_log_channel(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["alerts", "test", str(EXAMPLE_PAPER)]) == EXIT_OK
    assert "test alert sent via log" in capsys.readouterr().out


def _telegram_config(tmp_path: Path) -> Path:
    path = tmp_path / "telegram.toml"
    path.write_text(EXAMPLE_PAPER.read_text().replace('channel = "log"', 'channel = "telegram"'))
    return path


@pytest.mark.parametrize("command", [["alerts", "test"], ["paper", "run", "--follow"]])
def test_telegram_without_credentials_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    monkeypatch.delenv("ARBITRON_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ARBITRON_TELEGRAM_CHAT_ID", raising=False)
    config = str(_telegram_config(tmp_path))
    argv = [*command[:2], config, *command[2:]]

    assert main(argv) == EXIT_INVALID_CONFIG
    assert "ARBITRON_TELEGRAM_BOT_TOKEN" in capsys.readouterr().err


def test_alerts_test_reports_telegram_failure_without_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx  # noqa: PLC0415

    from arbitron.cli import EXIT_VENUE_ERROR  # noqa: PLC0415

    token = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123456"
    monkeypatch.setenv("ARBITRON_TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("ARBITRON_TELEGRAM_CHAT_ID", "42")
    real_client = httpx.AsyncClient

    def unauthorized(**kwargs: object) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(lambda _: httpx.Response(401)))

    monkeypatch.setattr(httpx, "AsyncClient", unauthorized)

    assert main(["alerts", "test", str(_telegram_config(tmp_path))]) == EXIT_VENUE_ERROR
    captured = capsys.readouterr()
    assert "HTTP 401" in captured.err
    assert token not in captured.out + captured.err


EXAMPLE_SANDBOX = Path(__file__).parents[2] / "configs" / "example.sandbox.toml"


def test_sandbox_needs_a_sandbox_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sandbox", "check", str(EXAMPLE_PAPER)]) == EXIT_INVALID_CONFIG
    assert "needs app.mode = 'sandbox'" in capsys.readouterr().err


def test_sandbox_qualify_needs_confirmation(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sandbox", "qualify", str(EXAMPLE_SANDBOX)]) == EXIT_INVALID_CONFIG
    assert "--yes" in capsys.readouterr().err


def test_sandbox_without_credentials_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["sandbox", "check", str(EXAMPLE_SANDBOX)]) == EXIT_INVALID_CONFIG
    assert "ARBITRON_EXCHANGE_API_KEY" in capsys.readouterr().err


def test_sandbox_rejects_unconfigured_symbol(capsys: pytest.CaptureFixture[str]) -> None:
    argv = ["sandbox", "check", str(EXAMPLE_SANDBOX), "--symbol", "ETH/USDT"]
    assert main(argv) == EXIT_INVALID_CONFIG
    assert "not in exchange.symbols" in capsys.readouterr().err
