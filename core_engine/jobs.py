"""
Asyncio processing queue with queue position and ETA.

GPU work (NVENC render, RVC, Ollama vision) is serialised: `concurrency=1` per GPU host is the
safe default, so a 6 h upload never fights another job for VRAM. ETA uses an exponential moving
average of completed job seconds-per-source-minute, so it learns the host's real throughput.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import secrets
import time
from typing import Any, Awaitable, Callable

JobFn = Callable[["Job"], Awaitable[dict[str, Any]]]
Listener = Callable[["Job"], Awaitable[None] | None]
DEFAULT_SEC_PER_SOURCE_MIN = 6.0  # first-guess throughput before any job finished on this host
EMA_ALPHA = 0.3


@dataclass
class Job:
    job_id: str
    owner_id: str
    source_minutes: float
    fn: JobFn
    status: str = "queued"  # queued | running | done | failed | cancelled
    progress: float = 0.0
    stage: str = "queued"
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel: asyncio.Event = field(default_factory=asyncio.Event)

    def public(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id, "status": self.status, "progress": round(self.progress, 4), "stage": self.stage,
            "error": self.error, "created_at": self.created_at, "started_at": self.started_at, "finished_at": self.finished_at,
            "source_minutes": round(self.source_minutes, 2),
        }


class JobQueue:
    def __init__(self, concurrency: int = 1, listener: Listener | None = None) -> None:
        self.concurrency = max(1, int(concurrency))
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []  # queued job ids in FIFO order
        self._queue: asyncio.Queue[str] | None = None
        self._workers: list[asyncio.Task[None]] = []
        self.sec_per_source_min = DEFAULT_SEC_PER_SOURCE_MIN
        self.listener = listener

    async def start(self) -> None:
        if self._workers:
            return
        self._queue = asyncio.Queue()
        for jid in self._order:
            self._queue.put_nowait(jid)
        self._workers = [asyncio.create_task(self._worker()) for _ in range(self.concurrency)]

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    async def _notify(self, job: Job) -> None:
        if self.listener is not None:
            maybe = self.listener(job)
            if asyncio.iscoroutine(maybe):
                await maybe

    def submit(self, owner_id: str, source_minutes: float, fn: JobFn, job_id: str | None = None) -> Job:
        job = Job(job_id or secrets.token_hex(16), owner_id, max(0.0, float(source_minutes)), fn)
        self.jobs[job.job_id] = job
        self._order.append(job.job_id)
        if self._queue is not None:
            self._queue.put_nowait(job.job_id)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self.jobs.get(job_id)
        if job is None or job.status in ("done", "failed", "cancelled"):
            return False
        job.cancel.set()
        if job.status == "queued":
            job.status, job.stage, job.finished_at = "cancelled", "cancelled", time.time()
            if job_id in self._order:
                self._order.remove(job_id)
        return True

    def position(self, job_id: str) -> int | None:
        """1-based place in the waiting line; 0 while running; None when finished or unknown."""
        job = self.jobs.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return None
        if job.status == "running":
            return 0
        return self._order.index(job_id) + 1 if job_id in self._order else None

    def eta_sec(self, job_id: str) -> float | None:
        """Seconds until `job_id` finishes: remaining running work, queued work ahead / concurrency, then itself."""
        job = self.jobs.get(job_id)
        if job is None or job.status not in ("queued", "running"):
            return None
        rate = self.sec_per_source_min
        if job.status == "running":
            return round(job.source_minutes * rate * (1.0 - job.progress), 1)
        ahead = self._order[: self._order.index(job_id)] if job_id in self._order else []
        running = [j for j in self.jobs.values() if j.status == "running"]
        backlog = sum(j.source_minutes * rate * (1.0 - j.progress) for j in running)
        backlog += sum(self.jobs[a].source_minutes * rate for a in ahead)
        return round(backlog / self.concurrency + job.source_minutes * rate, 1)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        return {**job.public(), "queue_position": self.position(job_id), "eta_sec": self.eta_sec(job_id)}

    async def set_progress(self, job: Job, progress: float, stage: str) -> None:
        job.progress = min(1.0, max(job.progress, float(progress)))
        job.stage = stage
        await self._notify(job)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            jid = await self._queue.get()
            job = self.jobs.get(jid)
            if job is None or job.status != "queued":
                continue
            if jid in self._order:
                self._order.remove(jid)
            job.status, job.stage, job.started_at = "running", "starting", time.time()
            await self._notify(job)
            try:
                job.result = await job.fn(job)
                job.status, job.progress, job.stage = "done", 1.0, "done"
                elapsed = time.time() - job.started_at
                if job.source_minutes > 0:
                    sample = elapsed / job.source_minutes
                    self.sec_per_source_min = (1 - EMA_ALPHA) * self.sec_per_source_min + EMA_ALPHA * sample
            except asyncio.CancelledError:
                job.status, job.stage = "cancelled", "cancelled"
                if not job.cancel.is_set():
                    raise  # worker shutdown, not a user cancel
            except Exception as exc:  # noqa: BLE001 - job failures are data
                job.status, job.stage, job.error = "failed", "failed", str(exc)[:500]
            finally:
                job.finished_at = time.time()
                await self._notify(job)
