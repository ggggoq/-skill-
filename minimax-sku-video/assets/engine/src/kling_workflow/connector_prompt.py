"""Deterministic conversion from the reviewed prompt to the connector payload prompt.

The reviewed prompt (``可灵MCP提交提示词.txt``) is written for a human reviewer and
carries internal evidence rows.  The Kling connector needs the same text with the
element reference resolved to the numeric element id and without those internal
rows.  This module performs exactly that mechanical rewrite so a submission never
depends on hand-edited prompt text.
"""

from __future__ import annotations

import re

from .prompting import PromptError

# Evidence rows that are meaningful locally but must not reach the model.
_INTERNAL_PREFIXES = (
    "- audio_policy：",
    "- element_fingerprint：",
    "- prepared：",
    "- project_request_text：",
    "- scene_image_manifest：",
    "- video_manifest：",
    "- schema_version：",
    "- sku：",
    "- review_standard：",
    "- 引用映射：",
)
# Analysis section built only from local evidence rows; the connector prompt keeps
# the action timeline in the shot body instead.
_DROP_SECTIONS = ("## 参考动作与镜头分析", "## 参考素材映射")
_RESIDUAL_MARKERS = ("素材映射按文件名", "离线审核和人工终审")
_ELEMENT_ID = re.compile(r"^[0-9]{6,24}$")


def build_connector_prompt(markdown: str, element_id: str) -> str:
    """Return the connector prompt for one reviewed markdown prompt."""
    if not isinstance(markdown, str) or not markdown.strip():
        raise PromptError("待转换的提示词为空")
    if not isinstance(element_id, str) or not _ELEMENT_ID.fullmatch(element_id):
        raise PromptError("连接器主体编号必须是数字")
    if "<<<element_1>>>" not in markdown:
        raise PromptError("待转换的提示词缺少 <<<element_1>>> 占位符")

    lines = markdown.splitlines()
    referenced = _element_materials(lines)
    action = _mapping_value(lines, "动作参考视频")
    scene = _mapping_value(lines, "场景参考")
    if not referenced:
        raise PromptError("参考素材映射缺少主参考图或服装细节，无法生成主体映射")

    kept: list[str] = []
    skipping = False
    skip_residual = False
    insert_at: int | None = None
    for line in lines:
        if line.startswith("## "):
            skipping = line.startswith(_DROP_SECTIONS)
            skip_residual = False
            if line.startswith("## 参考素材映射") and insert_at is None:
                insert_at = len(kept)
            if skipping:
                continue
            kept.append(line)
            continue
        if skipping or skip_residual:
            continue
        if any(marker in line for marker in _RESIDUAL_MARKERS):
            skip_residual = True
            continue
        if line.startswith("- ") and line[2:].startswith(_INTERNAL_PREFIXES):
            continue
        kept.append(line)

    block = ["## 参考素材映射", ""]
    block.append(
        f"- <<<{element_id}>>> ← 主体（角色+服饰）：主参考图与服装细节 {'、'.join(referenced)}"
    )
    if action:
        block.append(f"- 视频1 ← 动作参考视频：{action}")
    if scene:
        block.append(f"- 图片1 ← 场景参考：{scene}")
    block.append("")

    insert_at = len(kept) if insert_at is None else insert_at
    result = [*kept[:insert_at], *block, *kept[insert_at:]]
    text = "\n".join(result)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("<<<element_1>>>", f"<<<{element_id}>>>")
    if "<<<element_1>>>" in text:
        raise PromptError("元素占位符替换失败")
    return text if text.endswith("\n") else text + "\n"


def _mapping_value(lines: list[str], label: str) -> str | None:
    prefix = f"- {label}："
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def _element_materials(lines: list[str]) -> list[str]:
    values: list[str] = []
    prefix = "- <<<element_1>>> ← 主参考图："
    detail_prefix = "- 服装细节参考："
    for line in lines:
        if line.startswith(prefix):
            values.extend(item.strip() for item in line[len(prefix) :].split("、") if item.strip())
        elif line.startswith(detail_prefix):
            values.extend(
                item.strip() for item in line[len(detail_prefix) :].split("、") if item.strip()
            )
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen
