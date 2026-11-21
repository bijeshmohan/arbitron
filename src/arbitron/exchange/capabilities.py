from collections.abc import Mapping
from dataclasses import dataclass

from arbitron.domain.venues import VenueProfile
from arbitron.ports.errors import UnsupportedCapability


@dataclass(frozen=True, slots=True)
class VenueCapabilities:
    """The subset of CCXT ``has`` flags this application relies on.

    CCXT reports ``True``, ``False``, ``"emulated"``, or ``None``. Only a real
    ``True`` counts: emulated endpoints are built from other calls and may
    behave differently under failure.
    """

    fetch_ohlcv: bool
    fetch_ticker: bool
    fetch_time: bool
    fetch_balance: bool
    fetch_order: bool
    fetch_open_orders: bool
    fetch_my_trades: bool
    fetch_order_book: bool
    create_order: bool
    cancel_order: bool

    @classmethod
    def from_ccxt_has(cls, has: Mapping[str, object]) -> "VenueCapabilities":
        def native(key: str) -> bool:
            return has.get(key) is True

        return cls(
            fetch_ohlcv=native("fetchOHLCV"),
            fetch_ticker=native("fetchTicker"),
            fetch_time=native("fetchTime"),
            fetch_balance=native("fetchBalance"),
            fetch_order=native("fetchOrder"),
            fetch_open_orders=native("fetchOpenOrders"),
            fetch_my_trades=native("fetchMyTrades"),
            fetch_order_book=native("fetchOrderBook"),
            create_order=native("createOrder"),
            cancel_order=native("cancelOrder"),
        )

    def require(self, *names: str) -> None:
        missing = [name for name in names if not getattr(self, name)]
        if missing:
            msg = f"venue lacks required capabilities: {', '.join(missing)}"
            raise UnsupportedCapability(msg)

    def require_market_data(self, profile: VenueProfile) -> None:
        self.require(*profile.market_data_capabilities)
