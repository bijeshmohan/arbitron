import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from arbitron.config import ArbitronConfig, Mode, Secrets, load_config

EXAMPLE_PAPER = Path(__file__).parents[2] / "configs" / "example.paper.toml"


def example_data() -> dict[str, Any]:
    with EXAMPLE_PAPER.open("rb") as fh:
        return tomllib.load(fh)


def with_override(section: str, key: str, value: object) -> dict[str, Any]:
    data = example_data()
    data[section][key] = value
    return data


def test_example_paper_config_is_valid() -> None:
    config = load_config(EXAMPLE_PAPER)

    assert config.app.mode is Mode.PAPER
    assert config.risk.max_order_notional == Decimal("50")
    assert isinstance(config.risk.max_order_notional, Decimal)
    assert config.live.enabled is False


def test_live_section_defaults_to_disabled() -> None:
    data = example_data()
    del data["live"]

    config = ArbitronConfig.model_validate(data)

    assert config.live.enabled is False
    assert config.live.acknowledgement is False


def test_live_mode_is_refused() -> None:
    data = with_override("app", "mode", "live")
    data["live"] = {"enabled": True, "acknowledgement": True}

    with pytest.raises(ValidationError, match="live mode is not available"):
        ArbitronConfig.model_validate(data)


@pytest.mark.parametrize("flag", ["enabled", "acknowledgement"])
def test_live_flags_refused_outside_live_mode(flag: str) -> None:
    data = with_override("live", flag, True)

    with pytest.raises(ValidationError, match=r"\[live\] flags must be false"):
        ArbitronConfig.model_validate(data)


def test_float_financial_value_is_refused() -> None:
    data = with_override("risk", "max_order_notional", 50.1)

    with pytest.raises(ValidationError, match="not floats"):
        ArbitronConfig.model_validate(data)


def test_integer_financial_value_is_accepted() -> None:
    config = ArbitronConfig.model_validate(with_override("risk", "max_daily_loss", 10))

    assert config.risk.max_daily_loss == Decimal(10)


@pytest.mark.parametrize("value", ["0", "-1", "NaN", "Infinity"])
def test_non_positive_or_non_finite_limit_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(with_override("risk", "max_daily_loss", value))


def test_drawdown_pct_above_100_is_refused() -> None:
    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(with_override("risk", "max_drawdown_pct", "100.01"))


def test_order_notional_above_asset_exposure_is_refused() -> None:
    data = with_override("risk", "max_order_notional", "101")

    with pytest.raises(ValidationError, match="max_order_notional must not exceed"):
        ArbitronConfig.model_validate(data)


def test_asset_exposure_above_total_exposure_is_refused() -> None:
    data = with_override("risk", "max_asset_exposure", "150")
    data["risk"]["max_total_exposure"] = "120"

    with pytest.raises(ValidationError, match="max_asset_exposure must not exceed"):
        ArbitronConfig.model_validate(data)


def test_unknown_key_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ArbitronConfig.model_validate(with_override("risk", "max_leverage", "3"))


def test_missing_risk_section_is_refused() -> None:
    data = example_data()
    del data["risk"]

    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(data)


def test_non_spot_is_refused() -> None:
    with pytest.raises(ValidationError, match="only spot markets"):
        ArbitronConfig.model_validate(with_override("exchange", "spot_only", False))


def test_non_utc_timezone_is_refused() -> None:
    with pytest.raises(ValidationError, match="only 'UTC'"):
        ArbitronConfig.model_validate(with_override("app", "timezone", "Asia/Kolkata"))


@pytest.mark.parametrize("symbols", [[], ["btc/usdt"], ["BTCUSDT"], ["BTC/USDT", "BTC/USDT"]])
def test_invalid_symbols_are_refused(symbols: list[str]) -> None:
    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(with_override("exchange", "symbols", symbols))


@pytest.mark.parametrize("timeframe", ["5", "0m", "5x", "m5"])
def test_invalid_timeframe_is_refused(timeframe: str) -> None:
    with pytest.raises(ValidationError, match="invalid timeframe"):
        ArbitronConfig.model_validate(with_override("strategy", "timeframe", timeframe))


def test_sandbox_flag_without_sandbox_mode_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"exchange\.sandbox must be true if and only if"):
        ArbitronConfig.model_validate(with_override("exchange", "sandbox", True))


def test_sandbox_mode_without_sandbox_flag_is_refused() -> None:
    with pytest.raises(ValidationError, match=r"exchange\.sandbox must be true if and only if"):
        ArbitronConfig.model_validate(with_override("app", "mode", "sandbox"))


def test_sandbox_mode_with_sandbox_flag_is_valid() -> None:
    data = with_override("app", "mode", "sandbox")
    data["exchange"]["sandbox"] = True

    assert ArbitronConfig.model_validate(data).app.mode is Mode.SANDBOX


def test_config_is_immutable() -> None:
    config = load_config(EXAMPLE_PAPER)

    with pytest.raises(ValidationError):
        config.app.mode = Mode.LIVE  # type: ignore[misc]


def test_config_hash_is_stable_and_sensitive_to_changes() -> None:
    first = load_config(EXAMPLE_PAPER)
    second = load_config(EXAMPLE_PAPER)
    changed = ArbitronConfig.model_validate(with_override("risk", "max_open_orders", 3))

    assert first.config_hash() == second.config_hash()
    assert first.config_hash() != changed.config_hash()


def test_secrets_never_appear_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_KEY", "key-value-123")
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_SECRET", "secret-value-456")

    secrets = Secrets(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert secrets.has_exchange_credentials
    assert "key-value-123" not in repr(secrets)
    assert "secret-value-456" not in str(secrets)


def test_empty_secret_env_vars_count_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_KEY", "")
    monkeypatch.setenv("ARBITRON_EXCHANGE_API_SECRET", "")

    assert not Secrets(_env_file=None).has_exchange_credentials  # pyright: ignore[reportCallIssue]


def test_strategy_parameters_are_loaded() -> None:
    config = load_config(EXAMPLE_PAPER)

    assert config.strategy.parameters == {"fast": 10, "slow": 30}


def test_strategy_parameters_default_to_empty() -> None:
    data = example_data()
    del data["strategy"]["parameters"]

    assert ArbitronConfig.model_validate(data).strategy.parameters == {}


def test_float_strategy_parameter_is_refused() -> None:
    data = example_data()
    data["strategy"]["parameters"]["fast"] = 10.5

    with pytest.raises(ValidationError, match="floats are not allowed"):
        ArbitronConfig.model_validate(data)


def test_nested_strategy_parameter_is_refused() -> None:
    data = example_data()
    data["strategy"]["parameters"]["fast"] = {"x": 1}

    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(data)


@pytest.mark.parametrize("section", ["sizing"])
def test_sizing_section_is_required(section: str) -> None:
    data = example_data()
    del data[section]

    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(data)


@pytest.mark.parametrize(
    "field",
    [
        "trading_enabled",
        "max_orders_per_hour",
        "max_spread_pct",
        "min_quote_reserve",
        "max_ticker_age_seconds",
    ],
)
def test_new_risk_limits_have_no_defaults(field: str) -> None:
    data = example_data()
    del data["risk"][field]

    with pytest.raises(ValidationError):
        ArbitronConfig.model_validate(data)


def test_entry_larger_than_max_order_is_refused() -> None:
    data = example_data()
    data["sizing"]["entry_notional"] = "51"

    with pytest.raises(ValidationError, match="every entry would be rejected"):
        ArbitronConfig.model_validate(data)


def test_symbols_must_share_one_quote() -> None:
    data = with_override("exchange", "symbols", ["BTC/USDT", "ETH/BTC"])

    with pytest.raises(ValidationError, match="one quote currency"):
        ArbitronConfig.model_validate(data)


def test_quote_currency() -> None:
    assert load_config(EXAMPLE_PAPER).exchange.quote_currency == "USDT"


def test_zero_reserve_and_fee_are_allowed() -> None:
    data = example_data()
    data["risk"]["min_quote_reserve"] = "0"
    data["sizing"]["assumed_fee_pct"] = "0"

    config = ArbitronConfig.model_validate(data)

    assert config.risk.min_quote_reserve == 0
