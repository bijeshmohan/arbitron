from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from arbitron.domain.fills import ExchangeTrade
from arbitron.domain.orders import ExchangeOrder, OrderIntent


class ExecutionPort(Protocol):
    """Order submission, cancellation, and order/trade queries.

    Implemented by the live CCXT adapter and by the paper broker. Raises
    ``arbitron.ports.errors.VenueError`` subclasses on failure; see that module
    for why submission is never retried blindly.
    """

    async def submit_order(self, intent: OrderIntent) -> ExchangeOrder:
        """Submit using ``intent.client_order_id`` where the venue supports it."""
        ...

    async def cancel_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        """Request cancellation. ``None`` if the venue returns no order snapshot.

        A cancel acknowledgment does not mean no further fills: callers must
        re-fetch the order and its trades afterwards.
        """
        ...

    async def fetch_order(self, exchange_order_id: str, symbol: str) -> ExchangeOrder | None:
        """``None`` if the venue reports no such order."""
        ...

    async def fetch_order_by_client_id(
        self, client_order_id: str, symbol: str
    ) -> ExchangeOrder | None:
        """Look up by client order ID; ``None`` if not found.

        Raises ``UnsupportedCapability`` if the venue cannot query by client ID,
        so callers never mistake "cannot ask" for "not found".
        """
        ...

    async def fetch_open_orders(self, symbol: str | None = None) -> Sequence[ExchangeOrder]: ...

    async def fetch_trades(
        self,
        symbol: str,
        *,
        since: datetime | None = None,
        limit: int | None = None,
    ) -> Sequence[ExchangeTrade]:
        """The account's own trades, ascending by time."""
        ...

    async def close(self) -> None: ...
