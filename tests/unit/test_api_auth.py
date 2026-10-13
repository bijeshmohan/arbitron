"""API token validation, file-based secrets, and the CLI refusing to serve without a token."""

from pathlib import Path

import pytest

from arbitron.api.auth import MIN_TOKEN_LENGTH, ApiTokenError, validate_token
from arbitron.application.operations import api_actor
from arbitron.cli import EXIT_INVALID_CONFIG, main
from arbitron.config.settings import Secrets
from tests.unit.test_cli import EXAMPLE_PAPER

GOOD = "a" * MIN_TOKEN_LENGTH


@pytest.mark.parametrize("token", [None, "", "a" * (MIN_TOKEN_LENGTH - 1), f"{GOOD}\n", f" {GOOD}"])
def test_weak_or_missing_tokens_are_refused(token: str | None) -> None:
    with pytest.raises(ApiTokenError):
        validate_token(token)


def test_good_token_is_accepted() -> None:
    assert validate_token(GOOD) == GOOD


@pytest.mark.parametrize("operator", ["", "a b", "x" * 65, "evil\nname", "semi;colon"])
def test_operator_names_are_restricted(operator: str) -> None:
    with pytest.raises(ValueError, match="operator"):
        api_actor(operator)


def test_operator_actor_is_prefixed() -> None:
    assert api_actor("bijesh") == "api:bijesh"


def test_secrets_can_come_from_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ARBITRON_API_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    (tmp_path / "arbitron_api_token").write_text(f"{GOOD}\n")  # editors add newlines
    monkeypatch.setenv("ARBITRON_SECRETS_DIR", str(tmp_path))

    token = Secrets().api_token

    assert token is not None
    assert token.get_secret_value() == GOOD


def test_environment_wins_over_secret_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "arbitron_api_token").write_text("from-file")
    monkeypatch.setenv("ARBITRON_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("ARBITRON_API_TOKEN", GOOD)

    token = Secrets().api_token

    assert token is not None
    assert token.get_secret_value() == GOOD


def test_no_secrets_dir_by_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARBITRON_SECRETS_DIR", raising=False)
    monkeypatch.delenv("ARBITRON_API_TOKEN", raising=False)

    assert Secrets().api_token is None


def test_paper_follow_refuses_without_api_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = EXAMPLE_PAPER.resolve()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARBITRON_API_TOKEN", raising=False)
    monkeypatch.delenv("ARBITRON_SECRETS_DIR", raising=False)

    assert main(["paper", "run", str(config), "--follow"]) == EXIT_INVALID_CONFIG
    assert "ARBITRON_API_TOKEN" in capsys.readouterr().err


def test_blank_secret_files_count_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ARBITRON_TELEGRAM_BOT_TOKEN", raising=False)
    (tmp_path / "arbitron_telegram_bot_token").write_text("\n")
    monkeypatch.setenv("ARBITRON_SECRETS_DIR", str(tmp_path))

    assert Secrets().telegram_bot_token is None


def test_code_version_uses_build_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    from arbitron import __version__  # noqa: PLC0415
    from arbitron.application.backtesting import code_version  # noqa: PLC0415

    monkeypatch.setenv("ARBITRON_BUILD_REVISION", "abc123def456")

    assert code_version() == f"{__version__}+gabc123def456"
