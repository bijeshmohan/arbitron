from pathlib import Path

import pytest

from arbitron import __version__
from arbitron.cli import EXIT_INVALID_CONFIG, EXIT_OK, main

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
