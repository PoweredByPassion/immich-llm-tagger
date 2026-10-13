"""配置完整性校验。纯函数，不碰数据库。

三条规则：
1. marker_rules.marker_tag_id 指向的 tag 必须存在且 kind = marker；
2. settings.fallback_tag_name 对应的 tag 必须存在、kind = primary 且 enabled；
3. settings.active_prompt_version_id 若非 null 则必须存在。
"""
from __future__ import annotations

from dataclasses import dataclass

from app.store.errors import Problem, ValidationError


@dataclass(frozen=True)
class TagRow:
    id: int
    name: str
    kind: str
    enabled: bool


def validate_config(
    *,
    tags: list[TagRow],
    marker_rules: list[tuple[int, int]],
    fallback_tag_name: str,
    active_prompt_version_id: int | None,
    prompt_version_ids: set[int],
) -> None:
    problems: list[Problem] = []
    by_id = {t.id: t for t in tags}
    by_name = {t.name: t for t in tags}

    for rule_id, marker_tag_id in marker_rules:
        tag = by_id.get(marker_tag_id)
        if tag is None:
            problems.append(Problem(
                "store.validation.marker_tag_missing", (rule_id, marker_tag_id)
            ))
        elif tag.kind != "marker":
            problems.append(Problem(
                "store.validation.marker_tag_kind", (rule_id, tag.name, tag.kind)
            ))

    fallback = by_name.get(fallback_tag_name)
    if fallback is None:
        problems.append(Problem(
            "store.validation.fallback_missing", (fallback_tag_name,)
        ))
    else:
        if fallback.kind != "primary":
            problems.append(Problem(
                "store.validation.fallback_kind", (fallback_tag_name,)
            ))
        if not fallback.enabled:
            problems.append(Problem(
                "store.validation.fallback_disabled", (fallback_tag_name,)
            ))

    if (
        active_prompt_version_id is not None
        and active_prompt_version_id not in prompt_version_ids
    ):
        problems.append(Problem(
            "store.validation.active_prompt_missing", (active_prompt_version_id,)
        ))

    if problems:
        raise ValidationError(problems)
