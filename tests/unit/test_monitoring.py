"""Logging redaction, health, metrics, alert mapping, Telegram notifier, HTTP serving."""

import json
import logging
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from fastapi import FastAPI

from arbitron.clocks import ManualClock
from arbitron.domain.enums import KillSwitchState, Severity
from arbitron.domain.events import Alert, SystemEvent
from arbitron.monitoring.alerts import (
    LogNotifier,
    NotifierError,
    TelegramNotifier,
    alert_for,
    format_alert,
)
from arbitron.monitoring.health import HealthRegistry, Readiness, TaskStatus
from arbitron.monitoring.logging import REDACTED, JsonFormatter, Redactor, TextFormatter
from arbitron.monitoring.metrics import Metrics
from tests.factories import T0

TOKEN = "123456789:AAH-abcdefghijklmnopqrstuvwxyz_0123456"  # fake, test-only


def record(message: str, **extra: object) -> logging.LogRecord:
    rec = logging.LogRecord("arbitron.test", logging.ERROR, __file__, 1, message, (), None)
    for key, value in extra.items():
        setattr(rec, key, value)
    return rec


class TestRedaction:
    def test_registered_secrets_are_removed_everywhere(self) -> None:
        redactor = Redactor(["sk-live-supersecret"])

        assert redactor("key=sk-live-supersecret!") == f"key={REDACTED}!"

    def test_short_values_are_not_registered(self) -> None:
        assert Redactor(["abc"])("abc def") == "abc def"

    def test_url_credentials_are_removed(self) -> None:
        text = Redactor()("connect postgresql+psycopg://arbitron:hunter2@db:5432/x failed")

        assert "hunter2" not in text
        assert f"arbitron:{REDACTED}@db" in text

    def test_telegram_tokens_are_removed(self) -> None:
        # The token directly follows "bot" in API URLs: no word boundary there.
        text = Redactor()(f"POST https://api.telegram.org/bot{TOKEN}/sendMessage")

        assert TOKEN not in text
        assert "api.telegram.org/bot[REDACTED]/sendMessage" in text

    def test_json_formatter_redacts_message_extras_and_exceptions(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(JsonFormatter, "redactor", Redactor(["my-api-key-123"]))
        formatter = JsonFormatter()
        try:
            raise ValueError("failed with my-api-key-123")
        except ValueError:
            import sys  # noqa: PLC0415

            rec = record("using my-api-key-123", detail="key my-api-key-123", order_id="o-1")
            rec.exc_info = sys.exc_info()
        out = formatter.format(rec)

        assert "my-api-key-123" not in out
        fields = json.loads(out)
        assert fields["level"] == "error"
        assert fields["component"] == "arbitron.test"
        assert fields["order_id"] == "o-1"
        assert "ValueError" in fields["error"]

    def test_text_formatter(self) -> None:
        out = TextFormatter().format(record("hello", symbol="BTC/USDT"))

        assert "ERROR   arbitron.test: hello [symbol=BTC/USDT]" in out


class TestHealth:
    def test_readiness_tracks_critical_tasks_only(self) -> None:
        clock = ManualClock(T0)
        health = HealthRegistry(clock)
        health.register("trading", critical=True, stale_after=timedelta(minutes=12))
        health.register("http", critical=False, stale_after=timedelta(days=1))

        assert health.task_checks() == {"task:trading": "starting"}
        health.set_status("trading", TaskStatus.RUNNING)
        assert health.task_checks() == {"task:trading": "started; first iteration not complete yet"}
        health.beat("trading", "cycle ok")
        assert health.task_checks() == {"task:trading": "ok"}

        clock.advance(timedelta(minutes=13))
        assert "no heartbeat" in health.task_checks()["task:trading"]
        health.set_status("trading", TaskStatus.RESTARTING, "boom")
        assert health.task_checks()["task:trading"].startswith("restarting")
        assert health.view()["trading"]["restarts"] == 1

    def test_readiness_requires_every_check(self) -> None:
        assert Readiness({"a": "ok", "b": "ok"}).ready
        assert not Readiness({"a": "ok", "b": "kill switch tripped"}).ready


class TestMetrics:
    def test_render_includes_heartbeats_kill_switch_and_equity(self) -> None:
        clock = ManualClock(T0)
        health = HealthRegistry(clock)
        health.register("trading", critical=True, stale_after=timedelta(minutes=1))
        health.beat("trading")
        clock.advance(timedelta(seconds=7))
        metrics = Metrics()
        metrics.watch_heartbeats(health.ages)
        metrics.set_kill_switch(KillSwitchState.ARMED)
        metrics.set_equity(Decimal("1000.5"), "USDT")
        metrics.order_outcomes.labels("executed").inc()

        text = metrics.render().decode()

        assert 'arbitron_task_heartbeat_age_seconds{task="trading"} 7.0' in text
        assert 'arbitron_kill_switch_state{state="armed"} 1.0' in text
        assert 'arbitron_kill_switch_state{state="tripped"} 0.0' in text
        assert 'arbitron_equity{quote="USDT"} 1000.5' in text
        assert "_created" not in text

    def test_equity_is_absent_until_known(self) -> None:
        metrics = Metrics()
        metrics.set_equity(None, "USDT")

        assert "arbitron_equity{" not in metrics.render().decode()

    def test_separate_instances_do_not_collide(self) -> None:
        Metrics()
        Metrics()  # a global registry would raise "Duplicated timeseries"


class TestAlertMapping:
    def event(self, event_type: str, **payload: str) -> SystemEvent:
        return SystemEvent(event_type, Severity.CRITICAL, "test", T0, "c", payload)

    def test_known_events_get_actions_and_remediation(self) -> None:
        alert = alert_for(self.event("kill_switch.tripped", reason="daily loss"))

        assert alert.title == "Kill switch TRIPPED"
        assert "blocked" in alert.action_taken
        assert "killswitch arm" in alert.remediation
        assert "reason=daily loss" in alert.message

    def test_subject_drives_title_and_dedupe(self) -> None:
        a = alert_for(self.event("order.submission_unknown", order_id="o-1"))
        b = alert_for(self.event("order.submission_unknown", order_id="o-2"))

        assert a.title == "Order outcome unknown: o-1"
        assert a.dedupe_key != b.dedupe_key

    def test_most_specific_prefix_wins(self) -> None:
        assert alert_for(self.event("supervisor.restart_limit", task="trading")).title.startswith(
            "Task crashed repeatedly"
        )
        assert alert_for(self.event("supervisor.task_crashed", task="trading")).title.startswith(
            "Task crashed and is restarting"
        )

    def test_unknown_events_still_alert(self) -> None:
        alert = alert_for(self.event("something.new"))

        assert alert.title == "something.new"

    def test_format_states_action_and_next_step(self) -> None:
        text = format_alert(alert_for(self.event("reconciliation.discrepancy", detail="x")))

        assert "[arbitron CRITICAL]" in text
        assert "Action taken:" in text
        assert "Next step:" in text


ALERT = Alert(Severity.CRITICAL, "t", "m", "a", "r", "k", T0)


class TestTelegramNotifier:
    async def test_sends_plain_text_to_the_chat(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"ok": True})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        await TelegramNotifier(bot_token=TOKEN, chat_id="42", client=client).notify(ALERT)

        assert seen[0].url.path == f"/bot{TOKEN}/sendMessage"
        body = json.loads(seen[0].content)
        assert body["chat_id"] == "42"
        assert "Action taken: a" in body["text"]
        assert "parse_mode" not in body  # plain text: nothing to escape or inject

    async def test_http_error_status_raises_without_token(self) -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401)))

        with pytest.raises(NotifierError, match="HTTP 401") as exc_info:
            await TelegramNotifier(bot_token=TOKEN, chat_id="42", client=client).notify(ALERT)
        assert TOKEN not in str(exc_info.value)

    async def test_transport_error_never_leaks_the_url(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        with pytest.raises(NotifierError) as exc_info:
            await TelegramNotifier(bot_token=TOKEN, chat_id="42", client=client).notify(ALERT)

        assert TOKEN not in str(exc_info.value)
        assert exc_info.value.__cause__ is None  # the chained httpx error is suppressed

    async def test_log_notifier_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        await LogNotifier().notify(ALERT)

        assert "ALERT t: m" in caplog.text


def _free_port() -> int:
    import socket  # noqa: PLC0415

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TestServe:
    def app(self) -> FastAPI:
        app = FastAPI()

        @app.get("/ready")
        async def ready() -> dict[str, bool]:  # pyright: ignore[reportUnusedFunction]
            return {"ready": True}

        return app

    async def test_serves_until_stopped(self) -> None:
        import asyncio  # noqa: PLC0415

        from arbitron.monitoring.http import serve  # noqa: PLC0415

        port = _free_port()
        stop = asyncio.Event()
        response: httpx.Response | None = None
        server = asyncio.create_task(serve(self.app(), host="127.0.0.1", port=port, stop=stop))
        async with httpx.AsyncClient() as client:
            for _ in range(200):
                try:
                    response = await client.get(f"http://127.0.0.1:{port}/ready")
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.02)
        stop.set()
        await asyncio.wait_for(server, timeout=5)

        assert response is not None
        assert response.status_code == 200

    async def test_busy_port_is_reported(self) -> None:
        import asyncio  # noqa: PLC0415
        import socket  # noqa: PLC0415

        from arbitron.monitoring.http import HttpServerError, serve  # noqa: PLC0415

        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            # uvicorn calls sys.exit() when it cannot bind; serve() must turn that
            # into an ordinary error rather than let SystemExit escape.
            with pytest.raises(HttpServerError, match="could not start"):
                await asyncio.wait_for(
                    serve(self.app(), host="127.0.0.1", port=port, stop=asyncio.Event()),
                    timeout=5,
                )
