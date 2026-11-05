"""Engine settings that keep the single-instance lock honest (persistence.db)."""

from typing import Any

import pytest

from arbitron.persistence import db


def test_engine_detects_dead_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_create(url: object, **kwargs: Any) -> object:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(db, "create_async_engine", fake_create)

    db.create_engine("postgresql+psycopg://u:p@localhost/x")

    args = seen["connect_args"]
    assert args["keepalives"] == 1
    # Dead after about idle + interval * count = 60 s, not the OS default of 2 h.
    assert args["keepalives_idle"] + args["keepalives_interval"] * args["keepalives_count"] <= 60
    assert args["tcp_user_timeout"] <= 60_000
    assert args["options"] == "-c timezone=UTC"


def test_only_psycopg_urls_are_accepted() -> None:
    with pytest.raises(ValueError, match=r"postgresql\+psycopg"):
        db.create_engine("postgresql://u:p@localhost/x")
