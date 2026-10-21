"""LLM output parsing, refusal detection, normalization and archive policy."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

REFUSAL_MARKERS = (
    "敏感内容", "无法响应", "无法回答", "不能回答", "抱歉",
    "i can't", "i cannot", "i'm unable", "i am unable", "i won't",
)


def extract_json(text: str) -> dict | None:
    t = text.strip()
    # 剥 markdown 围栏
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t).strip()
    # 围栏剥离后可能仍带尾部散文，先尝试直接 loads，
    # 再用括号配对截取首个平衡的 {...} 块兜底
    obj = _try_loads(t)
    if obj is not None:
        return obj
    m = re.search(r"\{", t)
    if m:
        # 兜底：从首个 { 起，返回第一个可解析的平衡块
        start = m.start()
        while True:
            block = _balanced_block(t, start)
            if block is None:
                break
            obj = _try_loads(block)
            if obj is not None:
                return obj
            nxt = t.find("{", start + 1)
            if nxt == -1:
                break
            start = nxt
    return None


def _try_loads(s: str) -> dict | None:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _balanced_block(s: str, start: int) -> str | None:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start : i + 1]
    return None


def is_refusal(text: str) -> bool:
    t = text.strip()
    if t.startswith("{"):
        return False
    low = t.lower()
    return any(m.lower() in low for m in REFUSAL_MARKERS)


def refusal_marker_names(marker_rules: list[tuple[str, dict]]) -> tuple[str, ...]:
    """挑出"拒答触发"的 marker 标签名（condition 带 on_refusal）。

    配置中心（Marker 规则页）可给任意 kind=marker 的标签挂一条 on_refusal 规则，
    模型拒答时该标签就会被打上。顺序按配置，同名去重。
    """
    out: list[str] = []
    for name, cond in marker_rules:
        if cond.get("on_refusal") and name not in out:
            out.append(name)
    return tuple(out)


def _unique(names) -> tuple[str, ...]:
    return tuple(dict.fromkeys(names))


@dataclass(frozen=True)
class Classification:
    tag: str
    confidence: float | None
    sensitive: bool
    reason: str | None
    markers: tuple[str, ...]
    rating: int | None = None
    description: str | None = None


def normalize(
    raw: dict | None,
    *,
    refusal_text: str | None,
    enabled_primary_tags: set[str],
    fallback_tag: str,
    marker_rules: list[tuple[str, dict]],
) -> Classification:
    # 拒答 marker（配置中心 Marker 规则页的 on_refusal 规则）：模型没能给出
    # 可用判定时打上，让人在 Immich 里能一键筛出"识别失败"的图。
    refusal_markers = refusal_marker_names(marker_rules)

    if refusal_text is not None and is_refusal(refusal_text):
        return Classification(
            tag=fallback_tag, confidence=None, sensitive=True, reason=None,
            # 内置规则：拒答 → sensitive marker + 配置的拒答 marker
            markers=_unique(("sensitive", *refusal_markers)),
        )

    tag = fallback_tag
    if raw and isinstance(raw.get("tag"), str) and raw["tag"] in enabled_primary_tags:
        tag = raw["tag"]

    confidence: float | None = None
    if raw:
        conf = raw.get("confidence")
        if isinstance(conf, (int, float)) and not isinstance(conf, bool):
            c = float(conf)
            if 0.0 <= c <= 1.0:
                confidence = c

    sensitive = bool(raw.get("sensitive")) if raw else False
    reason = raw.get("reason") if raw and isinstance(raw.get("reason"), str) else None

    markers: list[str] = []
    if sensitive:
        markers.append("sensitive")
    for name, cond in marker_rules:
        try:
            hit = "confidence_lt" in cond and confidence is not None and confidence < float(cond["confidence_lt"])
        except (TypeError, ValueError):
            continue
        if hit:
            markers.append(name)

    if raw is None:
        # 走到这里 = 文本既不是合法 JSON，也没命中拒答词（典型是模型输出散文
        # 或残缺 JSON）。pipeline 对这两种情形都记 status="refused"，所以拒答
        # marker 必须一并打上 —— 否则"Immich 里带 failed 标签的图"与"UI 显示
        # refused 的图"对不上。
        markers.extend(n for n in refusal_markers if n not in markers)

    rating: int | None = None
    if raw:
        r = raw.get("rating")
        # bool 是 int 的子类，必须显式排除
        if isinstance(r, int) and not isinstance(r, bool) and 1 <= r <= 5:
            rating = r

    description: str | None = None
    if raw:
        d = raw.get("description")
        if isinstance(d, str) and d.strip():
            description = d.strip()

    return Classification(
        tag=tag, confidence=confidence, sensitive=sensitive,
        reason=reason, markers=tuple(markers),
        rating=rating, description=description,
    )


@dataclass(frozen=True)
class TagDef:
    """Tag 配置快照。"""

    name: str
    archive: bool
    min_confidence: float | None = None


def decide_archive(
    tag: TagDef | None,
    confidence: float | None,
    default_min_confidence: float,
) -> bool:
    if tag is None or confidence is None or not tag.archive:
        return False
    threshold = tag.min_confidence if tag.min_confidence is not None else default_min_confidence
    return confidence >= threshold
