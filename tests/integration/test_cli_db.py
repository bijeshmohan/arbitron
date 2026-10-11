import pytest

from arbitron.cli import EXIT_OK, main


def test_db_status_reports_current_schema(
    database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ARBITRON_DATABASE_URL", database_url)

    assert main(["db", "upgrade"]) == EXIT_OK
    assert main(["db", "status"]) == EXIT_OK
    assert "schema is current" in capsys.readouterr().out
