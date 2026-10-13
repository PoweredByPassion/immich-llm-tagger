"""Worker 线程池。

不做进程内任务队列——SQLite jobs 表就是队列。每个 worker 线程持独立
Database（SQLite 连接不可跨线程共享），循环 claim_next → 分发 → sleep。
业务编排全部在 executor；本模块只是线程载体。

client/llm 由 client_factory/llm_factory 在每次执行前构建（读 DB 当前
连接配置，配置改动热生效；连接对象也各自独立，不跨线程共享）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from app.clients.immich import ImmichClient
from app.clients.llm import LLMClient
from app.db.connection import Database
from app.engine.executor import run_job
from app.store import events, jobs
from app.store.connections import ConnectionStore

DEFAULT_POLL_SECONDS = 1.0


def default_client_factory(db: Database):
    conn = ConnectionStore(db).get_immich()
    return ImmichClient(base_url=conn.url, api_key=conn.api_key)


def default_llm_factory(db: Database):
    conn = ConnectionStore(db).get_llm()
    return LLMClient(base_url=conn.base_url, api_key=conn.api_key,
                     model=conn.model, timeout=conn.timeout,
                     max_tokens=conn.max_tokens)


class WorkerPool:
    def __init__(self, db_path: str | Path, *, size: int = 16,
                 executor=run_job, poll_seconds: float = DEFAULT_POLL_SECONDS,
                 client_factory: Callable[[Database], object] = default_client_factory,
                 llm_factory: Callable[[Database], object] = default_llm_factory,
                 on_scan_split: Callable[[int], None] | None = None):
        self._db_path = db_path
        self._size = size
        self._executor = executor
        self._poll_seconds = poll_seconds
        self._client_factory = client_factory
        self._llm_factory = llm_factory
        self._on_scan_split = on_scan_split
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        """起 size 个 daemon 线程，各自独立 Database(db_path)。"""
        self._stop.clear()
        for i in range(self._size):
            t = threading.Thread(target=self._worker, name=f"worker-{i}",
                                 daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        """置停止标志，join 所有线程。"""
        self._stop.set()
        for t in self._threads:
            t.join(timeout=10)

    def _worker(self) -> None:
        db = Database(self._db_path)  # 线程内独立连接
        while not self._stop.is_set():
            job = jobs.claim_next(db)
            if job is None:
                self._stop.wait(self._poll_seconds)
                continue
            try:
                # 每 job 现建：独立连接对象 + 连接配置热生效。
                client = self._client_factory(db)
                llm = self._llm_factory(db)
                self._executor(db, client, llm, job,
                               on_scan_split=self._on_scan_split)
            except Exception as exc:  # noqa: BLE001 —— 执行器异常不能杀死 worker
                events.log_event(db, "system", "job_error", {
                    "job_id": job.id, "job_type": job.type,
                    "error": f"worker: {exc}",
                })
                jobs.finish_job(db, job.id, error=str(exc))
