from typing import Protocol

from arbitron.domain.events import Alert


class Notifier(Protocol):
    """Delivers operator alerts (Telegram, email, webhook, ...).

    Deduplication and rate limiting happen before this port, keyed on
    ``Alert.dedupe_key``. Delivery failure must be logged and must never block
    or reverse a safety action that has already been taken.
    """

    async def notify(self, alert: Alert) -> None: ...
