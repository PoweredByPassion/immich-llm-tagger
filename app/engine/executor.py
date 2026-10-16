"""Job 执行器——单 job 的编排逻辑，无线程，可单线程测。

**scan 拆分语义**：scan 只负责发现候选并过滤"已完成"，随后把每张图拆成一条
`classify_item` 子任务；逐图分类由 worker 池并发执行。子任务的进度与记录都记到
**父 scan job**（`incr_progress`/事件用父 id，`classify_asset` 传 `job_id=父 id`），
父 job 由协调器线程在子任务全部终态后收尾（`finish_scan_parent`）。

**total 语义定义**（scan job）：`total` = 扫描发现的候选数（含"已完成"被跳过
的）。`done` / `failed` 只累计实际调了管线的图；"已完成"过滤跳过的不计入
done 也不计入 failed。

**取消语义**：running 状态的取消只是置 cancel_requested。scan 的拆分循环在每张
之间轮询 `is_canceled`，取消则停止建新子任务；协调器收尾时先 `cancel_children_of`
清掉排队中的子任务，父 job 最终 `finish_job`（done）。"canceled" 终态只发生在
queued 阶段被 `cancel_job` 直接置（那是入库层的事，executor 不管）。
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from app.classify.undo import UndoSession
from app.classify.pipeline import (
    PipelineConfig,
    PipelineResult,
    classify_asset,
)
from app.classify.reclassify import reclassify_asset
from app.classify.scope import (
    iter_scope_assets,
    load_scope_rules,
    scan_queue_assets,
)
from app.clients.immich import ImmichError
from app.db.connection import Database
from app.store import events, jobs
from app.store.connections import ConnectionStore
from app.store.settings import SettingsStore
from app.store.tags import load_tags

# 内置默认模板；active_prompt_version_id
# 为 None 时使用它。必须包含 MANDATORY_PLACEHOLDER（{{TAG_LIST}}）。
DEFAULT_PROMPT_TEMPLATE = (
    "You are a photo tagging assistant. Classify the screenshot into exactly "
    "one tag from the list below.\n\n"
    "Tags:\n{{TAG_LIST}}\n\n"
    "File: {{FILENAME}}\n"
    "Date: {{DATE}}\n\n"
    "First describe what you actually see, then classify, then rate. "
    'Respond with JSON: {"description": "<one sentence>", "tag": "<tag>", '
    '"confidence": <0-1>, "sensitive": <bool>, "reason": "<short>", '
    '"rating": <1-5>}\n'
    "If none fits, use the fallback tag. If this is not a screenshot or the "
    "content is sensitive, refuse."
)

_UNKNOWN_TYPES = ("backfill", "undo", "reset", "reclassify")

# scan 拆分后的协调器：轮询子任务直到全部终态，再收尾父 scan。不占用 worker
# 池线程（spec：分类并发 = worker 数）。
SCAN_COORD_POLL_SECONDS = 0.5
SCAN_COORDINATOR_TIMEOUT = 6 * 3600   # 协调器最长等待，防僵尸线程
SCAN_COORD_ERROR_LOG_INTERVAL = 60.0  # 协调器错误日志节流（同 scheduler）


def build_pipeline_config(db: Database) -> PipelineConfig:
    """从 DB 读配置快照：settings + tags + prompt 模板 + llm conn。

    tags 为空时 tag_list 空——渲染出的 prompt 无标签，LLM 会拒答或选
    fallback，这是可接受的降级。
    """
    settings = SettingsStore(db)
    conns = ConnectionStore(db)
    tags = load_tags(db)

    pv_id = settings.get("active_prompt_version_id")
    template = DEFAULT_PROMPT_TEMPLATE
    if pv_id is not None:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT body FROM prompt_versions WHERE id = ?", (pv_id,)
            ).fetchone()
        if row is not None:
            template = row["body"]
    else:
        # 未激活任何版本时回退**最新版本**；表完全为空才用
        # 内置 DEFAULT_PROMPT_TEMPLATE。
        with db.connect() as conn:
            row = conn.execute(
                "SELECT body FROM prompt_versions "
                "ORDER BY created_at DESC, id DESC LIMIT 1"
            ).fetchone()
        if row is not None:
            template = row["body"]

    marker_rules = _load_marker_rules(db)

    return PipelineConfig(
        prompt_template=template,
        tags=tags,
        marker_rules=marker_rules,
        fallback_tag=settings.get("fallback_tag_name"),
        default_min_confidence=settings.get("default_min_confidence"),
        thumb=_thumb_config(settings),
        queue_tag_name=settings.get("queue_tag_name"),
        llm=conns.get_llm(),
        concurrency_owner=_owner_id(db),
    )


def _thumb_config(settings: SettingsStore):
    from app.classify.thumb import ThumbConfig
    return ThumbConfig(preset=settings.get("thumb_preset"),
                       quality=settings.get("thumb_quality"))


def _load_marker_rules(db: Database) -> list[tuple[str, dict]]:
    """marker_rules 表 → [(marker_tag_name, condition_dict)]。"""
    import json

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT t.name, r.condition FROM marker_rules r "
            "JOIN tags t ON t.id = r.marker_tag_id "
            "WHERE r.enabled = 1 ORDER BY r.id"
        ).fetchall()
    rules: list[tuple[str, dict]] = []
    for name, cond in rows:
        try:
            rules.append((name, json.loads(cond)))
        except (TypeError, ValueError):
            continue
    return rules


def _owner_id(db: Database | None = None) -> int:
    """租约 owner：hostname + pid + thread ident 的稳定 hash。

    同一进程的多个 worker 线程、同主机的多个进程（多容器共享 DB），
    各自拿到不同 owner——lease 的互斥语义才成立（BUG-2 回归）。
    崩溃 worker 的租约只能靠 lease_expires_at 过期回收，这是设计本意。

    db 形参保留以最小化调用点 diff，当前不使用。
    """
    import hashlib
    import os
    import socket
    import threading

    raw = f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"
    return int.from_bytes(hashlib.sha256(raw.encode()).digest()[:4], "big")


# ------------------------------------------------------------------ events


def _log_job_event(db: Database, event_type: str, job: jobs.JobRow,
                   **payload) -> None:
    events.log_event(db, "system", event_type, {"job_id": job.id,
                                                "job_type": job.type, **payload})


# ---------------------------------------------------------------- executor


def run_single_job(db: Database, client, llm, job: jobs.JobRow,
                   cfg: PipelineConfig) -> None:
    """single / dryrun：处理 params 里的一张图。任何未预期异常都收敛为
    finish_job(error) —— worker 循环不能被单 job 炸掉。"""
    try:
        _run_single_job(db, client, llm, job, cfg)
    except Exception as exc:  # noqa: BLE001 —— 失败模式 5
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_single_job(db, client, llm, job, cfg) -> None:
    params = job.params
    asset_id = params["asset_id"]
    source = params.get("source", "manual")
    dry_run = job.type == "dryrun" or bool(params.get("dry_run"))
    # 沙盒试跑允许携带编辑框里的草稿模板（未保存版本也能试跑）
    if dry_run and params.get("prompt_body"):
        from dataclasses import replace as _dc_replace
        cfg = _dc_replace(cfg, prompt_template=str(params["prompt_body"]))

    _log_job_event(db, "job_started", job, asset_id=asset_id)
    result = classify_asset(
        db, client, llm, asset_id, cfg,
        source=source, dry_run=dry_run, job_id=job.id,
        prompt_version_id=job.prompt_version_id,
        asset_hint=params.get("asset_hint"),
    )
    jobs.incr_progress(db, job.id, failed=(result.status == "error"))
    _record_result(db, job.id, result, result.dry_run)
    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, asset_id=asset_id)


def _record_result(db: Database, job_id: int, result: PipelineResult,
                   dry_run: bool) -> None:
    """按结果写 asset 事件（计数由调用方负责——需与 incr_progress 分离，
    才能保证「计数恰好一次」，见 run_classify_item 的 counted 守卫）。"""
    if result.status == "ok":
        events.log_event(db, "system", "asset_ingested", {
            "job_id": job_id, "asset_id": result.asset_id, "tag": result.tag,
            "confidence": result.confidence, "dry_run": dry_run,
        })
    elif result.status == "refused":
        events.log_event(db, "system", "asset_skipped", {
            "job_id": job_id, "asset_id": result.asset_id,
            "markers": list(result.markers), "dry_run": dry_run,
        })
    else:
        events.log_event(db, "system", "job_error", {
            "job_id": job_id, "asset_id": result.asset_id,
            "error": "classification failed", "dry_run": dry_run,
        })


def run_scan_job(db: Database, client, llm, job: jobs.JobRow,
                 cfg_factory: Callable[[], PipelineConfig]) -> None:
    """scan：发现 → 过滤"已完成" → 拆成逐图 classify_item 子任务（不再逐张分类）。
    异常收敛为 finish_job(error)，父 job 失败即不产生子任务。"""
    try:
        _run_scan_job(db, client, llm, job, cfg_factory)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_scan_job(db: Database, client, llm, job: jobs.JobRow,
                  cfg_factory: Callable[[], PipelineConfig]) -> None:
    """scan 新语义：发现 → 过滤"已完成" → 拆成逐图 classify_item 子任务。

    逐图分类由 worker 池并发执行（子任务各写父 job 的进度/记录）；父 job
    由协调器线程在子任务全部终态后收尾（见 finish_scan_parent）。scan 自身
    不再直接调管线。"""
    rules = load_scope_rules(db)
    # cfg 快照：scan 开始时读一次，用于队列标签名与"已完成"自愈。
    cfg = cfg_factory()
    candidates = scan_queue_assets(client, db, cfg.queue_tag_name, rules)

    # total = 这个 job 累计要处理的总张数。定义见模块 docstring。
    #
    # 首轮：done=0，total = 本轮候选数。
    # 续跑（job 被 requeue_orphan_running_jobs 重新入队）：上一轮已处理
    # 685 张且它们的队列标签已摘除，本轮只扫到剩余部分。若直接覆盖 total，
    # 进度会从 "685/722" 退化成 "722/37"——所以要把已完成的加回去。
    # job.done 是 claim_next 之后从库里读的当前值，首轮恒为 0。
    total = len(candidates) + job.done
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = ? WHERE id = ?",
                     (total, job.id))

    _log_job_event(db, "job_started", job, total=total)

    split = 0
    for asset_id in candidates:
        # 取消检查在每张之间：不再为新图建子任务（已建的由协调器清理）
        if jobs.is_canceled(db, job.id):
            break
        if _already_done(db, asset_id):
            # 自愈：已完成但仍挂在队列标签上（历史 untag 假成功的遗留），
            # 补一次摘标签；失败只记事件不中断 scan。
            _heal_queue_tag(db, client, asset_id, cfg.queue_tag_name)
            continue
        jobs.create_job(db, "classify_item", {
            "asset_id": asset_id,
            "parent_job_id": job.id,
            "source": job.params.get("source", "poll"),
        })
        split += 1

    events.log_event(db, "system", "scan_split",
                     {"job_id": job.id, "split": split})


def run_classify_item(db: Database, client, llm, job: jobs.JobRow,
                      cfg_factory: Callable[[], PipelineConfig]) -> None:
    """classify_item：分类单张图。进度记到**父 job**；传给管线的 job_id 是
    父 id（change_log / asset_records / asset_state 都归父——undo 按钮与
    失败清单才能继续工作）。cfg 每 job 读一次 = 配置热生效。

    计数恰好一次：``counted`` 守卫保证 _record_result（写事件）抛错时不会在
    except 里重复 incr_progress——否则 done+failed 会超过 total。

    error 结果**一律**先 sleep 1s 再复查 _already_done（不只租约冲突：管线
    无法区分租约冲突与真实失败），因此真实失败（LLM/Immich 挂）也多付 1s。
    两个兄弟子任务并发分类同一张图时二者都可能 incr 父计数 → done 可能短暂
    超过 total（子任务级去重是 split 层的事，这里不做全局去重）。"""
    parent_id: int | None = None
    counted = False
    try:
        parent_id = int(job.params["parent_job_id"])
        asset_id = job.params["asset_id"]
        cfg = cfg_factory()
        if _already_done(db, asset_id):        # 幂等二次确认
            # 已完成却仍挂着队列标签（拆分后、本子任务执行前被并发副本抢先
            # 处理且其 untag 失败，或历史 untag 假成功的遗留）→ 补摘标签。
            # 否则该图会永久残留「未分类」标签，被后续每轮 scan 反复发现。
            _heal_queue_tag(db, client, asset_id, cfg.queue_tag_name)
            jobs.incr_progress(db, parent_id)
            counted = True
            jobs.finish_job(db, job.id)
            return
        result = classify_asset(
            db, client, llm, asset_id, cfg,
            source=job.params.get("source", "poll"),
            dry_run=False,
            job_id=parent_id,
            prompt_version_id=_active_prompt_version_id(db),
        )
        if result.status == "error":
            # 可能是并发副本持有租约（claim 失败即返回 error）：稍候复查。
            # 若这段时间里该图已被并发副本跑完（_already_done 变真），计 done
            # 而非 failed——否则同一张图会被两个子任务各记一次失败。
            time.sleep(1.0)
            if _already_done(db, asset_id):
                jobs.incr_progress(db, parent_id)
                counted = True
                jobs.finish_job(db, job.id)
                return
        jobs.incr_progress(db, parent_id, failed=(result.status == "error"))
        counted = True
        _record_result(db, parent_id, result, dry_run=False)
        jobs.finish_job(db, job.id)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        # parent_id 未解析出来（params 畸形）则无法归因父 job；否则计一次失败。
        if parent_id is not None and not counted:
            jobs.incr_progress(db, parent_id, failed=True)
        jobs.finish_job(db, job.id, error=str(exc))


def _active_prompt_version_id(db: Database) -> int | None:
    """执行时解析"本次实际渲染所用"的 prompt 版本 id，写进 asset_records。

    必须与 build_pipeline_config 的取值规则一致，否则会记录一个"其 body 并未
    被渲染"的版本：
    - active 为 None → 回退最新版本（无版本 → None）。
    - active 有值且行存在 → 该 id。
    - active 有值但行缺失（悬空）→ None：build_pipeline_config 此时回退内置
      DEFAULT_PROMPT_TEMPLATE（**不是**最新版本），故没有版本 id 可记。"""
    settings = SettingsStore(db)
    pv_id = settings.get("active_prompt_version_id")
    with db.connect() as conn:
        if pv_id is not None:
            row = conn.execute("SELECT id FROM prompt_versions WHERE id = ?",
                               (pv_id,)).fetchone()
            return row["id"] if row is not None else None
        row = conn.execute(
            "SELECT id FROM prompt_versions "
            "ORDER BY created_at DESC, id DESC LIMIT 1").fetchone()
    return row["id"] if row is not None else None


def finish_scan_parent(db: Database, job_id: int) -> None:
    """协调器收尾：没有未终态子任务 → 结束父 job；取消时先清掉排队中的子任务。
    幂等：父 job 非 running 时直接返回。"""
    job = jobs.get_job(db, job_id)
    if job is None or job.status != "running":
        return
    if jobs.is_canceled(db, job_id):
        jobs.cancel_children_of(db, job_id)
    if jobs.pending_children(db, job_id) == 0:
        jobs.finish_job(db, job_id)
        events.log_event(db, "system", "job_complete",
                         {"job_id": job_id, "job_type": "scan"})


def _safe_log_event(db: Database, event_type: str, payload: dict) -> None:
    """协调器内的日志：失败绝不杀死线程（I1）。"""
    try:
        events.log_event(db, "system", event_type, payload)
    except Exception:  # noqa: BLE001, S110
        pass


def start_scan_coordinator(db_path: str | Path,
                           parent_job_id: int) -> threading.Thread:
    """守护线程：轮询 pending_children 直到 0，再收尾父 scan。
    不占用 worker 池线程（spec：分类并发 = worker 数）。

    循环体整段 try/except：一次瞬态 sqlite 锁冲突或写事件失败不能让线程死掉
    ——否则父 scan 永远停在 running，且 has_active_job_of_type('scan') 恒真，
    全局轮询调度停摆直到进程重启。错误日志按 SCAN_COORD_ERROR_LOG_INTERVAL
    节流（否则每 0.5s 一条，6h 可达 ~43k 条）。超时（SCAN_COORDINATOR_TIMEOUT）
    时记 scan_coordinator_timeout **并把父 job 置失败**——否则调度停摆要等到
    进程重启才恢复。"""
    def _loop() -> None:
        db = Database(db_path)
        deadline = time.monotonic() + SCAN_COORDINATOR_TIMEOUT
        last_error_log: float | None = None
        while time.monotonic() < deadline:
            try:
                job = jobs.get_job(db, parent_job_id)
                if job is None or job.status != "running":
                    return
                finish_scan_parent(db, parent_job_id)
                if jobs.get_job(db, parent_job_id).status != "running":
                    return
            except Exception as exc:  # noqa: BLE001
                now = time.monotonic()
                if (last_error_log is None
                        or now - last_error_log >= SCAN_COORD_ERROR_LOG_INTERVAL):
                    last_error_log = now
                    _safe_log_event(db, "scan_coordinator_error",
                                    {"job_id": parent_job_id, "error": str(exc)})
            time.sleep(SCAN_COORD_POLL_SECONDS)

        _safe_log_event(db, "scan_coordinator_timeout",
                        {"job_id": parent_job_id})
        # 失败父 job：让轮询调度无需重启即可恢复（I1/H1）。
        try:
            job = jobs.get_job(db, parent_job_id)
            if job is not None and job.status == "running":
                jobs.finish_job(db, parent_job_id,
                                error="scan coordinator timeout")
        except Exception:  # noqa: BLE001, S110
            pass

    t = threading.Thread(target=_loop, daemon=True,
                         name=f"scan-coord-{parent_job_id}")
    t.start()
    return t


def _already_done(db: Database, asset_id: str) -> bool:
    """"已完成"过滤：asset_records 有 dry_run=false AND status=ok 的记录
    （权威来源是 asset_records 而非 asset_state）。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM asset_records "
            "WHERE asset_id = ? AND dry_run = 0 AND status = 'ok' LIMIT 1",
            (asset_id,),
        ).fetchone()
    return row is not None


def _heal_queue_tag(db: Database, client, asset_id: str, queue_tag_name: str) -> None:
    """对已完成但仍挂队列标签的资产补摘标签（含 VIDEO 等非 IMAGE 类型，
    scan 的搜索条件捞到什么摘什么）。

    队列标签 id 在 scan_queue_assets 里已 ensure 过，这里按名再取一次即可。
    摘失败记事件，下轮 scan 重试——不影响本轮其他资产。
    """
    import json as _json

    if client is None:  # 测试替身路径（run_scan_job 允许 client=None）
        return
    try:
        tag_ids = client.ensure_tags([queue_tag_name])
        client.untag_assets(tag_ids[queue_tag_name], [asset_id])
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (NULL, ?, 'queue_remove', 'apply', ?)",
                (asset_id, _json.dumps({"queue_tag_name": queue_tag_name,
                                        "healed": True})),
            )
        events.log_event(db, "system", "queue_tag_healed", {"asset_id": asset_id})
    except ImmichError as exc:
        events.log_event(db, "system", "queue_tag_heal_failed",
                         {"asset_id": asset_id, "error": str(exc)})


# ------------------------------------------------------------- dispatch


def run_job(db: Database, client, llm, job: jobs.JobRow, *,
            on_scan_split: Callable[[int], None] | None = None) -> None:
    """按 job type 分发；未知 type 记 job_error 并 fail。

    on_scan_split：scan 拆分成功后（父仍 running）以父 job id 回调，由调用方
    启动协调器线程——协调器不占 worker 池线程。
    """
    if job.type in ("single", "dryrun"):
        run_single_job(db, client, llm, job, build_pipeline_config(db))
    elif job.type == "classify_item":
        run_classify_item(db, client, llm, job,
                          lambda: build_pipeline_config(db))
    elif job.type == "scan":
        # cfg_factory 是零参回调（run_scan_job 约定），build_pipeline_config 需要 db ——
        # 真机曾因此炸：直接传函数对象，cfg_factory() 调用时缺 db 参数
        run_scan_job(db, client, llm, job, lambda: build_pipeline_config(db))
        # 拆分成功（父仍 running）才收尾；失败/被取消的父无需协调器。
        # 未注入回调时**直接**起协调器——否则父永远停在 running（I2）。
        if jobs.get_job(db, job.id).status == "running":
            if on_scan_split is not None:
                on_scan_split(job.id)
            else:
                start_scan_coordinator(db.path, job.id)
    elif job.type == "backfill":
        run_backfill_job(db, client, job, lambda: build_pipeline_config(db))
    elif job.type == "reclassify":
        run_reclassify_job(db, client, llm, job,
                           lambda: build_pipeline_config(db))
    elif job.type == "reset":
        run_reset_job(db, client, job)
    elif job.type == "undo":
        run_undo_job(db, client, job)
    else:
        msg = f"unsupported job type: {job.type}"
        events.log_event(db, "system", "job_error",
                         {"job_id": job.id, "job_type": job.type,
                          "error": msg})
        jobs.finish_job(db, job.id, error=msg)


# ------------------------------------------------------------- undo


def run_undo_job(db: Database, client, job: jobs.JobRow) -> None:
    """undo：撤销目标 job 的 apply change_log。异常收敛 finish_job(error)。"""
    try:
        _run_undo_job(db, client, job)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_undo_job(db, client, job) -> None:
    target_job_id = int(job.params["target_job_id"])
    session = UndoSession(db, client, undo_job_id=job.id)

    total = session.total(target_job_id)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = ? WHERE id = ?",
                     (total, job.id))

    _log_job_event(db, "job_started", job, target_job_id=target_job_id,
                   total=total)

    undone = 0
    for row in session.load_rows(target_job_id):
        # 取消检查在每条之间（语义同 scan）
        if jobs.is_canceled(db, job.id):
            break
        # total = apply 条数；404 跳过的一条同样 incr（进度语义按条数计）。
        # 跳过不是失败（对应关系已被用户手工删除是预期路径），不计 failed；
        # 但 undone 只统计实际执行的条
        if session.undo_row(row):
            undone += 1
        jobs.incr_progress(db, job.id)

    session.flush_state()
    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, undone=undone, total=total)


# ------------------------------------------------------- backfill

BACKFILL_BATCH_SIZE = 200


def run_backfill_job(db: Database, client, job: jobs.JobRow,
                     cfg_factory: Callable[[], PipelineConfig]) -> None:
    """backfill（全量回填）：范围内未处理图片打队列标签，不立即分类。
    入队不是分类结果：不写 change_log、不改 asset_state（计划已决策）。"""
    try:
        _run_backfill_job(db, client, job, cfg_factory)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_backfill_job(db, client, job, cfg_factory) -> None:
    queue_tag_name = cfg_factory().queue_tag_name
    rules = load_scope_rules(db)

    candidates = [
        aid for aid in iter_scope_assets(client, db, rules)
        if not _already_done(db, aid)
    ]
    total = len(candidates)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = ? WHERE id = ?",
                     (total, job.id))

    _log_job_event(db, "job_started", job, total=total)

    done = 0
    tag_id = client.ensure_tags([queue_tag_name])[queue_tag_name]
    for i in range(0, len(candidates), BACKFILL_BATCH_SIZE):
        # 取消检查在每批之间
        if jobs.is_canceled(db, job.id):
            break
        batch = candidates[i : i + BACKFILL_BATCH_SIZE]
        client.tag_assets([tag_id], batch)
        for asset_id in batch:
            events.log_event(db, "system", "asset_enqueued", {
                "job_id": job.id, "asset_id": asset_id,
            })
        for _ in batch:
            jobs.incr_progress(db, job.id)
        done += len(batch)

    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, done=done, total=total)


# ---------------------------------------------------- reclassify


def run_reclassify_job(db: Database, client, llm, job: jobs.JobRow,
                       cfg_factory: Callable[[], PipelineConfig]) -> None:
    """reclassify：对 params["asset_ids"] 逐张摘管理标签并重跑分类。"""
    try:
        _run_reclassify_job(db, client, llm, job, cfg_factory)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_reclassify_job(db, client, llm, job, cfg_factory) -> None:
    asset_ids = [str(a) for a in job.params.get("asset_ids", [])]
    total = len(asset_ids)
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total = ? WHERE id = ?",
                     (total, job.id))

    _log_job_event(db, "job_started", job, total=total)

    cfg = cfg_factory()
    failed = 0
    for asset_id in asset_ids:
        # 取消检查在每张之间
        if jobs.is_canceled(db, job.id):
            break
        result = reclassify_asset(
            db, client, llm, cfg, asset_id, job_id=job.id,
            prompt_version_id=job.prompt_version_id,
        )
        if result.status == "error":
            failed += 1
        jobs.incr_progress(db, job.id, failed=(result.status == "error"))

    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, done=total - failed,
                   failed=failed, total=total)


# --------------------------------------------------------- reset


from app.classify.reset_ops import run_reset_steps


def run_reset_job(db: Database, client, job: jobs.JobRow) -> None:
    """reset（危险区）：unarchive 本系统归档过的资产 + 摘除管理标签（标签实体保留）+ 清 asset_state。"""
    try:
        _run_reset_job(db, client, job)
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_reset_job(db, client, job) -> None:
    _log_job_event(db, "job_started", job)
    done = run_reset_steps(db, client, job_id=job.id,
                           is_canceled=lambda: jobs.is_canceled(db, job.id))
    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, **done)
