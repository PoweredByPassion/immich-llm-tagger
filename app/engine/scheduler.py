"""轮询调度器。

独立 daemon 线程按固定小步 sleep；每 tick 现读 enabled/interval（配置热生效），
到点且无活跃 scan 时触发 enqueue_scan。

并发/时序正确性全部收敛在纯函数 decide_tick（单线程可测），
线程只是载体；线程内的时间源可注入（_clock）。
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from app.store import events, jobs

DEFAULT_TICK_STEP = 5.0

# scan_skip_active 的最小记录间隔（秒）。卡死状态下（孤儿 running job
# 阻塞轮询）这个事件每 tick 都满足条件，不节流会在几小时内灌出上千条
# 同内容事件，把真正有用的事件流淹掉。保留节流后的记录——它是用户
# 发现"轮询不触发"的线索。
SKIP_ACTIVE_LOG_INTERVAL = 300.0


def decide_tick(
    *,
    now: float,
    last_run: float | None,
    interval: float,
    enabled: bool,
    has_active: bool,
) -> bool:
    """决定本 tick 是否触发 enqueue_scan。

    - disabled 永不触发（即使从未跑过）
    - 未到点（now - last_run < interval）不触发
    - 已有活跃 scan（queued/running）不触发
    """
    if not enabled:
        return False
    if last_run is not None and now - last_run < interval:
        return False
    if has_active:
        return False
    return True


class PollScheduler:
    def __init__(
        self,
        db_path: Path | str,
        enqueue_scan: Callable[[], None],
        *,
        interval_getter: Callable[[], float],
        enabled_getter: Callable[[], bool],
        tick_step: float = DEFAULT_TICK_STEP,
    ) -> None:
        self._db_path = db_path
        self._enqueue_scan = enqueue_scan
        self._interval_getter = interval_getter
        self._enabled_getter = enabled_getter
        self._tick_step = tick_step
        self._clock: Callable[[], float] = time.monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_run: float | None = None
        self._last_skip_log: float | None = None

    # ---------- 单线程可测的 tick 逻辑 ----------

    def maybe_tick(self, now: float) -> bool:
        """执行一次 tick。now 由调用方提供（测试 mock 时间）。

        返回是否触发了 enqueue_scan。stop() 后恒为 False 且不触碰 DB。
        """
        if self._stop.is_set():
            return False
        last_run = self._last_run
        from app.db.connection import Database

        db = Database(self._db_path)
        enabled = self._enabled_getter()
        interval = self._interval_getter()
        has_active = jobs.has_active_job_of_type(db, "scan")
        time_due = last_run is None or now - last_run >= interval
        due = decide_tick(
            now=now,
            last_run=self._last_run,
            interval=interval,
            enabled=enabled,
            has_active=has_active,
        )
        if not due:
            if enabled and time_due and has_active:
                # 节流：同一阻塞状态最多每 SKIP_ACTIVE_LOG_INTERVAL 记一条
                if (self._last_skip_log is None
                        or now - self._last_skip_log >= SKIP_ACTIVE_LOG_INTERVAL):
                    self._last_skip_log = now
                    events.log_event(
                        db, "poll", "scan_skip_active",
                        {"reason": "active scan exists"},
                    )
            return False
        # 收紧竞态窗口：enabled 检查到创建 job 之间还有 DB 读，
        # 全局暂停可能在期间被设置 —— 创建 job 前再查一次。
        if not self._enabled_getter():
            return False
        self._last_run = now
        self._enqueue_scan()
        return True

    # ---------- retention：24h 间隔自管理 ----------

    def maybe_purge(
        self,
        now: float | None = None,
        purge: Callable[[object], dict] | None = None,
    ) -> dict | None:
        """距上次清理 >24h 时执行一次 purge_expired。

        last_retention_run 存 settings（epoch 秒，float）；启动时 last 为
        None 表示从未跑过，立即执行。purge/now 参数供测试注入。
        返回 purge 结果，未触发返回 None。
        """
        from app.db.connection import Database
        from app.store.retention import purge_expired as _purge_expired
        from app.store.settings import SettingsStore

        if self._stop.is_set():
            return None
        now = time.time() if now is None else now
        purge = purge if purge is not None else _purge_expired
        db = Database(self._db_path)
        settings = SettingsStore(db)
        last = settings.get("last_retention_run")
        if last is not None and now - last < 24 * 3600:
            return None
        result = purge(db)
        settings.set("last_retention_run", now)
        return result

    # ---------- 线程载体 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="poll-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join()
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.maybe_tick(now=self._clock())
            except Exception:
                # 单次 tick 异常（DB 忙、配置读失败等）不杀死调度线程；
                # 继续下一 tick 重试。
                pass
            try:
                # 周期清理：maybe_purge 内部有 24h 间隔判断
                # （last_retention_run），每 tick 调用成本仅一次
                # settings 读；异常不杀死调度循环。
                self.maybe_purge()
            except Exception:
                pass
            self._stop.wait(self._tick_step)
