"""Prompt 渲染器：把 PromptContext 填进 prompt 模板。"""

from dataclasses import dataclass, field
from typing import Any

MANDATORY_PLACEHOLDER = "{{TAG_LIST}}"

# 可选占位符 → ctx 上的字段名；缺失时整行省略
OPTIONAL_PLACEHOLDERS = {
    "{{FILENAME}}": "filename",
    "{{EXIF}}": "exif",
    "{{SCOPE}}": "scope",
    "{{DATE}}": "date",
}


@dataclass(frozen=True)
class PromptContext:
    tag_list: list[tuple[str, str]] = field(default_factory=list)
    filename: str | None = None
    exif: dict[str, Any] | None = None
    scope: str | None = None
    date: str | None = None


def build_tag_list_block(tags: list[tuple[str, str]]) -> str:
    return "\n".join(f"- {name}: {description}" for name, description in tags)


def _format_exif(exif: dict[str, Any]) -> str | None:
    lines = []
    for key, label in [
        ("make", "厂商"),
        ("model", "型号"),
        ("lens_model", "镜头"),
        ("focal_length", "焦距"),
        ("iso", "ISO"),
        ("f_number", "光圈"),
        ("exposure_time", "快门"),
        ("date_original", "拍摄时间"),
        ("city", "城市"),
    ]:
        value = exif.get(key)
        if value is None:
            continue
        lines.append(f"{label}: {value}")
    if not lines:
        return None
    return "\n".join(lines)


def render_prompt(template: str, ctx: PromptContext) -> str:
    out = template.replace(MANDATORY_PLACEHOLDER, build_tag_list_block(ctx.tag_list))
    values = {
        "{{FILENAME}}": ctx.filename,
        "{{EXIF}}": _format_exif(ctx.exif) if ctx.exif else None,
        "{{SCOPE}}": ctx.scope,
        "{{DATE}}": ctx.date,
    }
    lines = out.split("\n")
    kept = []
    for line in lines:
        dropped = False
        for ph, val in values.items():
            if ph in line and val is None:
                dropped = True
            elif ph in line:
                line = line.replace(ph, str(val))
        if not dropped:
            kept.append(line)
    return "\n".join(kept)
