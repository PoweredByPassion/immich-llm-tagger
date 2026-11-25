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

import json
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx

from app.classify.pipeline import (
    PipelineConfig,
    PipelineResult,
    classify_asset,
)
from app.classify.reclassify import reclassify_asset
from app.classify.scope import (
    check_auto_eligibility,
    iter_scope_assets,
    load_exclusions,
    load_scope_rules,
    scan_queue_assets,
)
from app.classify.undo import UndoSession
from app.clients._immich_gen.models.asset_type_enum import AssetTypeEnum
from app.clients._immich_gen.models.metadata_search_dto import MetadataSearchDto
from app.clients.immich import ImmichError, ImmichScanIncomplete
from app.db.connection import Database
from app.store import events, jobs
from app.store.assets import processing_complete
from app.store.connections import ConnectionStore
from app.store.rules import DEFAULT_PROMPT_TEMPLATE as DEFAULT_PROMPT_TEMPLATE
from app.store.rules import read_rule_snapshot
from app.store.settings import SettingsStore
from app.store.tags import TagDef

# scan 拆分后的协调器：轮询子任务直到全部终态，再收尾父 scan。不占用 worker
# 池线程（spec：分类并发 = worker 数）。
SCAN_COORD_POLL_SECONDS = 0.5
SCAN_COORDINATOR_TIMEOUT = 6 * 3600   # 协调器最长等待，防僵尸线程
SCAN_COORD_ERROR_LOG_INTERVAL = 60.0  # 协调器错误日志节流（同 scheduler）


def build_pipeline_config(db: Database, job: jobs.JobRow | None = None) -> PipelineConfig:
    """Use the queued rules and current connection credentials and privacy settings."""
    from app.classify.thumb import ThumbConfig

    with db.connect() as conn:
        conn.execute("BEGIN")
        snapshot = None
        if job is not None:
            snapshot_job = job.params["parent_job_id"] if job.type == "classify_item" else job.id
            row = conn.execute("SELECT rule_snapshot FROM jobs WHERE id=?", (snapshot_job,)).fetchone()
            if row and row["rule_snapshot"]:
                snapshot = json.loads(row["rule_snapshot"])
        if snapshot is None:
            snapshot = read_rule_snapshot(conn)
        current = SettingsStore(db).get_all(conn)
        llm = ConnectionStore(db).get_llm(conn)

    return PipelineConfig(
        prompt_template=snapshot["prompt_template"],
        prompt_version_id=snapshot["prompt_version_id"],
        tags=[TagDef(**tag) for tag in snapshot["tags"]],
        marker_rules=[tuple(rule) for rule in snapshot["marker_rules"]],
        fallback_tag=snapshot["fallback_tag_name"],
        default_min_confidence=snapshot["default_min_confidence"],
        thumb=ThumbConfig(preset=snapshot["thumb_preset"], quality=snapshot["thumb_quality"]),
        queue_tag_name=snapshot["queue_tag_name"],
        llm=llm,
        concurrency_owner=_owner_id(db),
        enabled_effects=frozenset(snapshot["enabled_effects"]),
        skip_marker_tag_name=snapshot["skip_marker_tag_name"] or "保留人工信息",
        sensitive_marker_tag_name=snapshot["sensitive_marker_tag_name"],
        apply_max_attempts=int(snapshot["apply_max_attempts"]),
        allow_cloud_images=bool(current["allow_cloud_images"]),
        retain_model_payloads=bool(current["retain_model_payloads"]),
    )


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
    with db.connect() as conn:
        interrupted = conn.execute(
            "SELECT status, apply_status FROM asset_records WHERE job_id=? AND asset_id=? ORDER BY id DESC LIMIT 1",
            (job.id, asset_id),
        ).fetchone()
    if interrupted and interrupted["apply_status"] == "applied":
        failed = interrupted["status"] == "error"
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET total=1, done=?, failed=? WHERE id=?",
                         (int(not failed), int(failed), job.id))
        jobs.finish_job(db, job.id, error="Model output was invalid" if failed else None)
        return
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total=1 WHERE id=?", (job.id,))
    # 沙盒试跑允许携带编辑框里的草稿模板（未保存版本也能试跑）
    if dry_run and params.get("prompt_body"):
        cfg = replace(cfg, prompt_template=str(params["prompt_body"]), prompt_version_id=None)

    _log_job_event(db, "job_started", job, asset_id=asset_id)
    result = classify_asset(
        db, client, llm, asset_id, cfg,
        source=source, dry_run=dry_run, job_id=job.id,
        prompt_version_id=cfg.prompt_version_id or job.prompt_version_id,
        asset_hint=params.get("asset_hint"),
        auto_check=source in {"webhook", "poll"} and client is not None,
        skip_completed=not dry_run and source in {"webhook", "poll"},
    )
    if result.status == "busy" and job.status == "running":
        jobs.defer_discovery(db, job.id, delay_seconds=5)
        return
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET done=?, failed=? WHERE id=?",
                     (int(not result.failed), int(result.failed), job.id))
    _record_result(db, job.id, result, result.dry_run)
    jobs.finish_job(db, job.id, error=(result.apply_error or "Classification failed") if result.failed else None)
    _log_job_event(db, "job_complete", job, asset_id=asset_id)


def _record_result(db: Database, job_id: int, result: PipelineResult,
                   dry_run: bool) -> None:
    """按结果写 asset 事件（计数由调用方负责——需与 incr_progress 分离，
    才能保证「计数恰好一次」，见 run_classify_item 的 counted 守卫）。"""
    if result.failed:
        events.log_event(db, "system", "asset_apply_incomplete" if result.apply_status else "job_error", {
            "job_id": job_id, "asset_id": result.asset_id, "status": result.apply_status,
            "error": result.apply_error or "classification failed", "dry_run": dry_run,
        })
    elif result.status == "ok":
        events.log_event(db, "system", "asset_ingested", {
            "job_id": job_id, "asset_id": result.asset_id, "tag": result.tag,
            "confidence": result.confidence, "dry_run": dry_run,
        })
    elif result.status in ("refused", "busy", "skipped"):
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
    except ImmichScanIncomplete as exc:
        _pause_discovery(db, job.id, str(exc))
    except ImmichError as exc:
        if hasattr(client, "iter_asset_pages") and _temporarily_unavailable(exc):
            _pause_discovery(db, job.id, str(exc))
        else:
            jobs.cancel_children_of(db, job.id)
            _log_job_event(db, "job_error", job, error=str(exc))
            jobs.finish_job(db, job.id, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        jobs.cancel_children_of(db, job.id)
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
    if hasattr(client, "iter_asset_pages"):
        _discover_queued_pages(db, client, job, cfg, rules)
        return
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


def _pause_discovery(db, job_id, reason):
    jobs.defer_discovery(db, job_id)
    events.log_event(db, "system", "scan_discovery_paused", {"job_id": job_id, "reason": reason})


def _temporarily_unavailable(exc: ImmichError) -> bool:
    status = exc.status_code
    return (status in {408, 429} or (status is not None and status >= 500)
            or isinstance(exc.__cause__, httpx.TransportError))


def _discover_queued_pages(db, client, job, cfg, rules):
    if jobs.is_canceled(db, job.id):
        jobs.cancel_children_of(db, job.id)
        jobs.finish_job(db, job.id)
        return
    tag_id = client.ensure_tags([cfg.queue_tag_name])[cfg.queue_tag_name]
    query = MetadataSearchDto(type_=AssetTypeEnum.IMAGE, tag_ids=[tag_id])
    exclusions = load_exclusions(db)
    source = job.params.get("source", "poll")
    if source not in {"poll", "webhook", "manual"}:
        source = "poll"
    for page, checkpoint in client.iter_asset_pages(query, page=job.scan_page, cursor=job.scan_cursor):
        if jobs.is_canceled(db, job.id):
            break
        candidates = []
        for asset in page:
            if jobs.is_canceled(db, job.id):
                break
            asset_id = str(asset.id)
            eligible, _, _ = check_auto_eligibility(
                client, db, asset_id, asset=asset, rules=rules, exclusions=exclusions,
            )
            if eligible:
                candidates.append(asset_id)
        jobs.enqueue_scan_items(
            db, job.id, candidates, source,
            next_page=checkpoint if isinstance(checkpoint, int) else None,
            next_cursor=checkpoint if isinstance(checkpoint, str) else None,
            defer_until_discovered=True, complete_discovery=checkpoint is None,
        )
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET discovery_complete=1 WHERE id=?", (job.id,))
    events.log_event(db, "system", "scan_split", {"job_id": job.id})


def run_classify_item(db: Database, client, llm, job: jobs.JobRow,
                      cfg_factory: Callable[[], PipelineConfig]) -> None:
    """Process one scan item and complete it atomically with the parent counter."""
    parent_id: int | None = None
    try:
        parent_id = int(job.params["parent_job_id"])
        asset_id = job.params["asset_id"]
        cfg = cfg_factory()
        if _already_done(db, asset_id):
            # 已完成却仍挂着队列标签（拆分后、本子任务执行前被并发副本抢先
            # 处理且其 untag 失败，或历史 untag 假成功的遗留）→ 补摘标签。
            # 否则该图会永久残留「未分类」标签，被后续每轮 scan 反复发现。
            _heal_queue_tag(db, client, asset_id, cfg.queue_tag_name)
            jobs.finish_scan_item(db, job.id, parent_id)
            return
        result = classify_asset(
            db, client, llm, asset_id, cfg,
            source=job.params.get("source", "poll"),
            dry_run=False,
            job_id=parent_id,
            prompt_version_id=cfg.prompt_version_id,
            auto_check=client is not None,
        )
        if result.status == "busy":
            if _already_done(db, asset_id):
                jobs.finish_scan_item(db, job.id, parent_id)
            else:
                jobs.defer_discovery(db, job.id, delay_seconds=5)
            return
        jobs.finish_scan_item(db, job.id, parent_id,
                              error=(result.apply_error or "Classification failed") if result.failed else None)
        _record_result(db, parent_id, result, dry_run=False)
    except Exception as exc:  # noqa: BLE001
        if parent_id is None:
            jobs.finish_job(db, job.id, error=str(exc))
        else:
            jobs.finish_scan_item(db, job.id, parent_id, error=str(exc))
        _log_job_event(db, "job_error", job, error=str(exc))


def _active_prompt_version_id(db: Database) -> int | None:
    """执行时解析"本次实际渲染所用"的 prompt 版本 id，写进 asset_records。

    必须与 build_pipeline_config 的取值规则一致，否则会记录一个"其 body 并未
    被渲染"的版本：
    - active 为 None → 回退最新版本（无版本 → None）。
    - active 有值且行存在 → 该 id。
    - active 有值但行缺失（悬空）→ None：build_pipeline_config 此时回退内置
      DEFAULT_PROMPT_TEMPLATE（**不是**最新版本），故没有版本 id 可记。"""
    return build_pipeline_config(db).prompt_version_id


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


def start_scan_coordinator(db_path: str | Path, parent_job_id: int, *,
                           stop_event: threading.Event | None = None) -> threading.Thread:
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
        stop = stop_event or threading.Event()
        deadline = time.monotonic() + SCAN_COORDINATOR_TIMEOUT
        last_error_log: float | None = None
        progress = None
        while not stop.is_set() and time.monotonic() < deadline:
            try:
                job = jobs.get_job(db, parent_job_id)
                if job is None or job.status != "running":
                    return
                current_progress = job.done + job.failed
                if progress is not None and current_progress > progress:
                    deadline = time.monotonic() + SCAN_COORDINATOR_TIMEOUT
                progress = current_progress
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
            stop.wait(SCAN_COORD_POLL_SECONDS)

        if stop.is_set():
            return
        _safe_log_event(db, "scan_coordinator_timeout",
                        {"job_id": parent_job_id})
        # 失败父 job：让轮询调度无需重启即可恢复（I1/H1）。
        try:
            job = jobs.get_job(db, parent_job_id)
            if job is not None and job.status == "running":
                jobs.cancel_children_of(db, parent_job_id)
                jobs.finish_job(db, parent_job_id,
                                error="scan coordinator timeout")
        except Exception:  # noqa: BLE001, S110
            pass

    t = threading.Thread(target=_loop, daemon=True,
                         name=f"scan-coord-{parent_job_id}")
    t.start()
    return t


def _already_done(db: Database, asset_id: str) -> bool:
    """Use the durable asset state, with a fallback for older records."""
    with db.connect() as conn:
        return processing_complete(conn, asset_id)



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
        run_single_job(db, client, llm, job, build_pipeline_config(db, job))
    elif job.type == "classify_item":
        run_classify_item(db, client, llm, job,
                          lambda: build_pipeline_config(db, job))
    elif job.type == "scan":
        # cfg_factory 是零参回调（run_scan_job 约定），build_pipeline_config 需要 db ——
        # 真机曾因此炸：直接传函数对象，cfg_factory() 调用时缺 db 参数
        run_scan_job(db, client, llm, job, lambda: build_pipeline_config(db, job))
        # 拆分成功（父仍 running）才收尾；失败/被取消的父无需协调器。
        # 未注入回调时**直接**起协调器——否则父永远停在 running（I2）。
        if jobs.get_job(db, job.id).status == "running":
            if on_scan_split is not None:
                on_scan_split(job.id)
            else:
                start_scan_coordinator(db.path, job.id)
    elif job.type == "backfill":
        run_backfill_job(db, client, job, lambda: build_pipeline_config(db, job))
    elif job.type == "reclassify":
        run_reclassify_job(db, client, llm, job,
                           lambda: build_pipeline_config(db, job))
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
    session.prepare(target_job_id)

    total = session.total(target_job_id)
    finished, skipped = session.progress()
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET total=?, done=? WHERE id=?", (total, finished, job.id))

    _log_job_event(db, "job_started", job, target_job_id=target_job_id,
                   total=total)

    for row in session.load_rows(target_job_id):
        # 取消检查在每条之间（语义同 scan）
        if jobs.is_canceled(db, job.id):
            break
        session.undo_row(row)
        finished, skipped = session.progress()
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET done=? WHERE id=?", (finished, job.id))

    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, undone=finished - skipped, total=total, conflicts=skipped)


# ------------------------------------------------------- backfill

BACKFILL_BATCH_SIZE = 200


def run_backfill_job(db: Database, client, job: jobs.JobRow,
                     cfg_factory: Callable[[], PipelineConfig]) -> None:
    """backfill（全量回填）：范围内未处理图片打队列标签，不立即分类。
    入队不是分类结果：不写 change_log、不改 asset_state（计划已决策）。"""
    try:
        _run_backfill_job(db, client, job, cfg_factory)
    except ImmichScanIncomplete as exc:
        _pause_discovery(db, job.id, str(exc))
    except ImmichError as exc:
        if hasattr(client, "iter_asset_pages") and _temporarily_unavailable(exc):
            _pause_discovery(db, job.id, str(exc))
        else:
            _log_job_event(db, "job_error", job, error=str(exc))
            jobs.finish_job(db, job.id, error=str(exc))
    except Exception as exc:  # noqa: BLE001
        _log_job_event(db, "job_error", job, error=str(exc))
        jobs.finish_job(db, job.id, error=str(exc))


def _run_backfill_job(db, client, job, cfg_factory) -> None:
    queue_tag_name = cfg_factory().queue_tag_name
    rules = load_scope_rules(db)

    if hasattr(client, "iter_asset_pages"):
        _backfill_pages(db, client, job, queue_tag_name, rules)
        return

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


def _backfill_pages(db, client, job, queue_tag_name, rules):
    exclusions = load_exclusions(db)
    dto = MetadataSearchDto(type_=AssetTypeEnum.IMAGE)
    tag_id = client.ensure_tags([queue_tag_name])[queue_tag_name]
    for page, checkpoint in client.iter_asset_pages(dto, page=job.scan_page,
                                                    cursor=job.scan_cursor, size=BACKFILL_BATCH_SIZE):
        if jobs.is_canceled(db, job.id):
            break
        ids = []
        for asset in page:
            asset_id = str(asset.id)
            if not _already_done(db, asset_id) and check_auto_eligibility(
                client, db, asset_id, asset=asset, rules=rules, exclusions=exclusions,
            )[0]:
                ids.append(asset_id)
        if jobs.is_canceled(db, job.id):
            break
        if ids:
            client.tag_assets([tag_id], ids)
        with db.connect() as conn:
            conn.execute(
                "UPDATE jobs SET done=done+?, total=total+?, scan_page=COALESCE(?, scan_page), "
                "scan_cursor=?, discovery_complete=? WHERE id=?",
                (len(ids), len(ids), checkpoint if isinstance(checkpoint, int) else None,
                 checkpoint if isinstance(checkpoint, str) else None, checkpoint is None, job.id),
            )
    jobs.finish_job(db, job.id)


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
    for asset_id in asset_ids[job.done + job.failed:]:
        # 取消检查在每张之间
        if jobs.is_canceled(db, job.id):
            break
        with db.connect() as conn:
            previous = conn.execute(
                "SELECT apply_status FROM asset_records WHERE job_id=? AND asset_id=? ORDER BY id DESC LIMIT 1",
                (job.id, asset_id),
            ).fetchone()
        if previous and previous["apply_status"] == "applied":
            jobs.incr_progress(db, job.id)
        else:
            result = reclassify_asset(
                db, client, llm, cfg, asset_id, job_id=job.id,
                prompt_version_id=cfg.prompt_version_id or job.prompt_version_id,
            )
            jobs.incr_progress(db, job.id, failed=result.failed)

    current = jobs.get_job(db, job.id)
    jobs.finish_job(db, job.id, error=(f"{current.failed} assets could not be reclassified"
                                      if current.failed else None))
    _log_job_event(db, "job_complete", job, done=current.done,
                   failed=current.failed, total=total)


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
    dims = job.params.get("dimensions")
    def update_progress(attempted, total, skipped):
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET done=?, total=? WHERE id=?", (attempted, total, job.id))

    done = run_reset_steps(db, client, job_id=job.id,
                           is_canceled=lambda: jobs.is_canceled(db, job.id),
                           dimensions=set(dims) if dims else None, on_progress=update_progress)
    jobs.finish_job(db, job.id)
    _log_job_event(db, "job_complete", job, **done)
