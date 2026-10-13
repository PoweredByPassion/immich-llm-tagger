"""FastAPI 应用装配。

lifespan 中的顺序是**负载承载**的，不是风格选择：
1. Database(cfg.db_path)
2. migrate(db)   ← 必须第一个真正打开库：单线程把 fresh 库切成 WAL
3. seed_from_env(db, cfg)
在非 WAL 库上，多连接并发执行 `PRAGMA journal_mode = WAL` 会间歇性
`database is locked` 且 busy_timeout 无效（见 app/db/connection.py）。
worker 并发进来之前，migrate() 已单线程完成切换。
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from app.config import BootstrapConfig
from app.db.connection import Database
from app.db.migrate import migrate
from app.engine.pool import WorkerPool
from app.engine.scheduler import PollScheduler
from app.store import events, jobs
from app.store.seed import seed_from_env
from app.store.settings import SettingsStore
from app.web import auth, i18n
from app.web.i18n import lang_from_cookie, t
from app.web.routes import config_conn, config_poll, config_prompt, config_tags, health, hooks, jobs_ui, overview, usage_ui

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
_STATIC_DIR = Path(__file__).resolve().parent / "static"

# worker 数固定（spec：分类并发 = 5），不再读 llm_conn.concurrency。
DEFAULT_WORKER_SIZE = 5


def create_app(
    data_dir: Path | None = None,
    bootstrap: BootstrapConfig | None = None,
) -> FastAPI:
    if bootstrap is None:
        bootstrap = BootstrapConfig.from_env()
    if data_dir is not None:
        bootstrap = _with_data_dir(bootstrap, Path(data_dir))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db = Database(bootstrap.db_path)
        migrate(db)
        seed_from_env(db, bootstrap)
        app.state.db = db
        app.state.bootstrap = bootstrap

        # 孤儿 job 恢复：上次进程退出时 running 的 job 已无 worker 承载，
        # 不恢复会永久卡住轮询调度（has_active_job_of_type 恒真）。
        # 先清掉孤儿 classify_item 子任务（worker 尚未启动，此刻任何子任务都是
        # 孤儿；父 scan 会被重新入队并重拆，从根上消除重复子任务），再重新入队
        # running job——顺序不可颠倒。重新入队而非标记失败：scan 的 _already_done
        # 会自动跳过已完成的资产，重跑只补剩余的（详见 store/jobs.py docstring）。
        dropped = jobs.drop_orphan_children(db)
        resumed = jobs.requeue_orphan_running_jobs(db)
        if dropped:
            events.log_event(db, "system", "orphan_children_dropped",
                             {"count": dropped})
        if resumed:
            events.log_event(db, "system", "jobs_resumed", {
                "job_ids": resumed,
                "count": len(resumed),
                "reason": "进程重启时任务中断，已重新入队续跑",
            })

        def _scan_coordinator_spawner(parent_job_id: int) -> None:
            # 协调器线程不占 worker 池线程（spec：分类并发 = worker 数）。
            # 它在 scan 拆分成功后被调用；resumed 的 scan 会在 worker 重跑该
            # scan 时触发（on_scan_split），不在这里起（否则只会空转返回）。
            from app.engine.executor import start_scan_coordinator
            start_scan_coordinator(bootstrap.db_path, parent_job_id)

        pool = WorkerPool(bootstrap.db_path, size=DEFAULT_WORKER_SIZE,
                          on_scan_split=_scan_coordinator_spawner)
        pool.start()

        def enqueue_scan() -> None:
            # 独立 Database 实例（连接不可跨线程复用，scheduler 线程内自建）
            jobs.create_job(Database(bootstrap.db_path), "scan",
                            params={"source": "poll"})

        scheduler = PollScheduler(
            bootstrap.db_path,
            enqueue_scan,
            interval_getter=lambda: int(SettingsStore(db).get("poll_interval_seconds")),
            enabled_getter=lambda: bool(SettingsStore(db).get("poll_enabled")),
        )
        scheduler.start()
        # 启动时触发一次 retention 清理（maybe_purge 自管理 24h 间隔）
        try:
            scheduler.maybe_purge()
        except Exception:
            pass  # 清理失败不阻塞启动

        app.state.pool = pool
        app.state.scheduler = scheduler
        try:
            yield
        finally:
            scheduler.stop()
            pool.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.templates = Jinja2Templates(directory=_TEMPLATES_DIR)
    # 必须在首次模板编译之前装全局：Jinja 在编译期决定 pass_context 绑定
    i18n.install(app.state.templates)

    def _job_duration(job) -> str:
        """列表页耗时列：started_at → finished_at（无 finished 显示运行中）。"""
        from datetime import datetime

        def _ts(s):
            return datetime.strptime(s, "%Y-%m-%d %H:%M:%S") if s else None
        started = _ts(job.started_at)
        if started is None:
            return "-"
        end = _ts(job.finished_at) or datetime.utcnow()
        return f"{max((end - started).total_seconds(), 0.0):.0f}s"

    app.state.templates.env.globals["job_duration"] = _job_duration

    def _fmt_tokens(value) -> str:
        """token 数量级缩写：<1000 原样；≥1000 显示 12.3K / 1.2M（title 保留精确值由模板处理）。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            return str(value or 0)
        if abs(n) < 1000:
            return str(n)
        if abs(n) < 1_000_000:
            return f"{n / 1000:.1f}K"
        if abs(n) < 1_000_000_000:
            return f"{n / 1_000_000:.1f}M"
        return f"{n / 1_000_000_000:.1f}B"

    app.state.templates.env.filters["fmt_tokens"] = _fmt_tokens

    if _STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

    app.include_router(health.router)
    app.include_router(hooks.router)
    # config_conn.router 自带 prefix="/config"（占位契约），此处不再叠加前缀
    app.include_router(config_conn.router)
    app.include_router(config_tags.router)
    app.include_router(config_poll.router)
    app.include_router(overview.router)
    app.include_router(config_prompt.router)
    app.include_router(jobs_ui.router)
    app.include_router(jobs_ui.sse_router)
    from app.web.routes import (
        assets_ui,
        config_exclusions,
        config_markers,
        config_scope,
        config_transfer,
        danger_ui,
    )
    app.include_router(config_scope.router)
    app.include_router(config_exclusions.router)
    app.include_router(config_markers.router)
    app.include_router(config_transfer.router)
    app.include_router(danger_ui.router)
    app.include_router(assets_ui.router)
    app.include_router(usage_ui.router)

    # ---- 单密码访问认证（Task 5）----
    # 未设 AUTH_PASSWORD → 不注册中间件，全部放行（本地部署零摩擦）。
    # 关键 None 守卫：不判空会在默认部署下用 None 建中间件，密码校验必然出错。
    if bootstrap.auth_password:
        app.middleware("http")(auth.make_middleware(bootstrap.auth_password))

    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page(request: Request):
        return request.app.state.templates.TemplateResponse(
            request, "login.html", {"error": None}
        )

    @app.post("/login", include_in_schema=False)
    async def login_submit(request: Request):
        form = await request.form()
        supplied = str(form.get("password", ""))
        password = bootstrap.auth_password
        if password and not auth.password_matches(supplied, password):
            # 抗暴力破解：失败固定延迟后再渲染错误页。
            await asyncio.sleep(auth.LOGIN_DELAY_SECONDS)
            error = t("auth.wrong", lang_from_cookie(request.cookies))
            return request.app.state.templates.TemplateResponse(
                request, "login.html", {"error": error}, status_code=200
            )
        # 成功；未启用认证时登录无意义，直接放行（不种 cookie）。
        resp = RedirectResponse("/", status_code=303)
        if password:
            resp.set_cookie(
                auth.COOKIE_NAME,
                auth.make_token(password),
                max_age=auth.SESSION_TTL,
                httponly=True,
                samesite="lax",
                path="/",
                secure=auth.request_is_secure(request),
            )
        return resp

    return app


def _with_data_dir(bootstrap: BootstrapConfig, data_dir: Path) -> BootstrapConfig:
    return replace(bootstrap, data_dir=str(data_dir))
