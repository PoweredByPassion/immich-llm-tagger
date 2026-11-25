"""Worker 线程池。

不做进程内任务队列——SQLite jobs 表就是队列。每个 worker 线程持独立
Database（SQLite 连接不可跨线程共享），循环 claim_next → 分发 → sleep。
业务编排全部在 executor；本模块只是线程载体。

client/llm 由 client_factory/llm_factory 在每次执行前构建（读 DB 当前
连接配置，配置改动热生效；连接对象也各自独立，不跨线程共享）。
"""
from __future__ import annotations

import logging
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
from app.store.settings import SettingsStore

DEFAULT_POLL_SECONDS = 1.0
logger = logging.getLogger(__name__)


def default_client_factory(db: Database):
    conn = ConnectionStore(db).get_immich()
    return ImmichClient(base_url=conn.url, api_key=conn.api_key, verify_ssl=conn.verify_ssl)


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
        self._retry_lock = threading.Lock()
        self._next_retry_check = 0.0

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
            t.join()
        self._threads.clear()

    def _worker(self) -> None:
        db = Database(self._db_path)
        client = llm = None
        immich_connection = llm_connection = None
        try:
            while not self._stop.is_set():
                try:
                    job = jobs.claim_next(db)
                    if job is None:
                        self._enqueue_due_retries(db)
                        self._stop.wait(self._poll_seconds)
                        continue
                    try:
                        store = ConnectionStore(db)
                        new_immich = store.get_immich() if self._client_factory is default_client_factory else None
                        if client is None or new_immich != immich_connection or new_immich is None:
                            _close(client)
                            client = self._client_factory(db)
                            immich_connection = new_immich

                        model = None
                        if job.type in {"single", "classify_item", "dryrun", "reclassify"}:
                            new_llm = store.get_llm() if self._llm_factory is default_llm_factory else None
                            if llm is None or new_llm != llm_connection or new_llm is None:
                                _close(llm)
                                llm = self._llm_factory(db)
                                llm_connection = new_llm
                            model = llm
                        self._executor(db, client, model, job, on_scan_split=self._on_scan_split)
                    except Exception as exc:  # noqa: BLE001
                        jobs.finish_job(db, job.id, error=str(exc))
                        try:
                            events.log_event(db, "system", "job_error", {
                                "job_id": job.id, "job_type": job.type, "error": f"worker: {exc}",
                            })
                        except Exception:  # noqa: BLE001
                            logger.debug("Could not record worker failure", exc_info=True)
                except Exception:  # noqa: BLE001
                    logger.debug("Worker scheduling failed", exc_info=True)
                    self._stop.wait(self._poll_seconds)
        finally:
            _close(client)
            _close(llm)

    def _enqueue_due_retries(self, db):
        now = time.monotonic()
        with self._retry_lock:
            if now < self._next_retry_check:
                return
            self._next_retry_check = now + 5
        with db.connect() as conn:
            settings = SettingsStore(db).get_all(conn)
            allow_analysis = (settings["poll_enabled"] or settings["webhook_enabled"]) and not settings["llm_paused_reason"]
            rows = conn.execute(
                "SELECT asset_id FROM asset_state WHERE process_status='pending' "
                "AND (retry_after IS NULL OR retry_after <= datetime('now')) "
                "AND (? OR EXISTS (SELECT 1 FROM pending_effects p WHERE p.asset_id=asset_state.asset_id)) "
                "ORDER BY retry_after LIMIT 25", (allow_analysis,)
            ).fetchall()
        for row in rows:
            jobs.enqueue_auto_job(db, "single", {
                "asset_id": row["asset_id"], "source": "poll", "recover_pending": True,
            })


def _close(resource):
    close = getattr(resource, "close", None)
    if close is not None:
        try:
            close()
        except Exception:  # noqa: BLE001
            logger.warning("Failed to close upstream client", exc_info=True)
