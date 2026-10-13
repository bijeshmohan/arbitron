"""Operator alerts (plan §19.3; operator decisions 2026-10-13: Telegram, critical only).

Every component already records what happened as ``system_events``. The
``AlertDispatcher`` follows that table and turns new events at or above the
configured severity into alerts that state what happened, the **action the
system already took**, and a **remediation hint**:

- **Deduplicated:** the same event about the same subject (order, symbol, task)
  is sent at most once per ``dedupe_minutes``; repeats are counted and
  reported with the next one.
- **Rate limited:** at most ``max_per_hour``; excess is counted and summarized.
- **Never blocking:** delivery failures are logged, counted, and retried with
  backoff for up to an hour; the safety action behind the alert has already
  happened by the time it is sent.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Final

import httpx

from arbitron.domain.enums import Severity
from arbitron.domain.events import Alert, SystemEvent
from arbitron.monitoring.logging import Redactor
from arbitron.ports.clock import Clock
from arbitron.ports.notifier import Notifier
from arbitron.ports.repositories import UnitOfWork

logger = logging.getLogger("arbitron.alerts")

TELEGRAM_API: Final = "https://api.telegram.org"
TELEGRAM_MAX_CHARS: Final = 4000  # Telegram's limit is 4096
RETRY_FOR: Final = timedelta(hours=1)


class NotifierError(Exception):
    """Delivery failed. The message never contains credentials or URLs."""


class TelegramNotifier:
    """Sends alerts with the Telegram Bot API (``sendMessage``), as plain text."""

    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def notify(self, alert: Alert) -> None:
        try:
            response = await self._client.post(
                f"{TELEGRAM_API}/bot{self._token}/sendMessage",
                json={
                    "chat_id": self._chat_id,
                    "text": format_alert(alert)[:TELEGRAM_MAX_CHARS],
                    "disable_web_page_preview": True,
                },
            )
        except httpx.HTTPError as exc:
            # httpx messages include the request URL, which contains the token.
            msg = f"telegram request failed: {type(exc).__name__}"
            raise NotifierError(msg) from None
        if response.status_code != 200:
            msg = f"telegram returned HTTP {response.status_code}"
            raise NotifierError(msg)

    async def close(self) -> None:
        await self._client.aclose()


class LogNotifier:
    """Logs alerts only. For trying things out; never for unattended running."""

    async def notify(self, alert: Alert) -> None:
        logger.error(
            "ALERT %s: %s",
            alert.title,
            alert.message,
            extra={"action_taken": alert.action_taken, "remediation": alert.remediation},
        )


def format_alert(alert: Alert) -> str:
    return (
        f"[arbitron {alert.severity.value.upper()}] {alert.title}\n\n"
        f"{alert.message}\n\n"
        f"Action taken: {alert.action_taken}\n"
        f"Next step: {alert.remediation}\n"
        f"At: {alert.occurred_at.isoformat()}"
    )


# event_type prefix -> (title, action already taken, remediation)
_PLAYBOOK: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "kill_switch.tripped",
        "Kill switch TRIPPED",
        "All new orders are blocked.",
        "Investigate the reason, then `arbitron killswitch review` and `killswitch arm`.",
    ),
    (
        "kill_switch.",
        "Kill switch changed",
        "Recorded in kill_switch_events.",
        "Run `arbitron killswitch status` to confirm the state is what you expect.",
    ),
    (
        "order.submission_unknown",
        "Order outcome unknown",
        "New orders are blocked; reconciliation is checking the venue.",
        "Usually resolves automatically; if it escalates, run `arbitron orders unresolved`.",
    ),
    (
        "order.exchange_rejected",
        "Order rejected by the venue",
        "The order was closed as exchange_rejected.",
        "Check the error. Authentication failures also trip the kill switch.",
    ),
    (
        "order.venue_result_discrepancy",
        "Venue result inconsistent",
        "Order marked reconciliation_required; new orders are blocked.",
        "Compare `arbitron paper status` with the venue, then follow docs/failure-matrix.md.",
    ),
    (
        "order.operator_resolved",
        "Order resolved by an operator",
        "Recorded with the operator's name and note.",
        "No action needed if this was you.",
    ),
    (
        "reconciliation.discrepancy",
        "Reconciliation discrepancy",
        "New orders are blocked; the kill switch is in recovery review.",
        "Resolve the difference (`orders unresolved`, `paper status`), then arm the kill switch.",
    ),
    (
        "reconciliation.",
        "Reconciliation event",
        "New orders are blocked until reconciliation is clean.",
        "Run `arbitron orders unresolved`; most cases resolve automatically.",
    ),
    (
        "supervisor.restart_limit",
        "Task crashed repeatedly: session stopped",
        "The kill switch was tripped and the session stopped.",
        "Read the logs for the crash, fix the cause, restart, review and arm.",
    ),
    (
        "supervisor.task_crashed",
        "Task crashed and is restarting",
        "The task is being restarted with backoff.",
        "Watch for repeats; after the restart limit the session stops.",
    ),
    (
        "supervisor.",
        "Supervisor event",
        "Recorded.",
        "Check the logs and `/ready`.",
    ),
    (
        "strategy.failed",
        "Strategy failed",
        "Evaluation stopped for that market; its cursor did not advance.",
        "Read the error in the logs; fix the strategy or its config.",
    ),
    (
        "market_data.",
        "Market data problem",
        "Strategy evaluation and new orders are blocked for that market.",
        "Usually clears on its own; if it persists, check the venue and `market sync`.",
    ),
)


def alert_for(event: SystemEvent) -> Alert:
    title, action, remediation = event.event_type, "Recorded in system_events.", "See the logs."
    for prefix, t, a, r in _PLAYBOOK:
        if event.event_type.startswith(prefix):
            title, action, remediation = t, a, r
            break
    subject = next(
        (str(event.payload[k]) for k in ("order_id", "symbol", "task") if event.payload.get(k)),
        "",
    )
    details = ", ".join(f"{k}={v}" for k, v in event.payload.items() if v is not None)
    return Alert(
        severity=event.severity,
        title=f"{title}: {subject}" if subject else title,
        message=f"{event.event_type} from {event.component}" + (f" ({details})" if details else ""),
        action_taken=action,
        remediation=remediation,
        dedupe_key=f"{event.event_type}:{subject}",
        occurred_at=event.occurred_at,
    )


def _ignore_outcome(_outcome: str) -> None:
    return None


@dataclass
class _Pending:
    alert: Alert
    first_attempt: datetime
    next_attempt: datetime
    attempts: int = 0


@dataclass
class DispatcherStats:
    sent: int = 0
    deduplicated: int = 0
    rate_limited: int = 0
    failed: int = 0
    dropped: int = 0
    repeats: dict[str, int] = field(default_factory=dict[str, int])


class AlertDispatcher:
    def __init__(
        self,
        *,
        notifier: Notifier,
        uow_factory: Callable[[], UnitOfWork],
        clock: Clock,
        min_severity: Severity,
        dedupe_window: timedelta,
        max_per_hour: int,
        redactor: Redactor | None = None,
        on_outcome: Callable[[str], None] | None = None,
    ) -> None:
        self._notifier = notifier
        self._uow_factory = uow_factory
        self._clock = clock
        self._severities = frozenset(
            {Severity.CRITICAL}
            if min_severity is Severity.CRITICAL
            else {Severity.CRITICAL, Severity.WARNING}
        )
        self._dedupe_window = dedupe_window
        self._max_per_hour = max_per_hour
        self._redact = redactor or Redactor()
        self._on_outcome: Callable[[str], None] = on_outcome or _ignore_outcome
        self._cursor: int | None = None
        self._last_sent: dict[str, datetime] = {}
        self._sent_times: deque[datetime] = deque()
        self._pending: list[_Pending] = []
        self.stats = DispatcherStats()

    async def start_from_now(self) -> None:
        """Alert only on events recorded from now on (history is not re-sent)."""
        async with self._uow_factory() as tx:
            self._cursor = await tx.system_events.latest_id()

    async def poll(self) -> int:
        """Process new events and due retries. Returns alerts sent."""
        if self._cursor is None:
            await self.start_from_now()
        async with self._uow_factory() as tx:
            rows = await tx.system_events.list_after(self._cursor or 0, self._severities, 200)
        sent = 0
        for event_id, event in rows:
            self._cursor = event_id
            sent += await self._handle(alert_for(event))
        sent += await self._retry_due()
        return sent

    async def run(
        self, stop: asyncio.Event, *, interval: float = 5.0, beat: Callable[[], None] | None = None
    ) -> None:
        while not stop.is_set():
            await self.poll()
            if beat is not None:
                beat()
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _handle(self, alert: Alert) -> int:
        now = self._clock.now()
        last = self._last_sent.get(alert.dedupe_key)
        if last is not None and now - last < self._dedupe_window:
            self.stats.deduplicated += 1
            self.stats.repeats[alert.dedupe_key] = self.stats.repeats.get(alert.dedupe_key, 0) + 1
            self._on_outcome("deduplicated")
            return 0
        while self._sent_times and now - self._sent_times[0] >= timedelta(hours=1):
            self._sent_times.popleft()
        if len(self._sent_times) >= self._max_per_hour:
            self.stats.rate_limited += 1
            self._on_outcome("rate_limited")
            return 0
        return await self._deliver(_Pending(self._annotated(alert), now, now))

    def _annotated(self, alert: Alert) -> Alert:
        notes: list[str] = []
        repeats = self.stats.repeats.pop(alert.dedupe_key, 0)
        if repeats:
            notes.append(f"repeated {repeats} more time(s) since the last alert")
        if self.stats.rate_limited:
            notes.append(f"{self.stats.rate_limited} alert(s) were suppressed by the rate limit")
            self.stats.rate_limited = 0
        if not notes:
            return alert
        return replace(alert, message=f"{alert.message}\n({'; '.join(notes)})")

    async def _deliver(self, pending: _Pending) -> int:
        now = self._clock.now()
        pending.attempts += 1
        try:
            await self._notifier.notify(pending.alert)
        except Exception as exc:  # delivery must never take the bot down
            self.stats.failed += 1
            self._on_outcome("failed")
            if now - pending.first_attempt >= RETRY_FOR:
                self.stats.dropped += 1
                self._on_outcome("dropped")
                logger.critical(
                    "alert dropped after retrying for %s: %s",
                    RETRY_FOR,
                    pending.alert.title,
                    extra={"error": self._redact(str(exc))},
                )
                return 0
            pending.next_attempt = now + min(
                timedelta(seconds=15 * 2**pending.attempts), timedelta(minutes=10)
            )
            self._pending.append(pending)
            logger.error(
                "alert delivery failed; will retry",
                extra={"alert": pending.alert.title, "error": self._redact(str(exc))},
            )
            return 0
        self._last_sent[pending.alert.dedupe_key] = now
        self._sent_times.append(now)
        self.stats.sent += 1
        self._on_outcome("sent")
        logger.info("alert sent", extra={"alert": pending.alert.title})
        return 1

    async def _retry_due(self) -> int:
        now = self._clock.now()
        due = [p for p in self._pending if p.next_attempt <= now]
        self._pending = [p for p in self._pending if p.next_attempt > now]
        return sum([await self._deliver(p) for p in due])
