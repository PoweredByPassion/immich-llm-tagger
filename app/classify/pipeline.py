"""单张处理管线：把租约 / 取图 / prompt / LLM / 归一 / 记录 / 应用串起来。

本模块负责管线编排，单张失败不抛异常穿透（status="error"），
是否重试由上层 job 引擎决定。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.classify.commit import CommitResult, commit_effects
from app.classify.effectors import plan_all
from app.classify.effectors.base import Effect, EffectContext, Gates, PlanOutcome
from app.classify.lease import LLM_MAX_RETRIES, claim, lease_seconds_for, release, renew
from app.classify.parse import (
    decide_archive,
    extract_json,
    normalize,
    refusal_marker_names,
)
from app.classify.prompt import PromptContext, render_prompt
from app.classify.snapshot import AssetSnapshot
from app.classify.thumb import ThumbConfig, fetch_and_encode
from app.db.connection import Database
from app.store.assets import (
    bumped_apply_attempts, has_system_archive, last_written_value,
    write_changes_and_state,
)
from app.store.connections import LLMConn
from app.store.events import log_event
from app.store.tags import TagDef


@dataclass(frozen=True)
class PipelineConfig:
    """一个 job 启动时读一次的配置快照，循环内不再重复查库。"""

    prompt_template: str
    tags: list[TagDef]
    marker_rules: list[tuple[str, dict]]
    fallback_tag: str
    default_min_confidence: float
    thumb: ThumbConfig
    queue_tag_name: str
    llm: LLMConn
    concurrency_owner: int
    # --- Effector 开关与重试（§8.1）---
    # 默认值让既有构造点无需改动；生产路径由 build_pipeline_config 覆盖。
    # 只有全局效应的那三个：评分/描述改由标签维度控制（tags.write_rating /
    # write_description，§5.8），不在这个集合里。
    enabled_effects: frozenset[str] = frozenset({"tag", "visibility", "album"})
    skip_marker_tag_name: str = "保留人工信息"
    apply_max_attempts: int = 3


@dataclass(frozen=True)
class PipelineResult:
    asset_id: str
    tag: str
    confidence: float | None
    markers: tuple[str, ...]
    status: str  # "ok" | "refused" | "error"
    dry_run: bool


_ERROR = PipelineResult  # 仅用于构造错误结果的别名


def _error_result(asset_id: str, dry_run: bool) -> PipelineResult:
    return _ERROR(asset_id=asset_id, tag="", confidence=None, markers=(),
                  status="error", dry_run=dry_run)


def _unrecoverable(exc: BaseException) -> bool:
    """整张图"重试一万次也一样"的失败？看状态码，不看异常类型名。

    4xx = 这次请求本身有问题：Immich 侧媒体缺失/不可读（0 字节的 HEIC → 缩略图
    404），或 LLM 侧把图片/参数拒了。两者都不会因为重试而改变。
    5xx / 429 / 超时 / 网络 = 暂态，保持无限重试（上游恢复了就该成功）。
    """
    code = getattr(exc, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500


def _asset_meta(client, asset_id: str, asset_hint: dict | None):
    """有 hint 用 hint（webhook 携带，不重复查 Immich），否则回退 get_asset。"""
    if asset_hint is not None:
        return (
            asset_hint.get("originalFileName"),
            asset_hint.get("exifInfo"),
            asset_hint.get("localDateTime"),
        )
    dto = client.get_asset(asset_id)
    raw = dto.exif_info
    exif: dict | None = None
    if raw is not None:
        if hasattr(raw, "to_dict"):
            exif = raw.to_dict()
        elif isinstance(raw, dict):
            exif = raw
        else:
            exif = vars(raw)
    date = getattr(dto, "local_date_time", None)
    return dto.original_file_name, exif, date.isoformat() if date else None


def _messages(prompt: str, image_b64: str | None) -> list[dict]:
    """image_b64 为 None（未开多模态）时退化为纯文本 messages，不发图。"""
    if image_b64 is None:
        return [{"role": "user", "content": prompt}]
    return [{
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {"type": "image_url",
             "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
        ],
    }]


def _llm_request_snapshot(llm, messages: list[dict]) -> str:
    """发往 LLM 的请求参数快照（落 asset_records.llm_request）。

    base64 图片数据不落库（大且可从 immich 重取），替换为长度摘要。
    llm 是 LLMClient；字段读取用 getattr 容错单测替身缺属性的情况。
    """
    snapshot_messages = json.loads(json.dumps(messages, ensure_ascii=False))

    def _shrink(node) -> None:
        if isinstance(node, dict):
            url = node.get("image_url")
            if isinstance(url, dict) and isinstance(url.get("url"), str):
                raw = url["url"]
                url["url"] = f"{raw[:40]}... ({len(raw)} chars, base64 omitted)"
            for v in node.values():
                _shrink(v)
        elif isinstance(node, list):
            for item in node:
                _shrink(item)

    for msg in snapshot_messages:
        _shrink(msg)
    return json.dumps({
        "model": getattr(llm, "_model", None),
        "max_tokens": getattr(llm, "_max_tokens", None),
        "messages": snapshot_messages,
    }, ensure_ascii=False)


def classify_asset(
    db: Database,
    client,
    llm,
    asset_id: str,
    cfg: PipelineConfig,
    *,
    source: str,
    dry_run: bool = False,
    job_id: int | None = None,
    prompt_version_id: int | None = None,
    asset_hint: dict | None = None,
    override_manual: bool | None = None,
    allow_metadata_overwrite: bool | None = None,
) -> PipelineResult:
    # 统一将 asset_id 收敛为 str，下游（租约/记录/Applier）全部按 str 处理。
    asset_id = str(asset_id)
    lease_seconds = lease_seconds_for(cfg.llm.timeout, LLM_MAX_RETRIES)
    if not claim(db, asset_id, cfg.concurrency_owner, lease_seconds):
        return _error_result(asset_id, dry_run)
    try:
        return _process(db, client, llm, asset_id, cfg, source=source,
                        dry_run=dry_run, job_id=job_id,
                        prompt_version_id=prompt_version_id,
                        asset_hint=asset_hint, lease_seconds=lease_seconds,
                        override_manual=override_manual,
                        allow_metadata_overwrite=allow_metadata_overwrite)
    except Exception:
        return _error_result(asset_id, dry_run)
    finally:
        release(db, asset_id, cfg.concurrency_owner)


def _process(db, client, llm, asset_id, cfg, *, source, dry_run, job_id,
             prompt_version_id, asset_hint, lease_seconds,
             override_manual, allow_metadata_overwrite=False) -> PipelineResult:
    started = time.monotonic()
    # 取图/渲染 prompt 阶段就失败时没有请求可记，error 路径留 None。
    request_snapshot: str | None = None
    filename = None
    # 重试复用（§7.3 层 1）：已有"分析成功但未应用"的记录时跳过 LLM。
    reused = _reusable_analysis(db, asset_id) if not dry_run else None
    if reused is not None:
        cls, status, filename = reused["cls"], "ok", reused["filename"]
    else:
        try:
            # 未勾选多模态：不发图，也省掉一次取图（fetch_and_encode）。
            image_b64 = fetch_and_encode(client, asset_id, cfg.thumb) \
                if cfg.llm.multimodal else None
            filename, exif, date = _asset_meta(client, asset_id, asset_hint)
            primary = [(t.name, t.description) for t in cfg.tags
                       if t.kind == "primary" and t.enabled]
            prompt = render_prompt(cfg.prompt_template, PromptContext(
                tag_list=primary, filename=filename, exif=exif, scope=None,
                date=date,
            ))
            messages = _messages(prompt, image_b64)
            request_snapshot = _llm_request_snapshot(llm, messages)
            result = llm.chat(messages)
            renew(db, asset_id, cfg.concurrency_owner, lease_seconds)
        except Exception as exc:
            _write_record(db, asset_id, source=source, tag=None, confidence=None,
                          markers=(), model=None, usage=None, status="error",
                          raw_response=None, llm_request=request_snapshot,
                          dry_run=dry_run, job_id=job_id,
                          prompt_version_id=prompt_version_id,
                          duration_ms=_ms(started), filename=filename)
            _analysis_failed(db, client, asset_id, exc, cfg,
                             dry_run=dry_run, job_id=job_id)
            return _error_result(asset_id, dry_run)

        raw_text = result.content
        # extract_json 故意**不**套 try：解析期异常由 classify_asset 统一兜成
        # error（既有语义，test_post_llm_crash_becomes_error_and_releases 钉着）。
        raw = extract_json(raw_text)
        # raw is None 的两种情形（真拒答 / 不合契约）归一结果完全一致，
        # 原来分成两个分支是冗余，这里合并。
        cls = normalize(raw, refusal_text=None if raw is not None else raw_text,
                        enabled_primary_tags={n for n, _ in _primary_names(cfg)},
                        fallback_tag=cfg.fallback_tag,
                        marker_rules=cfg.marker_rules)
        status = "ok" if raw is not None else "refused"

        _write_record(db, asset_id, source=source, tag=cls.tag,
                      confidence=cls.confidence, markers=cls.markers,
                      model=result.model, usage=result.usage, status=status,
                      raw_response=raw_text, llm_request=request_snapshot,
                      dry_run=dry_run, job_id=job_id,
                      prompt_version_id=prompt_version_id,
                      duration_ms=_ms(started), filename=filename,
                      rating=cls.rating, description=cls.description)

    if dry_run:
        return PipelineResult(asset_id=asset_id, tag=cls.tag,
                              confidence=cls.confidence, markers=cls.markers,
                              status=status, dry_run=True)

    selected = next((t for t in cfg.tags
                     if t.kind == "primary" and t.enabled and t.name == cls.tag), None)
    archive = decide_archive(selected, cls.confidence, cfg.default_min_confidence)

    # override_manual 非 None 时替代 DB 读取（reclassify 用户显式发起 → 全量 apply）。
    # ⚠ 这段原来在被替换的 try 块**内部**，重建时必须挪到构建 ctx 之前。
    if override_manual is not None:
        effective_manual_override = override_manual
    else:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT manual_override FROM asset_state WHERE asset_id = ?",
                (asset_id,),
            ).fetchone()
        effective_manual_override = bool(row["manual_override"]) if row else False

    snapshot = _snapshot(client, asset_id)
    gates = Gates(
        dry_run=False,                       # 已在上面的 if dry_run 里返回
        manual_override=effective_manual_override,
        skip_immich=bool(getattr(selected, "skip_immich", False)),
        allow_metadata_overwrite=bool(allow_metadata_overwrite),
    )
    ctx = EffectContext(
        asset_id=asset_id, analysis=cls, snapshot=snapshot, tag_def=selected,
        archive=archive, enabled=cfg.enabled_effects, gates=gates)
    outcome = PlanOutcome()
    effects = plan_all(
        ctx,
        last_written={
            "rating_set": last_written_value(db, asset_id, "rating_set"),
            "description_set": last_written_value(db, asset_id, "description_set"),
        },
        outcome=outcome,
        has_system_archive=has_system_archive(db, asset_id),
    )
    # 跳过标记：唯一一处跨 effector 信息流（§5.4），由管线统一裁决。
    # `skipped` 一并写进 payload —— §5.8 要求它进 change_log 供 UI 与排障。
    #
    # 它是**标签写入**，所以 skip_immich 的标签一个标签都不写（含这个标记）：
    # 评分/描述与「忽略」解耦后，忽略标签也会产生 skipped（用户手写过评分），
    # 此时打标记就违背了「忽略」的承诺。判定结果同时喂给下面的 markers，
    # 免得 asset_state 记下一个根本没写进 Immich 的 marker。
    skip_marker = bool(outcome.skipped) and not gates.skip_immich
    if skip_marker:
        effects.append(Effect("tag_add", asset_id,
                              {"tag_name": cfg.skip_marker_tag_name,
                               "skipped": sorted(outcome.skipped)}))

    queue_ids = client.ensure_tags([cfg.queue_tag_name])
    try:
        result = commit_effects(
            db, client, effects, asset_id=asset_id, job_id=job_id,
            queue_tag_id=queue_ids[cfg.queue_tag_name],
            queue_tag_name=cfg.queue_tag_name, gates=gates)
    except Exception as exc:          # noqa: BLE001
        # 提交器的**意外**异常（Immich 侧的正常失败走 CommitResult(ok=False)，
        # 不抛）。分类已成功且 asset_records 已落 —— 不改写"分类过"这个事实，
        # 只按 apply 失败处理：不写 applied_at，下轮 scan 带着复用重试。
        log_event(db, "system", "apply_failed",
                  {"asset_id": asset_id, "bucket": "unknown", "error": str(exc)})
        result = CommitResult(ok=False, error=str(exc))

    markers = tuple(dict.fromkeys(
        [*cls.markers, *([cfg.skip_marker_tag_name] if skip_marker else [])]))
    if result.ok:
        _mark_applied(db, asset_id)
    else:
        _apply_failed(db, client, asset_id, error=result.error,
                      max_attempts=cfg.apply_max_attempts,
                      skip_marker_tag_name=cfg.skip_marker_tag_name)
    with db.connect() as conn:
        _update_state(conn, asset_id=asset_id, job_id=job_id, tag=cls.tag,
                      markers=markers, snapshot=snapshot,
                      committed={e.action: e.payload for e in result.committed})
    return PipelineResult(asset_id=asset_id, tag=cls.tag, confidence=cls.confidence,
                          markers=markers, status=status, dry_run=False)


def _snapshot(client, asset_id: str) -> AssetSnapshot:
    """取当前值快照（§5.3）。

    webhook hint 的 exifInfo 字段（rating/description）与 visibility 都**没有**
    对真实 Immich 实测过（§12），hint 里的这些值不能当事实 —— 所以快照**始终**
    从 `get_asset()` 取。`AssetSnapshot.from_hint` 适配器保留，等这些字段被实测
    验证、确认真带所需值时再启用。代价是每图多一次轻量 GET。
    """
    return AssetSnapshot.from_dto(client.get_asset(asset_id))


def _mark_applied(db, asset_id: str) -> None:
    """成功闸门通过：给**最新那条** dry_run=0 的记录补 applied_at（§7.1），
    并把 asset_state.apply_attempts 归零。

    口径必须与 `_already_done` 一致（同样取最新一条），否则重试场景会给
    旧记录盖章、新记录仍无凭据 —— 而 _already_done 看的正是新记录。

    apply_attempts 的语义是**连续失败次数**（§7.3）：只在失败时自增
    （`_apply_failed` / `bumped_apply_attempts`），成功时必须清零，否则
    「失败一次 → 下轮复用分析后应用成功」的资产会永远停在 1，详情页的
    「待重试 (1/3)」胶囊成为永久假阳性（与按 applied_at 判定的失败清单矛盾）。
    只在成功路径调用本函数，失败路径不经过这里。
    """
    with db.connect() as conn:
        conn.execute(
            "UPDATE asset_records SET applied_at = datetime('now') "
            " WHERE id = (SELECT id FROM asset_records "
            "              WHERE asset_id = ? AND dry_run = 0 "
            "              ORDER BY created_at DESC, id DESC LIMIT 1)",
            (asset_id,),
        )
        conn.execute(
            "UPDATE asset_state SET apply_attempts = 0 WHERE asset_id = ?",
            (asset_id,),
        )


def _update_state(conn, *, asset_id, job_id, tag, markers, snapshot,
                  committed) -> None:
    """把执行后的**实际**状态写进 asset_state。

    **来自 `result.committed`（真正落地的 Effect）的输入**：
    rating / description / visibility —— 只能从 committed 推导。从 `cls` 推导会
    在"effect 被门控/被跳过"时把没写进 Immich 的值塞进本地镜像 —— 本地比
    远端"新"，比不写更糟（撤销与重置都以此为准）。

    **刻意取「意图」而非 committed 的输入**：current_tag（`cls.tag`）与
    current_markers（管线算出的 markers）。原因有二，不是疏漏：
      1. committed 在调用点被压成 `{e.action: e.payload}` 字典，多次 `tag_add`
         （主标签 + 跳过标记标签）会互相覆盖 —— 它本身对标签维度就是有损的，
         无法据此还原全部 markers；
      2. `skip_immich` 契约要求：即使某个标签规定"什么都不写进 Immich"，也必须
         记下本次分类结果。此时 committed 为空，只有意图能表达"这张图分类过"。
    """
    rating = (committed["rating_set"]["new"] if "rating_set" in committed
              else snapshot.rating)
    description = (committed["description_set"]["new"]
                   if "description_set" in committed else snapshot.description)
    if "archive" in committed:
        visibility = "archive"
    elif "timeline" in committed:
        visibility = "timeline"
    else:
        visibility = snapshot.visibility
    write_changes_and_state(
        conn, asset_id=asset_id, job_id=job_id, changes=[],
        current_tag=tag, current_markers=list(markers), visibility=visibility,
        rating=rating, description=description,
    )


def _apply_failed(db, client, asset_id: str, *, error, max_attempts,
                  skip_marker_tag_name: str) -> None:
    """失败：不写 applied_at、不摘队列标签；计数 +1，达上限则放弃（§7.3）。"""
    n = bumped_apply_attempts(db, asset_id)
    if n < max_attempts:
        log_event(db, "system", "apply_retry",
                  {"asset_id": asset_id, "attempts": n, "error": error})
        return
    log_event(db, "system", "apply_gave_up",
              {"asset_id": asset_id, "attempts": n, "error": error})
    _tag_gave_up(db, client, asset_id, skip_marker_tag_name)


def _tag_gave_up(db, client, asset_id: str, tag_name: str) -> None:
    """给"放弃重试"的资产打标记标签（§7.3），并落账（§5.5 不变式）。

    打标失败不能反过来把管线搞挂：放弃本身就是终态，标记只是给人的线索。
    """
    try:
        tag_ids = client.ensure_tags([tag_name])
        client.tag_assets([tag_ids[tag_name]], [asset_id])
    except Exception as exc:                      # noqa: BLE001
        log_event(db, "system", "apply_gave_up_tag_failed",
                  {"asset_id": asset_id, "error": str(exc)})
        return
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
            "VALUES (NULL, ?, 'tag_add', 'apply', ?)",
            (asset_id, json.dumps({"tag_name": tag_name}, ensure_ascii=False)),
        )


def _analysis_failed(db, client, asset_id: str, exc: BaseException,
                     cfg: PipelineConfig, *, dry_run: bool,
                     job_id: int | None) -> None:
    """分析阶段失败：留痕 + 对不可恢复的失败做有限次重试后的终态处理。

    每张图各自一个 job、各自 apply —— 一张图失败不影响别的图（队列也不会因此
    被取消）。但"永远失败的图"会一直挂在队列里、每轮 poll 都失败一次，所以
    不可恢复的失败走与 apply 失败**同一套计数与上限**：到顶就打失败标记
    （配置中心 on_refusal 规则对应的「识别失败」），并让这张图不再被重扫
    （apply_attempts 达上限 → `_already_done` 为真 → 下一次 scan 的 heal 路径
    顺手摘掉队列标签，图片从此结束重试）。

    失败的**原因必须留痕**：原先这里什么都不记，坏图只能靠猜（真机：一张
    0 字节的 HEIC 每轮失败一次，UI 上只有一个"classification failed"）。
    """
    permanent = _unrecoverable(exc)
    log_event(db, "system", "analysis_failed", {
        "asset_id": asset_id, "job_id": job_id, "error": str(exc),
        "unrecoverable": permanent, "dry_run": dry_run})
    if not permanent or dry_run or client is None:
        return
    attempts = bumped_apply_attempts(db, asset_id)
    if attempts < cfg.apply_max_attempts:
        log_event(db, "system", "analysis_retry",
                  {"asset_id": asset_id, "attempts": attempts, "error": str(exc)})
        return
    log_event(db, "system", "analysis_gave_up",
              {"asset_id": asset_id, "attempts": attempts, "error": str(exc)})
    _tag_failed(db, client, asset_id, cfg)


def _tag_failed(db, client, asset_id: str, cfg: PipelineConfig) -> None:
    """给"识别失败"的图打标记标签（配置中心 on_refusal 规则，如「识别失败」）。

    没配这类规则时只记事件、不臆造标签名；打标失败也不能反过来把管线搞挂
    —— 放弃本身就是终态，标记只是给人的线索（同 §7.3 的 _tag_gave_up）。
    """
    names = refusal_marker_names(cfg.marker_rules)
    if not names:
        log_event(db, "system", "analysis_gave_up_untagged",
                  {"asset_id": asset_id, "reason": "未配置 on_refusal marker 规则"})
        return
    try:
        tag_ids = client.ensure_tags(list(names))
        client.tag_assets([tag_ids[n] for n in names], [asset_id])
    except Exception as exc:                      # noqa: BLE001
        log_event(db, "system", "analysis_gave_up_tag_failed",
                  {"asset_id": asset_id, "error": str(exc)})
        return
    with db.connect() as conn:
        for name in names:
            conn.execute(
                "INSERT INTO change_log (job_id, asset_id, action, direction, payload) "
                "VALUES (NULL, ?, 'tag_add', 'apply', ?)",
                (asset_id, json.dumps({"tag_name": name}, ensure_ascii=False)))


def _primary_names(cfg: PipelineConfig) -> list[tuple[str, str]]:
    return [(t.name, t.description) for t in cfg.tags
            if t.kind == "primary" and t.enabled]


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _write_record(db, asset_id, *, source, tag, confidence, markers, model, usage,
                  status, raw_response, llm_request, dry_run, job_id,
                  prompt_version_id, duration_ms, filename=None,
                  rating=None, description=None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, prompt_version_id, source, "
            "  predicted_tag, confidence, markers, model, usage, duration_ms, "
            "  status, raw_response, llm_request, dry_run, filename, "
            "  rating, description) "
            "VALUES (:aid, :job_id, :pv_id, :source, :tag, :conf, :markers, "
            "        :model, :usage, :dur, :status, :raw, :req, :dry, :fname, "
            "        :rating, :desc)",
            {
                "aid": asset_id, "job_id": job_id, "pv_id": prompt_version_id,
                "source": source, "tag": tag, "conf": confidence,
                "markers": json.dumps(list(markers)), "model": model,
                "usage": json.dumps(usage) if usage is not None else None,
                "dur": duration_ms, "status": status, "raw": raw_response,
                "req": llm_request,
                "dry": 1 if dry_run else 0,
                "fname": filename,
                "rating": rating, "desc": description,
            },
        )


def _reusable_analysis(db, asset_id: str):
    """重试复用：返回上次已分析但未应用的判定（§7.3 层 1）。

    只在**最新一条记录**满足 `status='ok' AND dry_run=0 AND applied_at IS NULL`
    时复用：
      · status='error'/'refused' → 没有可用判定
      · applied_at 非空 → 那条已被应用过，复用它等于拿旧结果盖新状态
    """
    from app.classify.parse import Classification

    with db.connect() as conn:
        row = conn.execute(
            "SELECT status, predicted_tag, confidence, markers, rating, "
            "       description, filename, applied_at "
            "  FROM asset_records WHERE asset_id = ? AND dry_run = 0 "
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (asset_id,),
        ).fetchone()
    if (row is None or row["status"] != "ok"
            or row["applied_at"] is not None
            or row["predicted_tag"] is None):
        return None
    markers = json.loads(row["markers"] or "[]")
    cls = Classification(
        tag=row["predicted_tag"], confidence=row["confidence"],
        sensitive="sensitive" in markers, reason=None, markers=tuple(markers),
        rating=row["rating"], description=row["description"],
    )
    return {"cls": cls, "filename": row["filename"]}
