"""Bounded, shared, single-flight compute jobs for interactive radar requests."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import ctypes
import fcntl
import gc
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import psutil
from pipeline.resource_budget import service_cgroup


class QueueBusy(Exception):
    """The bounded queue or its waiting-time budget has been exhausted."""


class HeavyJobQueue:
    """Use file leases to bound work and reuse results across API workers.

    Awaiters are shielded so a disconnected client cannot release a lease
    while its computation is still running. Only the two leased jobs enter
    the compute executor; waiting requests retain no volume/image arrays.
    """

    def __init__(self, directory: Path, workers: int = 2, max_pending: int = 32,
                 wait_seconds: float = 120, cache_mb: int = 128) -> None:
        self.directory = directory
        self.workers = workers
        self.max_pending = max_pending
        self.wait_seconds = wait_seconds
        self.cache_bytes = cache_mb * 1024 ** 2
        self._tasks: dict[str, asyncio.Task] = {}
        self._executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="radar-request")
        self._last_cleanup = 0.0
        self._cgroup = service_cgroup()

    async def run(self, key: str, function, slots: int = 1):
        digest = hashlib.sha256(key.encode()).hexdigest()
        task = self._tasks.get(digest)
        if task is None:
            if len(self._tasks) >= self.max_pending:
                raise QueueBusy("Radar processing is busy; try again shortly.")
            task = asyncio.create_task(self._run(digest, function, max(1, min(slots, self.workers))))
            self._tasks[digest] = task
            def finished(done):
                self._tasks.pop(digest, None)
                # Retrieve failures even if all original clients disconnected.
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
        return await asyncio.shield(task)

    def _read_cache(self, path: Path):
        try:
            if time.time() - path.stat().st_mtime <= 1800:
                return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        return None

    async def _lease(self, path: Path, deadline: float):
        handle = path.open("a+")
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return handle
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise QueueBusy("Radar processing is busy; try again shortly.")
                    await asyncio.sleep(.1)
        except BaseException:
            handle.close()
            raise

    def _has_memory(self, reservations: dict) -> bool:
        """Count each queued decode once, including work in other API workers."""
        if self._cgroup is None:
            return True
        try:
            high = (self._cgroup / "memory.high").read_text().strip()
            if high == "max":
                return True
            current = int((self._cgroup / "memory.current").read_text())
            stats = {line.split()[0]: int(line.split()[1])
                     for line in (self._cgroup / "memory.stat").read_text().splitlines()}
            by_process = {}
            for job in reservations.values():
                pid = job.get("pid")
                group = by_process.setdefault(pid, {"reserved": 0, "baseline": float("inf")})
                group["reserved"] += 1536 * 1024 ** 2
                group["baseline"] = min(group["baseline"], job.get("baseline_rss", 0))
            remaining = 0
            for pid, group in by_process.items():
                allocated = 0
                try:
                    if pid is not None:
                        allocated = max(0, psutil.Process(pid).memory_info().rss - group["baseline"])
                except (psutil.Error, TypeError):
                    pass
                # RSS growth is counted once per process, even when its two
                # executor threads own different jobs.
                remaining += max(0, group["reserved"] - allocated)
            working = max(0, current - stats.get("inactive_file", 0))
            return working + remaining + (1536 + 128) * 1024 ** 2 <= int(high)
        except (OSError, ValueError):
            return True

    async def _slot(self, deadline: float, slots: int):
        while True:
            allocation = await self._lease(self.directory / "allocation.lock", deadline)
            handles = []
            accepted = False
            try:
                reservations = {}
                for index in range(self.workers):
                    handle = (self.directory / f"slot-{index}.lock").open("a+")
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        handles.append(handle)
                    except BlockingIOError:
                        try:
                            handle.seek(0)
                            job = json.load(handle)
                            reservations[job["id"]] = job
                        except (ValueError, KeyError, TypeError):
                            reservations[f"unknown-{index}"] = {"pid": None, "baseline_rss": 0}
                        handle.close()
                if len(handles) >= slots and self._has_memory(reservations):
                    job = {"id": uuid.uuid4().hex, "pid": os.getpid(),
                           "baseline_rss": psutil.Process().memory_info().rss}
                    for handle in handles[:slots]:
                        handle.seek(0)
                        handle.truncate()
                        json.dump(job, handle)
                        handle.flush()
                    accepted = True
                    return handles[:slots]
            finally:
                for handle in handles[slots:] if accepted else handles:
                    handle.close()
                allocation.close()
            if time.monotonic() >= deadline:
                raise QueueBusy("Radar processing is busy; try again shortly.")
            await asyncio.sleep(.1)

    async def _run(self, digest: str, function, slots: int):
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{digest}.json"
        cached = self._read_cache(path)
        if cached is not None:
            return cached
        deadline = time.monotonic() + self.wait_seconds
        # A bounded number of lock buckets prevents unbounded inode growth.
        lease = await self._lease(self.directory / f"job-{digest[:3]}.lock", deadline)
        slot = None
        try:
            cached = self._read_cache(path)
            if cached is not None:
                return cached
            slot = await self._slot(deadline, slots)
            return await asyncio.get_running_loop().run_in_executor(
                self._executor, self._compute, path, function,
            )
        finally:
            if slot is not None:
                for handle in slot:
                    handle.close()
            lease.close()

    def _compute(self, path: Path, function):
        try:
            result = function()
            if result is not None:
                temporary = path.with_suffix(".tmp")
                temporary.write_text(json.dumps(result))
                temporary.replace(path)
                self._cleanup()
            return result
        finally:
            gc.collect()
            try:
                ctypes.CDLL("libc.so.6").malloc_trim(0)
            except OSError:
                pass

    def _cleanup(self) -> None:
        if time.monotonic() - self._last_cleanup < 60:
            return
        self._last_cleanup = time.monotonic()
        files = []
        for path in self.directory.glob("*.json"):
            try:
                stat = path.stat()
                files.append((stat.st_mtime, stat.st_size, path))
            except OSError:
                continue
        total = sum(size for _, size, _ in files)
        for timestamp, size, path in sorted(files):
            if time.time() - timestamp > 1800 or total > self.cache_bytes:
                path.unlink(missing_ok=True)
                total -= size

    async def close(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._executor.shutdown(wait=True)
