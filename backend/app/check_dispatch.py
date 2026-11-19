"""Schedule outbound probes without starting all HTTP checks in the same instant."""

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field

import httpx


_MAX_SPREAD = 10.0
_START_SPACING = 0.1
_ORIGIN_CONCURRENCY = 4

Origin = tuple[str, bytes, int]
Target = Origin | int


def http_origin(target: str) -> Origin | None:
    target = target.strip()
    try:
        url = httpx.URL(target if "://" in target else "https://" + target)
    except (httpx.InvalidURL, ValueError):
        return None  # The probe reports the configuration error as before.
    return _origin(url)


def _origin(url: httpx.URL) -> Origin | None:
    if url.scheme not in {"http", "https"} or not url.raw_host:
        return None
    return url.scheme, url.raw_host.lower().rstrip(b"."), url.port or (443 if url.scheme == "https" else 80)


def known_backends(index) -> dict[bytes, int]:
    """Use explicit web-service names and inventory IPs owned by one server."""
    owners: dict[bytes, set[int]] = {}
    for names in (index.domains, index.ips):
        for name, servers in names.items():
            try:
                host = httpx.URL(scheme="https", host=name).raw_host.lower().rstrip(b".")
            except (httpx.InvalidURL, ValueError):
                continue
            if host:
                owners.setdefault(host, set()).update(servers)
    return {host: next(iter(servers)) for host, servers in owners.items() if len(servers) == 1}


@dataclass(frozen=True)
class Probe:
    run: Callable[[], Awaitable]
    origin: Origin | None = None
    interval: int = 0


@dataclass
class OriginSlots:
    concurrent: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(_ORIGIN_CONCURRENCY))
    start: asyncio.Lock = field(default_factory=asyncio.Lock)
    next_start: float = 0.0


@dataclass
class BatchBudget:
    backends: dict[bytes, int]
    slots: dict[Target, OriginSlots] = field(default_factory=dict)

    def target(self, origin: Origin) -> Target:
        return self.backends.get(origin[1], origin)


_REQUEST_SLOTS: ContextVar[BatchBudget | None] = ContextVar("request_slots", default=None)
_QUEUE_WAIT: ContextVar[float] = ContextVar("request_queue_wait", default=0.0)


def probe_clock() -> float:
    """Local HTTP queue time is excluded from request latency and slow-site alerts."""
    return time.perf_counter() - _QUEUE_WAIT.get()


class RequestGate:
    """Hold one permit until HTTPX has read the response or starts a redirect."""

    def __init__(self, budget: BatchBudget):
        self.budget = budget
        self.permit: asyncio.Semaphore | None = None

    async def before_send(self, request: httpx.Request) -> None:
        self.close()
        origin = _origin(request.url)
        if origin is None:
            return
        target = self.budget.target(origin)
        slots = self.budget.slots.get(target)
        if slots is None:
            slots = self.budget.slots[target] = OriginSlots()
        started = time.perf_counter()
        await slots.concurrent.acquire()
        self.permit = slots.concurrent
        loop = asyncio.get_running_loop()
        async with slots.start:
            delay = slots.next_start - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            slots.next_start = loop.time() + _START_SPACING
        _QUEUE_WAIT.set(_QUEUE_WAIT.get() + (time.perf_counter() - started))

    def close(self) -> None:
        if self.permit is not None:
            self.permit.release()
            self.permit = None


def request_gate() -> RequestGate | None:
    budget = _REQUEST_SLOTS.get()
    return RequestGate(budget) if budget is not None else None


async def gather(probes: Sequence[Probe], limit: int, jitter: float,
                 backends: dict[bytes, int] | None = None) -> list:
    """Spread HTTP starts and keep one backend from owning the global slots."""
    budget = BatchBudget(backends or {})
    global_slots = asyncio.Semaphore(limit) if limit > 0 else None
    http_probes = [p for p in probes if p.origin is not None]
    target_slots = {target: asyncio.Semaphore(_ORIGIN_CONCURRENCY)
                    for target in {budget.target(p.origin) for p in http_probes}}
    # Use a quarter of the shortest interval at most. A single check has no added delay.
    shortest = min((max(p.interval, 5) for p in http_probes), default=5)
    step = min(_START_SPACING, min(_MAX_SPREAD, shortest / 4) / max(len(http_probes) - 1, 1))

    async def execute(probe: Probe):
        if global_slots is None:
            return await probe.run()
        async with global_slots:
            return await probe.run()

    async def run(probe: Probe, delay: float):
        if jitter > 0:
            delay = max(delay, random.uniform(0, jitter))
        if delay > 0:
            await asyncio.sleep(delay)
        if probe.origin is None:
            return await execute(probe)
        async with target_slots[budget.target(probe.origin)]:
            return await execute(probe)

    phases = (step * i for i in range(len(http_probes)))
    jobs = [run(probe, next(phases) if probe.origin is not None else 0.0) for probe in probes]
    token = _REQUEST_SLOTS.set(budget)
    try:
        return await asyncio.gather(*jobs, return_exceptions=True)
    finally:
        _REQUEST_SLOTS.reset(token)
