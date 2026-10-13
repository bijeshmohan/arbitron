"""Task health and readiness (plan §15.1).

Every supervised task reports heartbeats here. A task is *stale* when it has
not beaten within its expected interval; a stale or failed **critical** task
makes the process not ready. Readiness also covers the trading state
(startup done, kill switch armed, reconciliation clean, market data ok); those
checks are supplied by the runtime.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from arbitron.ports.clock import Clock


class TaskStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    RESTARTING = "restarting"
    FAILED = "failed"
    STOPPED = "stopped"


@dataclass
class TaskHealth:
    name: str
    critical: bool
    stale_after: timedelta
    status: TaskStatus = TaskStatus.STARTING
    last_beat: datetime | None = None
    detail: str = ""
    restarts: int = 0


@dataclass(frozen=True, slots=True)
class Readiness:
    """``checks`` maps each check to ``"ok"`` or the reason it fails."""

    checks: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    @property
    def ready(self) -> bool:
        return all(value == "ok" for value in self.checks.values())


class HealthRegistry:
    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._tasks: dict[str, TaskHealth] = {}

    def register(self, name: str, *, critical: bool, stale_after: timedelta) -> None:
        self._tasks[name] = TaskHealth(name, critical, stale_after)

    def beat(self, name: str, detail: str = "") -> None:
        task = self._tasks[name]
        task.last_beat = self._clock.now()
        task.status = TaskStatus.RUNNING
        if detail:
            task.detail = detail

    def set_status(self, name: str, status: TaskStatus, detail: str = "") -> None:
        task = self._tasks[name]
        task.status = status
        task.detail = detail or task.detail
        if status is TaskStatus.RESTARTING:
            task.restarts += 1

    def ages(self) -> dict[str, float]:
        now = self._clock.now()
        return {
            name: (now - task.last_beat).total_seconds()
            for name, task in self._tasks.items()
            if task.last_beat is not None
        }

    def task_checks(self) -> dict[str, str]:
        """Readiness checks for critical tasks: running and recently beating."""
        now = self._clock.now()
        checks: dict[str, str] = {}
        for name, task in self._tasks.items():
            if not task.critical:
                continue
            if task.status is not TaskStatus.RUNNING:
                checks[f"task:{name}"] = f"{task.status.value} {task.detail}".strip()
            elif task.last_beat is None:
                checks[f"task:{name}"] = "started; first iteration not complete yet"
            elif now - task.last_beat > task.stale_after:
                checks[f"task:{name}"] = f"no heartbeat for over {task.stale_after}"
            else:
                checks[f"task:{name}"] = "ok"
        return checks

    def view(self) -> dict[str, dict[str, object]]:
        now = self._clock.now()
        return {
            name: {
                "status": task.status.value,
                "critical": task.critical,
                "heartbeat_age_seconds": (
                    round((now - task.last_beat).total_seconds(), 3) if task.last_beat else None
                ),
                "stale_after_seconds": task.stale_after.total_seconds(),
                "restarts": task.restarts,
                "detail": task.detail,
            }
            for name, task in self._tasks.items()
        }
