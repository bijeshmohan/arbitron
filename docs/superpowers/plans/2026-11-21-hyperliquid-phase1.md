# Hyperliquid Spot Support, Phase 1 (data, paper, backtest) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Hyperliquid spot selectable for market data, backtests and paper trading (no keys, no orders), without changing Binance behaviour.

**Architecture:** A pure `VenueProfile` registry in `arbitron.domain.venues` replaces the `SUPPORTED_VENUES` allowlist. The CCXT market-data adapter reads the profile to choose how it builds tickers and checks the clock. Config resolves venue-default fees from the profile. Authenticated trading stays Binance-only in this phase.

**Tech Stack:** Python 3.12, CCXT 4.5.85 (`ccxt.async_support.hyperliquid`), pydantic settings, SQLAlchemy/PostgreSQL, pytest, ruff, pyright (strict).

**Spec:** `docs/superpowers/specs/2026-11-15-hyperliquid-design.md` (phase 1 of section 7). Phases 2 and 3 get their own plans.

## Refinements to the spec, found while planning (2026-11-21)

Measured against the live public API:

1. **Ticker from the order book, not CCXT's emulated ticker.** CCXT's emulated `fetchTicker` returns `bid=None`, `ask=None` (only `last`), and sizing and paper fills need bid and ask. The Hyperliquid profile builds the ticker from the top of `fetch_order_book`.
2. **Clock check from the order-book timestamp.** The `l2Book` response carries the venue's time (measured 42 ms from local mid-request time). That is a direct venue clock, so the existing `max_clock_skew_ms` (default 1000) applies unchanged; the spec's "looser tolerance from candle times" is not needed.
3. **History cap confirmed:** a 1h request from 400 days back returned exactly 5,000 candles, starting 2026-04-26.

## Global Constraints

- Work only in the worktree `/home/bijesh/projects/arbitron-hyperliquid` on branch `feature/hyperliquid`.
- Any local process must not bind port 9108 (the deployed app's health port on this host); local sessions use 9118.
- Never run `docker compose -f deploy/compose.yaml` from the worktree, never touch the `arbitron-*` containers or `deploy/arbitron.toml` (a forward paper test runs there until at least 2026-12-12).
- Manual verification uses the dev PostgreSQL (`deploy-postgres-1`, `localhost:5432`) with a **separate database `arbitron_hl`**, never the `arbitron` database.
- Spot only, long-only. No perpetuals, no leverage, no authenticated Hyperliquid calls, no order placement in this phase.
- Binance behaviour must not change: the full existing test suite passes with no edits to existing assertions, except where a task names the change.
- Hyperliquid default fees: taker `0.07` %, maker `0.04` % (CCXT base tier, spec section 2). Binance default: `0.1` %.
- Markets: `BTC/USDC` and `ETH/USDC` (operator decision 2026-11-21).
- Checks before every commit: `uv run ruff format --check .`, `uv run ruff check .`, `uv run pyright`, `uv run pytest -m "not integration" -q`.
- Commits are local on `feature/hyperliquid`; the branch is pushed only when the operator asks.

## Review Focus

1. **Empty book side.** An order book with no asks (or no bids) must give a `Ticker` with `ask=None` (or `bid=None`), not an `IndexError`; downstream sizing then refuses the entry as it already does for a missing quote. Test in Task 4.
2. **Backfill before the venue's history.** `market backfill` with a start earlier than Hyperliquid's ~5,000-candle cap must store what exists and report the gap from the start to the first candle, without crashing or looping. Test in Task 6.
3. **Authenticated use attempted.** A `sandbox`-mode config with `exchange.id = "hyperliquid"` must be refused by `create_trading` with "authenticated trading is not implemented for hyperliquid in this build", before any secret is read. Test in Task 3.
4. **Unknown venue id.** `exchange.id = "kraken"` must fail config validation with a message listing the supported venues. Test in Task 2.
5. **Explicit zero fee.** `fee_pct = "0"` in `[paper]` or `[backtest]` must give 0, not the venue default (a falsy-value bug). Test in Task 2.

---

### Task 1: Venue profiles and the 128-bit client ID codec

**Files:**
- Create: `src/arbitron/domain/venues.py`
- Modify: `src/arbitron/domain/ids.py` (add the codec next to the existing ID code)
- Test: `tests/unit/test_venues.py`

**Interfaces:**
- Produces:
  - `class TickerSource(StrEnum)`: `TICKER = "ticker"`, `ORDER_BOOK = "order_book"`.
  - `class ClockSource(StrEnum)`: `SERVER_TIME = "server_time"`, `ORDER_BOOK = "order_book"`.
  - `class ClientIdScheme(StrEnum)`: `PLAIN = "plain"`, `HEX128 = "hex128"`.
  - `@dataclass(frozen=True, slots=True) class VenueProfile`: `venue_id: str`, `market_data_capabilities: tuple[str, ...]` (names of `VenueCapabilities` fields), `ticker_source: TickerSource`, `clock_source: ClockSource`, `client_id_scheme: ClientIdScheme`, `default_taker_fee_pct: Decimal`, `default_maker_fee_pct: Decimal`, `supports_trading: bool`.
  - `VENUES: Mapping[str, VenueProfile]` with `"binance"` (`("fetch_ohlcv", "fetch_ticker", "fetch_time")`, TICKER, SERVER_TIME, PLAIN, `Decimal("0.1")`, `Decimal("0.1")`, `True`) and `"hyperliquid"` (`("fetch_ohlcv", "fetch_order_book")`, ORDER_BOOK, ORDER_BOOK, HEX128, `Decimal("0.07")`, `Decimal("0.04")`, `False`).
  - `class UnknownVenueError(DomainValidationError)`; `def venue_profile(venue_id: str) -> VenueProfile` (message: `exchange 'x' is not supported (supported: ['binance', 'hyperliquid'])`).
  - In `ids.py`: `def to_hex128_client_id(client_order_id: str) -> str` and `def from_hex128_client_id(value: str) -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
def test_hex128_round_trip() -> None:
    cid = client_order_id_for(uuid4())     # existing generator in ids.py
    venue = to_hex128_client_id(cid)
    assert venue == "0x000" + cid[3:]
    assert len(venue) == 34
    assert from_hex128_client_id(venue) == cid

@pytest.mark.parametrize("value", [
    "0x" + "1" * 32,           # not our prefix: foreign order
    "0x000" + "a" * 28,        # too short
    "0x000" + "A" * 29,        # upper case is not canonical
    "000" + "a" * 31,          # no 0x
    "",
])
def test_foreign_or_malformed_venue_ids_decode_to_none(value: str) -> None:
    assert from_hex128_client_id(value) is None

def test_encode_rejects_a_non_canonical_id() -> None:
    with pytest.raises(DomainValidationError):
        to_hex128_client_id("xyz" + "a" * 29)

def test_known_venues_and_their_defaults() -> None:
    assert sorted(VENUES) == ["binance", "hyperliquid"]
    hl = venue_profile("hyperliquid")
    assert (hl.default_taker_fee_pct, hl.default_maker_fee_pct) == (Decimal("0.07"), Decimal("0.04"))
    assert hl.ticker_source is TickerSource.ORDER_BOOK and hl.clock_source is ClockSource.ORDER_BOOK
    assert hl.supports_trading is False
    assert venue_profile("binance").default_taker_fee_pct == Decimal("0.1")

def test_unknown_venue_lists_the_supported_ones() -> None:
    with pytest.raises(UnknownVenueError, match=r"supported: \['binance', 'hyperliquid'\]"):
        venue_profile("kraken")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_venues.py -q`
Expected: FAIL with `ModuleNotFoundError: arbitron.domain.venues`.

- [ ] **Step 3: Implement `venues.py` and the two codec functions**

`to_hex128_client_id` validates its input with `is_canonical_client_order_id` and raises `DomainValidationError` otherwise. `from_hex128_client_id` returns `"arb" + value[5:]` only when `value` matches `^0x000[0-9a-f]{29}$`, else `None`. `venues.py` imports only stdlib and `arbitron.domain` (the boundary test enforces it).

- [ ] **Step 4: Run the tests and the boundary test**

Run: `uv run pytest tests/unit/test_venues.py tests/unit/test_boundaries.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/arbitron/domain/venues.py src/arbitron/domain/ids.py tests/unit/test_venues.py
git commit -m "Add venue profiles and the 128-bit client order ID codec"
```

---

### Task 2: Config validates the venue and resolves venue-default fees

**Files:**
- Modify: `src/arbitron/config/settings.py` (`ExchangeSection.id`, `PaperSection.fee_pct`, `BacktestSection.fee_pct`, `ArbitronConfig`)
- Modify: `src/arbitron/application/paper_session.py:121`, `src/arbitron/application/backtesting.py:143` (read the effective fee)
- Modify: `src/arbitron/cli.py` (`check-config` prints `venue fees: taker X%, maker Y%` and the effective paper/backtest fee)
- Test: `tests/unit/test_config.py` (add cases)

**Interfaces:**
- Consumes: `venue_profile`, `UnknownVenueError` (Task 1).
- Produces:
  - `PaperSection.fee_pct: NonNegativeDecimal | None = Field(default=None, le=5)` and the same on `BacktestSection`.
  - `ArbitronConfig.venue -> VenueProfile` (property).
  - `ArbitronConfig.paper_fee_pct -> Decimal` and `ArbitronConfig.backtest_fee_pct -> Decimal` (properties): the configured value when it is not `None`, else `venue.default_taker_fee_pct`.
  - `ExchangeSection` gains a field validator that calls `venue_profile(id)` and turns `UnknownVenueError` into a validation error with the same message.

- [ ] **Step 1: Write the failing tests**

```python
def test_binance_fee_default_is_unchanged(minimal_paper_config) -> None:      # existing fixture/helper style
    config = minimal_paper_config()                       # no fee_pct anywhere
    assert config.paper_fee_pct == Decimal("0.1")
    assert config.backtest_fee_pct == Decimal("0.1")

def test_hyperliquid_uses_its_venue_default_fee(minimal_paper_config) -> None:
    config = minimal_paper_config(exchange={"id": "hyperliquid", "symbols": ["BTC/USDC"]})
    assert config.paper_fee_pct == Decimal("0.07")
    assert config.backtest_fee_pct == Decimal("0.07")

@pytest.mark.parametrize("section", ["paper", "backtest"])
def test_explicit_zero_fee_wins_over_the_default(minimal_paper_config, section: str) -> None:
    config = minimal_paper_config(exchange={"id": "hyperliquid", "symbols": ["BTC/USDC"]},
                                  **{section: {"fee_pct": "0"}})
    assert getattr(config, f"{section}_fee_pct") == Decimal(0)

def test_unknown_exchange_is_a_config_error(minimal_paper_config) -> None:
    with pytest.raises(ValidationError, match=r"supported: \['binance', 'hyperliquid'\]"):
        minimal_paper_config(exchange={"id": "kraken"})
```

Use whatever config-building helper `tests/unit/test_config.py` already uses; the names above are placeholders for it, the assertions are the requirement.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_config.py -q`
Expected: the four new tests FAIL (`paper_fee_pct` missing; kraken accepted).

- [ ] **Step 3: Implement the fields, validator and properties; switch the two call sites to `config.paper_fee_pct` / `config.backtest_fee_pct`; extend `check-config` output**

The backtest record's `fee_pct` assumption keeps reporting the effective value (it is derived from `fee_rate`, so no change there).

- [ ] **Step 4: Run the unit suite**

Run: `uv run pytest -m "not integration" -q`
Expected: PASS, including every existing config, paper and backtest test.

- [ ] **Step 5: Commit**

```bash
git add src/arbitron/config/settings.py src/arbitron/application/paper_session.py src/arbitron/application/backtesting.py src/arbitron/cli.py tests/unit/test_config.py
git commit -m "Validate the venue in config and resolve venue-default fees"
```

---

### Task 3: Exchange factory uses the profile; trading refuses Hyperliquid

**Files:**
- Modify: `src/arbitron/exchange/capabilities.py` (remove `SUPPORTED_VENUES`; add `fetch_order_book`; profile-driven `require_market_data`)
- Modify: `src/arbitron/exchange/ccxt_adapter.py` (`_VENUE_OPTIONS`, `_VENUE_CLASSES`, `build_exchange`, `create_market_data`)
- Modify: `src/arbitron/exchange/ccxt_trading.py` (`create_trading` guard)
- Modify: `typings/ccxt/async_support/__init__.pyi` (add `class hyperliquid(Exchange)`, `fetch_order_book`)
- Test: `tests/unit/test_exchange_units.py`, `tests/contract/test_binance_trading.py` (one new test)

**Interfaces:**
- Consumes: `VENUES`, `venue_profile`, `VenueProfile` (Task 1); `config.venue` (Task 2).
- Produces:
  - `VenueCapabilities.fetch_order_book: bool` (native `fetchOrderBook`).
  - `VenueCapabilities.require_market_data(self, profile: VenueProfile) -> None`: `self.require(*profile.market_data_capabilities)`.
  - `_VENUE_OPTIONS["hyperliquid"] = {"fetchMarkets": {"types": ["spot"]}}`; `_VENUE_CLASSES["hyperliquid"] = ccxt_async.hyperliquid`.
  - `create_trading` raises `UnsupportedCapability("authenticated trading is not implemented for hyperliquid in this build")` when `not config.venue.supports_trading`, as its **first** check after the mode check.

- [ ] **Step 1: Write the failing tests**

```python
def test_hyperliquid_market_data_needs_order_book_not_ticker_or_time() -> None:
    caps = VenueCapabilities.from_ccxt_has({"fetchOHLCV": True, "fetchOrderBook": True,
                                            "fetchTicker": "emulated"})
    caps.require_market_data(venue_profile("hyperliquid"))          # no raise
    with pytest.raises(UnsupportedCapability, match="fetch_ticker, fetch_time"):
        caps.require_market_data(venue_profile("binance"))

def test_build_exchange_makes_a_spot_only_hyperliquid_instance(hl_config) -> None:
    exchange = build_exchange(hl_config)
    assert isinstance(exchange, ccxt_async.hyperliquid)
    assert exchange.options["fetchMarkets"] == {"types": ["spot"]}

def test_create_trading_refuses_hyperliquid_before_reading_secrets(hl_sandbox_config) -> None:
    secrets = Mock(spec=Secrets)                      # any attribute access would show up
    with pytest.raises(UnsupportedCapability, match="not implemented for hyperliquid"):
        create_trading(hl_sandbox_config, ManualClock(T0), secrets=secrets)
    assert secrets.mock_calls == []
```

`hl_config` / `hl_sandbox_config`: paper and sandbox configs with `exchange.id = "hyperliquid"`, `symbols = ["BTC/USDC"]`, built with the existing test config helpers.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_exchange_units.py tests/contract/test_binance_trading.py -q`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement**

`create_market_data` calls `adapter.capabilities.require_market_data(config.venue)`. `build_exchange` relies on config validation for unknown ids (keep a defensive `KeyError` → `UnsupportedCapability`).

- [ ] **Step 4: Run the unit and contract suites, and pyright**

Run: `uv run pytest -m "not integration" -q && uv run pyright`
Expected: PASS, 0 errors. Existing Binance contract tests unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/arbitron/exchange typings tests/unit/test_exchange_units.py tests/contract/test_binance_trading.py
git commit -m "Build venue instances from profiles; refuse authenticated Hyperliquid access"
```

---

### Task 4: Hyperliquid market data: order-book ticker and clock, with recorded fixtures

**Files:**
- Create: `tests/fixtures/hyperliquid/record.py` (one-off recorder, run by hand; not collected by pytest)
- Create: `tests/fixtures/hyperliquid/*.json` (recorded public `/info` responses)
- Modify: `tests/contract/conftest.py` (body-keyed routing for Hyperliquid)
- Modify: `src/arbitron/exchange/mapping.py` (add `ticker_from_order_book`)
- Modify: `src/arbitron/exchange/ccxt_adapter.py` (`CcxtMarketData` takes the profile; `fetch_ticker`, `fetch_time` branch on it)
- Test: `tests/contract/test_hyperliquid_market_data.py`, `tests/unit/test_order_mapping.py` (or the existing mapping test module)

**Interfaces:**
- Consumes: `VenueProfile`, `TickerSource`, `ClockSource` (Task 1); `build_exchange`, `create_market_data` (Task 3).
- Produces:
  - `ticker_from_order_book(raw: Mapping[str, object], *, exchange_id: str, symbol: str) -> Ticker`: `bid` = first bid price or `None`, `ask` = first ask price or `None`, `last=None`, `timestamp` = book `timestamp` (missing → `InvalidVenueData`).
  - `CcxtMarketData.__init__(..., profile: VenueProfile)`. `fetch_ticker` uses `fetch_order_book(symbol, 1)` when `profile.ticker_source is ORDER_BOOK`. `fetch_time` uses the book timestamp of `self._symbols[0]` when `profile.clock_source is ORDER_BOOK`. Binance paths unchanged.
  - `hyperliquid_with_transport(options) -> tuple[ccxt_async.Exchange, CannedTransport]` in `tests/contract/conftest.py`; `CannedTransport` accepts an optional `body_key` so Hyperliquid's `POST /info` routes by the JSON body's `"type"` (and `"coin"` for `l2Book`/`candleSnapshot`).

- [ ] **Step 1: Record the fixtures**

`record.py` wraps a CCXT `hyperliquid` instance's `fetch` to save each `/info` response to `tests/fixtures/hyperliquid/<type>[_<coin>].json`, then calls `load_markets()`, `fetch_order_book("BTC/USDC", 1)`, `fetch_ohlcv("BTC/USDC", "4h", limit=10)`. Run it once:

Run: `uv run python tests/fixtures/hyperliquid/record.py`
Expected: JSON files for the meta/spot-meta calls, `l2Book_@142` (or whichever coin id CCXT requests for BTC/USDC) and `candleSnapshot_@142`. Public data only; no address or key appears in any file (grep for `0x` addresses before committing). Also hand-write one `l2Book` fixture with `"levels": [[...bids...], []]` (no asks).

- [ ] **Step 2: Write the failing tests**

```python
async def test_markets_load_btc_and_eth_usdc_as_spot(hl_market_data) -> None:
    markets = await hl_market_data.load_markets()
    btc = markets["BTC/USDC"]
    assert (btc.base, btc.quote, btc.min_cost) == ("BTC", "USDC", Decimal(10))

async def test_ticker_is_the_top_of_the_book(hl_market_data) -> None:
    ticker = await hl_market_data.fetch_ticker("BTC/USDC")
    assert ticker.bid is not None and ticker.ask is not None and ticker.bid < ticker.ask
    assert ticker.timestamp == datetime_from_ms(load_hl_fixture("l2Book_@142.json")["time"], "t")

async def test_empty_ask_side_gives_no_ask_not_a_crash(hl_market_data, transport) -> None:
    transport.use("l2Book", "l2book_no_asks.json")
    ticker = await hl_market_data.fetch_ticker("BTC/USDC")
    assert ticker.ask is None and ticker.bid is not None

async def test_clock_comes_from_the_order_book_timestamp(hl_market_data) -> None:
    assert await hl_market_data.fetch_time() == datetime_from_ms(load_hl_fixture("l2Book_@142.json")["time"], "t")

async def test_candles_are_stored_under_hyperliquid(hl_market_data) -> None:
    candles = await hl_market_data.fetch_ohlcv("BTC/USDC", Timeframe("4h"), limit=10)
    assert candles and {c.exchange_id for c in candles} == {"hyperliquid"}

def test_order_book_without_timestamp_is_invalid() -> None:
    with pytest.raises(InvalidVenueData):
        ticker_from_order_book({"bids": [[1, 1]], "asks": [[2, 1]], "timestamp": None},
                               exchange_id="hyperliquid", symbol="BTC/USDC")
```

- [ ] **Step 3: Run them to verify they fail**

Run: `uv run pytest tests/contract/test_hyperliquid_market_data.py -q`
Expected: FAIL (`fetch_ticker` returns no bid/ask; `fetch_time` raises because CCXT has no `fetchTime`).

- [ ] **Step 4: Implement `ticker_from_order_book` and the profile branches**

- [ ] **Step 5: Run unit and contract suites and pyright**

Run: `uv run pytest -m "not integration" -q && uv run pyright`
Expected: PASS, 0 errors; all Binance contract tests unchanged.

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/hyperliquid tests/contract src/arbitron/exchange
git commit -m "Hyperliquid market data: order-book ticker and venue clock"
```

---

### Task 5: Opt-in live check against Hyperliquid's public API

**Files:**
- Create: `tests/live/test_hyperliquid_public.py` (same opt-in pattern as `tests/live/test_binance_public.py`, `ARBITRON_LIVE_EXCHANGE_TESTS=1`)

**Interfaces:**
- Consumes: `create_market_data` (Task 3), profile branches (Task 4).

- [ ] **Step 1: Write the test**

```python
async def test_live_hyperliquid_market_data_is_usable() -> None:
    md = create_market_data(hl_config(symbols=["BTC/USDC", "ETH/USDC"]), SystemClock())
    try:
        markets = await md.load_markets()
        assert {"BTC/USDC", "ETH/USDC"} <= set(markets)
        ticker = await md.fetch_ticker("BTC/USDC")
        assert ticker.bid and ticker.ask and ticker.bid < ticker.ask
        offset = await measure_clock_offset(md, SystemClock())
        assert abs(offset) < timedelta(seconds=1)
        candles = await md.fetch_ohlcv("ETH/USDC", Timeframe("4h"), limit=50)
        assert len(candles) == 50
    finally:
        await md.close()
```

- [ ] **Step 2: Run it**

Run: `ARBITRON_LIVE_EXCHANGE_TESTS=1 uv run pytest tests/live/test_hyperliquid_public.py -q`
Expected: PASS. Without the variable: SKIPPED.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_hyperliquid_public.py
git commit -m "Add an opt-in live check of Hyperliquid public market data"
```

---

### Task 6: Backfill, backtest and paper on Hyperliquid, with docs and an example config

**Files:**
- Create: `configs/example.hyperliquid.toml` (paper mode, `exchange.id = "hyperliquid"`, `symbols = ["BTC/USDC", "ETH/USDC"]`, `sandbox = false`, `timeframe = "4h"`, `sma_cross` fixture strategy with the existing integration-fixture comment, `sizing.assumed_fee_pct = "0.07"`, no `fee_pct` in `[paper]`/`[backtest]` so the venue default applies)
- Create: `docs/hyperliquid.md` (venue notes: spot only; USDC quote; BTC and ETH are Unit-bridged wrapped assets; ~5,000-candle history cap; fees 0.07/0.04; testnet is for order flow only; trading not yet supported; the phase 1 verification results from Task 7)
- Modify: `README.md` (one short section linking `docs/hyperliquid.md`)
- Test: `tests/unit/test_config.py` (example config parses), `tests/integration/test_hyperliquid_data.py`

**Interfaces:**
- Consumes: everything above; existing `CandleCollector`, backtest service and integration-test fixtures (follow the style of the existing backtest/collector integration tests).

- [ ] **Step 1: Write the failing tests**

```python
def test_example_hyperliquid_config_is_valid() -> None:
    config = load_config(Path("configs/example.hyperliquid.toml"))
    assert config.exchange.id == "hyperliquid"
    assert config.paper_fee_pct == Decimal("0.07")

async def test_venues_keep_separate_candle_histories(uow_factory) -> None:
    # same symbol string, same open_time, two venues: both rows stored, each query sees one
    ...store a binance and a hyperliquid BTC/USDC 4h candle at T0...
    assert [c.exchange_id for c in await load("hyperliquid", "BTC/USDC")] == ["hyperliquid"]

async def test_backfill_before_venue_history_reports_the_leading_gap(uow_factory) -> None:
    # fake MarketDataPort returns candles starting 30 days after `start`, then none
    result = await collector.backfill("BTC/USDC", start=T0, end=T0 + timedelta(days=60))
    assert result.written == <candles in the last 30 days>
    assert result.gaps[0].start == T0                 # reported, not raised
```

Adapt the names to the collector's actual result type; the requirement is: rows written, leading gap reported, no exception, the call returns.

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/unit/test_config.py tests/integration/test_hyperliquid_data.py -q`
Expected: FAIL (example file missing; any collector behaviour that raises on the leading gap shows here).

- [ ] **Step 3: Write the example config and docs; fix the collector only if the leading-gap test shows it raises or loops**

- [ ] **Step 4: Run the full suite including integration**

Run: `uv run pytest -q`
Expected: PASS (integration uses testcontainers; nothing touches the deployed stack).

- [ ] **Step 5: Commit**

```bash
git add configs/example.hyperliquid.toml docs/hyperliquid.md README.md tests
git commit -m "Hyperliquid example config, docs, and data-separation tests"
```

---

### Task 7: Manual verification against real data (no commit of results except the doc)

**Files:**
- Modify: `docs/hyperliquid.md` (append the verification results)

- [ ] **Step 1: Prepare a separate database**

Run: `docker exec deploy-postgres-1 psql -U arbitron -d arbitron -c "create database arbitron_hl"` then, with `ARBITRON_DATABASE_URL=postgresql+psycopg://arbitron:arbitron@localhost:5432/arbitron_hl`, `uv run arbitron db upgrade`.
Expected: `database schema upgraded to head`.

- [ ] **Step 2: Backfill**

Copy the example to `configs/hyperliquid.toml` (gitignored), set `[monitoring] http_port = 9118` (the deployed app holds 127.0.0.1:9108 on this host), and run `uv run arbitron market backfill configs/hyperliquid.toml --start 2024-01-01T00:00:00Z --end <today>T00:00:00Z`.
Expected: both symbols written; a leading gap reported up to the venue's first available 4h candle (about 5,000 candles back); no crash.

- [ ] **Step 3: Backtest**

Run: `uv run arbitron backtest run configs/hyperliquid.toml --start <first candle> --end <today>T00:00:00Z`
Expected: a report whose assumptions show `fee pct: 0.07`. This exercises plumbing only; its return is not evaluated (no strategy decision in this work).

- [ ] **Step 4: Paper session, 30 minutes, then stop it**

Run (with a 5m timeframe override in the local config so cycles happen): `uv run arbitron paper run configs/hyperliquid.toml --follow`, stop with Ctrl-C after 30 minutes.
Expected: startup reconciliation clean, `market_data_ok: true`, cycles every 5 minutes, no errors.

- [ ] **Step 5: Compare paper quotes with the live book**

For 10 cycles, compare the ticker the session recorded with an independent `fetch_order_book` taken in the same minute; record spread and the difference in `docs/hyperliquid.md`.
Expected: identical top-of-book source, differences explained by timing only.

- [ ] **Step 6: Commit the doc**

```bash
git add docs/hyperliquid.md
git commit -m "Record Hyperliquid phase 1 verification"
```

- [ ] **Step 7: Stop for the operator's phase 1 review.** Report the results; do not start phase 2.
