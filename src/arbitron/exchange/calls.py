"""Bounded, observed CCXT calls shared by the market-data and trading adapters.

Every call is concurrency-limited, guarded by an outer ``asyncio.timeout``,
translated into the venue error taxonomy, and timed for metrics. Reads may be
retried; writes never are (see ``exchange.retry``).
"""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

from arbitron.exchange.errors import venue_errors
from arbitron.exchange.retry import RetryPolicy, Sleep, retry_read


class VenueCaller:
    def __init__(
        self,
        *,
        request_timeout: timedelta,
        retry: RetryPolicy,
        concurrency: int,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
    ) -> None:
        # Outer guard slightly above CCXT's own timeout, so CCXT normally
        # reports first and this only catches a hung call.
        self._call_timeout = request_timeout.total_seconds() * 1.5
        self._retry = retry
        self._semaphore = asyncio.Semaphore(concurrency)
        self._sleep = sleep
        self._rng = rng
        # (operation, outcome, seconds) per attempt, for metrics; set by the runtime.
        self.observer: Callable[[str, str, float], None] | None = None

    async def _attempt[T](self, operation: str, make_call: Callable[[], Awaitable[T]]) -> T:
        async with self._semaphore:
            started = time.monotonic()
            outcome = "ok"
            try:
                with venue_errors():
                    async with asyncio.timeout(self._call_timeout):
                        return await make_call()
            except Exception as exc:
                outcome = type(exc).__name__
                raise
            finally:
                if self.observer is not None:
                    self.observer(operation, outcome, time.monotonic() - started)

    async def read[T](self, operation: str, make_call: Callable[[], Awaitable[T]]) -> T:
        """A safe read: retried on retryable venue errors."""
        return await retry_read(
            lambda: self._attempt(operation, make_call),
            self._retry,
            sleep=self._sleep,
            rng=self._rng,
        )

    async def write[T](self, operation: str, make_call: Callable[[], Awaitable[T]]) -> T:
        """A state-changing request: exactly one attempt, never retried."""
        return await self._attempt(operation, make_call)
