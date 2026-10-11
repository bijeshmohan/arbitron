"""Bounded retry with exponential backoff and jitter, for safe reads only.

Never wrap order submission or cancellation in this: a retried write can
duplicate an order whose first attempt actually reached the venue.
"""

import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from arbitron.ports.errors import VenueError

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    jitter: float = 0.25  # +/- fraction of the computed delay

    def __post_init__(self) -> None:
        if self.attempts < 1:
            msg = "attempts must be >= 1"
            raise ValueError(msg)
        if not 0 <= self.jitter < 1:
            msg = "jitter must be in [0, 1)"
            raise ValueError(msg)

    def delay(self, retry_number: int, rng: random.Random) -> float:
        """Delay before retry ``retry_number`` (1-based)."""
        delay = min(self.max_delay, self.base_delay * 2 ** (retry_number - 1))
        return delay * (1 + rng.uniform(-self.jitter, self.jitter))


async def retry_read[T](
    operation: Callable[[], Awaitable[T]],
    policy: RetryPolicy,
    *,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
) -> T:
    """Run ``operation``, retrying only ``VenueError``s marked retryable."""
    rng = rng or random.Random()  # noqa: S311 - jitter, not cryptography
    for attempt in range(1, policy.attempts + 1):
        try:
            return await operation()
        except VenueError as exc:
            if not exc.retryable or attempt == policy.attempts:
                raise
            await sleep(policy.delay(attempt, rng))
    raise AssertionError("unreachable")  # pragma: no cover
