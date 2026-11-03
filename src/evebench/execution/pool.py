from __future__ import annotations

import asyncio
import multiprocessing
import threading
from collections.abc import Callable
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, TypeVar

from pebble import ProcessExpired, ProcessPool

from evebench.observability import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


class BackendTaskTimeout(TimeoutError):
    """The global worker pool terminated a backend task after its deadline."""


class BackendProcessExpired(RuntimeError):
    """A global pool worker exited unexpectedly while running a backend."""

    def __init__(self, *, pid: int | None, exitcode: int) -> None:
        self.pid = pid
        self.exitcode = exitcode
        super().__init__(f"backend worker {pid} exited with code {exitcode}")


class BackendProcessPool:
    """One lazily started process pool shared by all backend adapters."""

    def __init__(
        self,
        *,
        max_workers: int,
        max_tasks: int = 25,
        start_method: str = "spawn",
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be positive")
        if max_tasks < 0:
            raise ValueError("max_tasks must be non-negative")
        self._max_workers = max_workers
        self._max_tasks = max_tasks
        self._context = multiprocessing.get_context(start_method)
        self._pool: ProcessPool | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def active(self) -> bool:
        return self._pool is not None and self._pool.active

    async def run(
        self,
        function: Callable[..., T],
        *,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
        timeout_ms: int,
        cancel_on_caller_cancel: bool = True,
    ) -> T:
        pool = self._get_pool()
        future = pool.schedule(
            function,
            args=args,
            kwargs=kwargs or {},
            timeout=timeout_ms / 1000,
        )
        wrapped = asyncio.wrap_future(future)
        try:
            return await asyncio.shield(wrapped)
        except FutureTimeoutError as exc:
            logger.warning(
                "backend task exceeded deadline",
                extra={
                    "event": "pool.task_timeout",
                    "timeout_ms": timeout_ms,
                },
            )
            raise BackendTaskTimeout(
                f"backend task exceeded {timeout_ms} ms"
            ) from exc
        except ProcessExpired as exc:
            logger.error(
                "backend worker expired",
                extra={
                    "event": "pool.worker_expired",
                    "worker_pid": exc.pid,
                    "exit_code": exc.exitcode,
                },
            )
            raise BackendProcessExpired(
                pid=exc.pid,
                exitcode=exc.exitcode,
            ) from exc
        except asyncio.CancelledError:
            if cancel_on_caller_cancel:
                future.cancel()
            raise

    def stop(self) -> None:
        """Stop workers and queued work. Safe to call more than once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            pool = self._pool
        if pool is not None:
            logger.info("stopping backend process pool", extra={"event": "pool.stopping"})
            pool.stop()
            pool.join()
            logger.info("backend process pool stopped", extra={"event": "pool.stopped"})

    def _get_pool(self) -> ProcessPool:
        with self._lock:
            if self._closed:
                raise RuntimeError("backend process pool is closed")
            if self._pool is None:
                logger.info(
                    "starting backend process pool",
                    extra={
                        "event": "pool.starting",
                        "max_workers": self._max_workers,
                        "max_tasks_per_worker": self._max_tasks,
                    },
                )
                self._pool = ProcessPool(
                    max_workers=self._max_workers,
                    max_tasks=self._max_tasks,
                    context=self._context,
                )
            return self._pool
