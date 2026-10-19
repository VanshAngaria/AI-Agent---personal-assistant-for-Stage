"""Dependency layer: every data read and model call goes through a budgeted, timed, fault-aware client.

Replay runs in virtual time. Each call charges a simulated duration to the decision's clock, so injected
outages (control_events.jsonl) translate into real latency and real fallbacks, deterministically.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from oua.settings import Settings
from oua.timeutil import iso, parse_ts


class BudgetExceeded(RuntimeError):
    pass


class DependencyUnavailable(RuntimeError):
    pass


class DependencyTimeout(RuntimeError):
    pass


class ModelSkipped(RuntimeError):
    """An optional model call was not started: it could break the latency target, or the system is shedding load."""


def unit_hash(*parts: Any) -> float:
    digest = hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest()[:12]
    return int(digest, 16) / 16**12


@dataclass
class FaultWindow:
    dependency: str
    kind: str  # unavailable | degraded
    start: datetime
    end: datetime
    detail: str


@dataclass
class FaultInjector:
    """Applies control-event dependency faults in virtual time."""

    windows: list[FaultWindow] = field(default_factory=list)

    def add_control(self, c: dict[str, Any]) -> FaultWindow | None:
        if c.get("type") not in ("dependency_unavailable", "dependency_degraded"):
            return None
        w = FaultWindow(
            dependency=c["dependency"],
            kind="unavailable" if c["type"] == "dependency_unavailable" else "degraded",
            start=parse_ts(c["at"]),
            end=parse_ts(c["until"]),
            detail=c.get("detail", ""),
        )
        self.windows.append(w)
        return w

    def active(self, dependency: str, at: datetime) -> FaultWindow | None:
        for w in self.windows:
            if w.dependency == dependency and w.start <= at < w.end:
                return w
        return None


@dataclass
class CircuitBreaker:
    """Opens after a dependency failure; skips calls until cooldown passes, then lets one probe through."""

    cooldown_s: float
    open_until: datetime | None = None
    last_error: str | None = None

    def allow(self, at: datetime) -> bool:
        return self.open_until is None or at >= self.open_until

    def trip(self, at: datetime, error: str) -> None:
        self.open_until = at + timedelta(seconds=self.cooldown_s)
        self.last_error = error

    def reset(self) -> None:
        self.open_until = None
        self.last_error = None


@dataclass
class CallRecord:
    kind: str  # tool | model
    name: str
    at: str
    duration_s: float
    outcome: str


class DecisionClock:
    """Virtual clock for one decision: start time + simulated dependency time + measured CPU time."""

    def __init__(self, start: datetime) -> None:
        self.start = start
        self.simulated_s = 0.0

    def charge(self, seconds: float) -> None:
        self.simulated_s += seconds

    def now(self, cpu_s: float = 0.0) -> datetime:
        return self.start + timedelta(seconds=self.simulated_s + cpu_s)


class ToolClient:
    """Per-decision gateway. Counts calls against the budget and applies faults/timeouts."""

    def __init__(self, settings: Settings, faults: FaultInjector, clock: DecisionClock,
                 store_breaker: CircuitBreaker, model_breaker: CircuitBreaker, call_key: str) -> None:
        self.s = settings
        self.faults = faults
        self.clock = clock
        self.store_breaker = store_breaker
        self.model_breaker = model_breaker
        self.call_key = call_key
        self.tool_calls = 0
        self.model_calls = 0
        self.log: list[CallRecord] = []
        self.model_deadline_s: float | None = None  # simulated seconds into the decision by which model calls must end
        self.shed_reason: str | None = None          # set when the harness is overloaded: skip optional model calls
        self.model_timed_out = False

    # -- tool calls (data store, catalogue, policy) ----------------------------------------------------------

    def tool(self, name: str, fn: Callable[[], Any], *, dependency: str | None = "user_data_store") -> Any:
        """Run a data read. Degraded store => simulated latency, timeouts and one retry."""
        attempts = 1 + (self.s.store_retries if dependency == "user_data_store" else 0)
        last_error = "unknown"
        for attempt in range(attempts):
            if self.tool_calls >= self.s.max_tool_calls:
                raise BudgetExceeded(f"tool budget {self.s.max_tool_calls} exhausted before {name}")
            self.tool_calls += 1
            at = self.clock.now()
            latency, failed = self._store_behaviour(dependency, name, attempt, at)
            self.clock.charge(latency)
            if failed:
                last_error = failed
                self.log.append(CallRecord("tool", name, iso(at), latency, failed))
                continue
            self.log.append(CallRecord("tool", name, iso(at), latency, "ok"))
            return fn()
        raise DependencyTimeout(f"{dependency}:{name} failed after {attempts} attempt(s): {last_error}")

    def _store_behaviour(self, dependency: str | None, name: str, attempt: int, at: datetime) -> tuple[float, str | None]:
        if dependency is None:
            return 0.0, None
        base = self.s.store_latency_s
        w = self.faults.active(dependency, at)
        if w is None:
            return base, None
        if w.kind == "unavailable":
            return base, f"{dependency} unavailable ({w.detail})"
        # Degraded: p95 ~8 s, 20% of reads time out (control event detail). Deterministic per call.
        u = unit_hash(self.call_key, name, attempt)
        if u < 0.20:
            return self.s.store_timeout_s, f"{dependency} read timed out after {self.s.store_timeout_s}s"
        latency = 0.2 + 7.8 * ((u - 0.20) / 0.80) ** 3  # heavy tail: median ~1.2 s, p95 ~8 s
        if latency > self.s.store_timeout_s:
            return self.s.store_timeout_s, f"{dependency} read exceeded timeout ({latency:.1f}s > {self.s.store_timeout_s}s)"
        return latency, None

    # -- model calls ----------------------------------------------------------------------------------------

    def model(self, name: str, fn: Callable[[float], Any]) -> Any:
        """Run a model call with a hard timeout. `fn(timeout_s)` returns (output, simulated_latency_s)."""
        if self.model_calls >= self.s.max_model_calls:
            raise BudgetExceeded(f"model budget {self.s.max_model_calls} exhausted before {name}")
        at = self.clock.now()
        timeout = self.s.model_timeout_s
        if self.shed_reason:
            self.log.append(CallRecord("model", name, iso(at), 0.0, "shed"))
            raise ModelSkipped(f"load shedding: {self.shed_reason}")
        if self.model_timed_out:
            self.log.append(CallRecord("model", name, iso(at), 0.0, "skipped_after_timeout"))
            raise ModelSkipped("a model call already timed out in this decision; not spending more of the budget")
        if self.model_deadline_s is not None and self.clock.simulated_s + timeout > self.model_deadline_s:
            self.log.append(CallRecord("model", name, iso(at), 0.0, "skipped_deadline"))
            raise ModelSkipped(f"starting {name} could push the page past its {self.model_deadline_s:.1f}s target")
        if not self.model_breaker.allow(at):
            self.log.append(CallRecord("model", name, iso(at), 0.0, "skipped_circuit_open"))
            raise DependencyUnavailable(f"model_provider circuit open ({self.model_breaker.last_error})")
        self.model_calls += 1
        w = self.faults.active("model_provider", at)
        if w is not None and w.kind == "unavailable":
            self.clock.charge(0.05)
            self.model_breaker.trip(at, f"HTTP 503 ({w.detail})")
            self.log.append(CallRecord("model", name, iso(at), 0.05, "unavailable"))
            raise DependencyUnavailable(f"model_provider unavailable: {w.detail}")
        try:
            output, latency = fn(timeout)
        except DependencyUnavailable as exc:  # a real provider failing (5xx, 429, network): same path as an injected 503
            self.clock.charge(0.05)
            self.model_breaker.trip(at, str(exc))
            self.log.append(CallRecord("model", name, iso(at), 0.05, "unavailable"))
            raise
        if latency >= timeout:
            self.model_timed_out = True
            self.clock.charge(timeout)
            self.log.append(CallRecord("model", name, iso(at), timeout, "timeout"))
            raise DependencyTimeout(f"model call {name} cancelled after {timeout}s (would have taken {latency:.0f}s)")
        self.clock.charge(latency)
        self.model_breaker.reset()
        self.log.append(CallRecord("model", name, iso(at), latency, "ok"))
        return output

    def summary(self) -> dict[str, Any]:
        return {
            "tool_calls": self.tool_calls,
            "max_tool_calls": self.s.max_tool_calls,
            "model_calls": self.model_calls,
            "max_model_calls": self.s.max_model_calls,
            "calls": [r.__dict__ for r in self.log],
        }
