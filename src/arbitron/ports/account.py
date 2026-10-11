from collections.abc import Mapping
from typing import Protocol

from arbitron.domain.portfolio import Balance


class AccountPort(Protocol):
    """Account-level state. Implemented by the live adapter and the paper broker."""

    async def fetch_balances(self) -> Mapping[str, Balance]:
        """Balances keyed by asset code."""
        ...

    async def close(self) -> None: ...
