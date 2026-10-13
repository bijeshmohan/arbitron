"""Prometheus metrics (plan §19.2).

One ``Metrics`` object owns a dedicated ``CollectorRegistry`` (not the global
default), so tests and multiple sessions never collide. Labels are
low-cardinality only: symbol, side, outcome, reason, task. Never order IDs.
"""

from collections.abc import Callable, Iterable, Mapping
from decimal import Decimal

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    disable_created_metrics,
    generate_latest,
)
from prometheus_client.core import GaugeMetricFamily
from prometheus_client.registry import Collector

from arbitron.domain.enums import KillSwitchState


class _HeartbeatCollector(Collector):
    """Heartbeat ages are computed at scrape time, so a stuck task shows a growing age."""

    def __init__(self, ages: Callable[[], Mapping[str, float]]) -> None:
        self._ages = ages

    def collect(self) -> Iterable[GaugeMetricFamily]:
        family = GaugeMetricFamily(
            "arbitron_task_heartbeat_age_seconds",
            "Seconds since each supervised task last reported progress",
            labels=["task"],
        )
        for task, age in self._ages().items():
            family.add_metric([task], age)
        yield family


# Counters' *_created timestamp series add noise and nothing we use.
disable_created_metrics()


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry(auto_describe=True)
        r = self.registry
        # Venue
        self.venue_requests = Counter(
            "arbitron_venue_requests_total", "Venue API calls", ["operation", "outcome"], registry=r
        )
        self.venue_latency = Histogram(
            "arbitron_venue_request_seconds",
            "Venue API call latency",
            ["operation"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=r,
        )
        # Market data
        self.market_data_age = Gauge(
            "arbitron_market_data_age_seconds",
            "Age of the latest closed candle",
            ["symbol"],
            registry=r,
        )
        self.quality_gate_ok = Gauge(
            "arbitron_quality_gate_ok", "1 if the data-quality gate passes", ["symbol"], registry=r
        )
        # Strategy, risk, orders
        self.signals = Counter("arbitron_signals_total", "Signals persisted", ["side"], registry=r)
        self.order_outcomes = Counter(
            "arbitron_order_outcomes_total", "Order manager outcomes", ["action"], registry=r
        )
        self.risk_rejections = Counter(
            "arbitron_risk_rejections_total", "Risk rejections by reason", ["reason"], registry=r
        )
        # Reconciliation
        self.reconciliation_runs = Counter(
            "arbitron_reconciliation_runs_total", "Reconciliation runs", ["result"], registry=r
        )
        self.reconciliation_discrepancies = Gauge(
            "arbitron_reconciliation_discrepancies", "Discrepancies in the latest run", registry=r
        )
        self.reconciliation_pending = Gauge(
            "arbitron_reconciliation_pending", "Pending items in the latest run", registry=r
        )
        # Portfolio and safety
        # Labelled so nothing is exported until equity is actually known (an
        # unlabelled gauge would report a misleading 0 at startup).
        self.equity = Gauge(
            "arbitron_equity", "Equity in the quote currency", ["quote"], registry=r
        )
        self.kill_switch = Gauge(
            "arbitron_kill_switch_state",
            "1 for the current kill-switch state",
            ["state"],
            registry=r,
        )
        # Supervision and alerts
        self.cycle_seconds = Histogram(
            "arbitron_cycle_seconds",
            "Duration of a trading cycle",
            buckets=(0.5, 1, 2.5, 5, 10, 30, 60, 120),
            registry=r,
        )
        self.task_restarts = Counter(
            "arbitron_task_restarts_total", "Supervised task restarts", ["task"], registry=r
        )
        self.alerts = Counter("arbitron_alerts_total", "Alerts by outcome", ["outcome"], registry=r)

    def watch_heartbeats(self, ages: Callable[[], Mapping[str, float]]) -> None:
        self.registry.register(_HeartbeatCollector(ages))

    def set_kill_switch(self, state: KillSwitchState | None) -> None:
        for candidate in KillSwitchState:
            self.kill_switch.labels(candidate.value).set(1 if candidate is state else 0)

    def set_equity(self, equity: Decimal | None, quote: str) -> None:
        if equity is not None:
            self.equity.labels(quote).set(float(equity))  # metrics are approximate by nature

    def render(self) -> bytes:
        return generate_latest(self.registry)
