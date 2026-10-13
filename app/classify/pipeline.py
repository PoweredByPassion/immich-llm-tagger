"""单张处理管线：把租约 / 取图 / prompt / LLM / 归一 / 记录 / 应用串起来。

本模块负责管线编排，单张失败不抛异常穿透（status="error"），
是否重试由上层 job 引擎决定。
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.classify.apply import ApplyPlan, apply_plan
from app.classify.lease import LLM_MAX_RETRIES, claim, lease_seconds_for, release, renew
from app.classify.parse import decide_archive, extract_json, normalize
from app.classify.prompt import PromptContext, render_prompt
from app.classify.thumb import ThumbConfig, fetch_and_encode
from app.db.connection import Database
from app.store.connections import LLMConn
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
                        override_manual=override_manual)
    except Exception:
        return _error_result(asset_id, dry_run)
    finally:
        release(db, asset_id, cfg.concurrency_owner)


def _process(db, client, llm, asset_id, cfg, *, source, dry_run, job_id,
             prompt_version_id, asset_hint, lease_seconds,
             override_manual) -> PipelineResult:
    started = time.monotonic()
    # 取图/渲染 prompt 阶段就失败时没有请求可记，error 路径留 None。
    request_snapshot: str | None = None
    filename = None
    try:
        # 未勾选多模态：不发图，也省掉一次取图（fetch_and_encode）。
        image_b64 = fetch_and_encode(client, asset_id, cfg.thumb) \
            if cfg.llm.multimodal else None
        filename, exif, date = _asset_meta(client, asset_id, asset_hint)
        primary = [(t.name, t.description) for t in cfg.tags
                   if t.kind == "primary" and t.enabled]
        prompt = render_prompt(cfg.prompt_template, PromptContext(
            tag_list=primary, filename=filename, exif=exif, scope=None, date=date,
        ))
        messages = _messages(prompt, image_b64)
        request_snapshot = _llm_request_snapshot(llm, messages)
        result = llm.chat(messages)
        renew(db, asset_id, cfg.concurrency_owner, lease_seconds)  # 每次 LLM 成功续租
    except Exception:
        _write_record(db, asset_id, source=source, tag=None, confidence=None,
                      markers=(), model=None, usage=None, status="error",
                      raw_response=None, llm_request=request_snapshot,
                      dry_run=dry_run, job_id=job_id,
                      prompt_version_id=prompt_version_id,
                      duration_ms=_ms(started), filename=filename)
        return _error_result(asset_id, dry_run)

    raw_text = result.content
    raw = extract_json(raw_text)
    if raw is None and _is_refusal(raw_text):
        cls = normalize(None, refusal_text=raw_text,
                        enabled_primary_tags={n for n, _ in _primary_names(cfg)},
                        fallback_tag=cfg.fallback_tag,
                        marker_rules=cfg.marker_rules)
        status = "refused"
    elif raw is None:
        # 非拒答也解析不出 JSON：按拒答归一（sensitive marker + fallback），
        # 状态记 refused（LLM 给了正文但不符合输出契约）。
        cls = normalize(None, refusal_text=raw_text,
                        enabled_primary_tags={n for n, _ in _primary_names(cfg)},
                        fallback_tag=cfg.fallback_tag,
                        marker_rules=cfg.marker_rules)
        status = "refused"
    else:
        cls = normalize(raw, refusal_text=None,
                        enabled_primary_tags={n for n, _ in _primary_names(cfg)},
                        fallback_tag=cfg.fallback_tag,
                        marker_rules=cfg.marker_rules)
        status = "ok"

    duration_ms = _ms(started)
    _write_record(db, asset_id, source=source, tag=cls.tag,
                  confidence=cls.confidence, markers=cls.markers,
                  model=result.model, usage=result.usage, status=status,
                  raw_response=raw_text, llm_request=request_snapshot,
                  dry_run=dry_run, job_id=job_id,
                  prompt_version_id=prompt_version_id, duration_ms=duration_ms,
                  filename=filename)

    if dry_run:
        return PipelineResult(asset_id=asset_id, tag=cls.tag,
                              confidence=cls.confidence, markers=cls.markers,
                              status=status, dry_run=True)

    selected = next((t for t in cfg.tags
                     if t.kind == "primary" and t.enabled and t.name == cls.tag), None)
    archive = decide_archive(selected, cls.confidence, cfg.default_min_confidence)
    plan = ApplyPlan(
        asset_id=asset_id,
        primary_tag=cls.tag,
        marker_tags=cls.markers,
        archive=archive,
        album=selected.album if selected is not None else None,
        queue_tag=cfg.queue_tag_name,
        skip_immich=selected.skip_immich if selected is not None else False,
    )
    try:
        # override_manual 非 None 时替代 DB 读取（reclassify 用户显式发起→全量 apply）
        if override_manual is not None:
            effective_manual_override = override_manual
        else:
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT manual_override FROM asset_state WHERE asset_id = ?",
                    (asset_id,),
                ).fetchone()
            effective_manual_override = bool(row["manual_override"]) if row else False
        tag_ids = client.ensure_tags([cfg.queue_tag_name])
        apply_plan(db, client, plan, job_id=job_id,
                   queue_tag_id=tag_ids[cfg.queue_tag_name],
                   manual_override=effective_manual_override)
    except Exception:
        # 分类已成功且 asset_records 已落（记录描述"分类过"，变更描述"改过"）。
        # apply 失败不改写结果状态，重试由上层决定。
        pass
    return PipelineResult(asset_id=asset_id, tag=cls.tag, confidence=cls.confidence,
                          markers=cls.markers, status=status, dry_run=False)


def _primary_names(cfg: PipelineConfig) -> list[tuple[str, str]]:
    return [(t.name, t.description) for t in cfg.tags
            if t.kind == "primary" and t.enabled]


def _is_refusal(text: str) -> bool:
    from app.classify.parse import is_refusal
    return is_refusal(text)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _write_record(db, asset_id, *, source, tag, confidence, markers, model, usage,
                  status, raw_response, llm_request, dry_run, job_id,
                  prompt_version_id, duration_ms, filename=None) -> None:
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO asset_records (asset_id, job_id, prompt_version_id, source, "
            "  predicted_tag, confidence, markers, model, usage, duration_ms, "
            "  status, raw_response, llm_request, dry_run, filename) "
            "VALUES (:aid, :job_id, :pv_id, :source, :tag, :conf, :markers, "
            "        :model, :usage, :dur, :status, :raw, :req, :dry, :fname)",
            {
                "aid": asset_id, "job_id": job_id, "pv_id": prompt_version_id,
                "source": source, "tag": tag, "conf": confidence,
                "markers": json.dumps(list(markers)), "model": model,
                "usage": json.dumps(usage) if usage is not None else None,
                "dur": duration_ms, "status": status, "raw": raw_response,
                "req": llm_request,
                "dry": 1 if dry_run else 0,
                "fname": filename,
            },
        )
