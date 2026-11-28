from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from PIL import Image

from .assets import PreparedAssets
from .models import ShotPlan
from .video_analysis import SEMANTIC_FIELDS


GARMENT_PROTECTION_SUFFIX = (
    "服装以元素为准，忽略参考视频里那个人穿的衣服："
    "款式、版型、长度、颜色、面料纹理、印花、纽扣、拉链、口袋及其他可见装饰不变；"
    "全片人物与服装唯一且不变；"
    "转身或任何姿态变化只能由同一身体连续旋转完成，依次经过正面→侧面→背面，"
    "不得切镜、不得重置人物、不得换装；"
    "每帧侧面与背面结构必须与该侧参考图一致；"
    "不新增外套、配饰、文字、商标或参考图中不存在的结构。"
)
# Legacy tail kept only so an older saved prompt can be rebuilt through the
# current contract instead of being hand-edited.
LEGACY_GARMENT_PROTECTION_SUFFIX = (
    "服装保持与参考元素一致：款式、版型、长度、颜色、面料纹理、印花、纽扣、拉链、口袋及其他可见装饰不变；"
    "不新增外套、配饰、文字、商标或参考图中不存在的结构。"
)
ELEMENT_PRIORITY_MARKERS = ("以元素为准", "忽略参考视频里那个人穿的衣服")
TURN_CONTINUITY_RULE = (
    "转身必须由同一身体连续旋转完成，依次经过正面→侧面→背面，"
    "不得在转身瞬间替换人物、重置画面或换装"
)
OBJECT_CONTINUITY_SUFFIX = (
    "物品连续性以主参考图和输出首帧为准：任一基准中未出现或无法确认的包、饰品、配件、手持道具及其他物品，"
    "全程不得新增、凭空出现、消失后重现；"
    "动作参考中的包、手提袋、提绳一律忽略，人物双手保持空手；"
    "插兜硬规则：动作参考中有插兜而主参考图无口袋或无法确认口袋时，禁止插兜，"
    "双手自然垂放或可见摆动，手不得插入衣物或与衣物穿插；"
    "全身穿着（含鞋袜配饰）须与场景参考图一致，禁止混合不同单品特征；冲突时以服装描述为准；"
    "动作参考中的其他冲突物品同样忽略，只保留不依赖该物品的动作；"
    "无法保证从首帧到结尾稳定一致时，宁可全程不出现。"
)
MUSIC_ONLY_AUDIO_RULE = (
    "参考视频没有音轨时，改为生成无歌词、无对白、无环境音，仅生成纯音乐；"
    "纯音乐必须是欢快明亮、轻快活泼的儿童向背景音乐，使用明亮大调、跳跃而稳定的中快节奏、"
    "钢琴/木琴/尤克里里一类清亮音色；禁止低沉、缓慢、忧伤、悲凉、史诗或悬疑的曲风，"
    "禁止小调慢板与沉重低音铺底；音乐必须贯穿全片，时长与成片视频时长完全一致。"
)
JUMP_ACTION_POLICY = (
    "跳跃动作默认删除；优先使用同组参考视频后 5 秒内的其他有效动作替代；"
    "只有后 5 秒没有可用替代动作时才保留，并把跳跃放在成片结尾。"
)
PORTRAIT_OUTPUT_RULE = (
    "仅允许竖版，画面高度必须大于宽度，固定 9:16；禁止生成横版；不得跟随横版动作参考改变输出方向。"
)
_FIXED_PROMPT_TAIL = GARMENT_PROTECTION_SUFFIX + OBJECT_CONTINUITY_SUFFIX
_VIDEO_MANIFEST_KEYS = frozenset(
    {"schema_version", "status", "semantic_analysis", "videos", "failures"}
)
_VIDEO_ROW_KEYS = frozenset(
    {
        "path",
        "status",
        "sha256",
        "backend_name",
        "frame_count",
        "fps",
        "width",
        "height",
        "duration_seconds",
        "sample_timestamps",
        "scene_boundaries",
        "contact_sheet",
        "error",
        "semantic_review",
        "absolute_path",
        "file_size_bytes",
        "mtime_ns",
        "aspect_ratio",
        "audio_status",
        "audio_duration_seconds",
        "decode_status",
        "key_frames",
        "action_timeline",
        "shot_table",
        "camera_angle",
        "person_orientation",
        "transitions",
        "final_conclusion",
    }
)
_CUT_EDIT_TERMS = re.compile(r"切镜|切换镜头|镜头切换|剪辑|转场")
_CONTINUOUS_SHOT_CUES = re.compile(
    r"一镜到底|一镜完成|一镜拍完|单一镜头|单镜头|同一镜头|一个镜头|"
    r"连续镜头|长镜头|镜头(?:不间断|不中断|不断|连续|持续)"
)
_NEGATION_BEFORE_CUE = re.compile(
    r"(?:没有|并无|未见|未发生|未出现|不存在|不擅自|不应|不得|禁止|避免|不要|无需|不能|并非|不是|无|未|不|非)"
    r"[^，。；,;!?！？]{0,6}$"
)
_SEMANTIC_ANALYSIS_KEYS = frozenset(
    {"instruction", "fields", "per_video_template"}
)
_OBJECT_BASELINE_MARKER = re.compile(
    r"<<<object_baseline:(包|饰品|配件|手持道具|道具|手提物)\|主参考图与输出首帧均确认存在>>>"
)
_MALFORMED_OBJECT_BASELINE_MARKER = re.compile(r"<<<object_baseline:[^>]*>>>")
_OBJECT_ALIASES = {
    "包": ("手提袋", "手袋", "背包", "提包", "挎包", "皮包", "箱包", "包"),
    "饰品": ("饰品", "首饰", "项链", "耳环", "耳饰", "手链", "戒指", "发饰"),
    "配件": ("配件",),
    "手持道具": ("手持道具", "手机", "雨伞", "花束", "书本", "水杯"),
    "道具": ("道具",),
    "手提物": ("手提物", "行李箱"),
}
_OBJECT_ALIAS_TO_CATEGORY = {
    alias: category
    for category, aliases in _OBJECT_ALIASES.items()
    for alias in aliases
}
_OBJECT_TERM = re.compile(
    "|".join(
        re.escape(alias)
        for alias in sorted(
            (*_OBJECT_ALIAS_TO_CATEGORY, "物品"), key=len, reverse=True
        )
    )
)
_NEW_OBJECT_CONFLICT = re.compile(
    r"突然|凭空|新增|新生成|多了|多出|增加|冒出|变出|"
    r"中途出现|后续出现|后来出现|首次出现|出现(?:了)?(?:一|新)|"
    r"画面里出现|画面中出现|从画面外|从镜头外|接过|递来|递给|"
    r"拿到|得到|获得|拿起新|取出新|生成(?:了)?(?:一|新)|"
    r"消失|离开画面|离开镜头|扔掉|丢掉"
)


class PromptError(ValueError):
    """A user-actionable error in the reviewable shot prompt or Kling payload."""


def validate_shots(shots: Sequence[ShotPlan], duration: int | float) -> None:
    if not isinstance(shots, Sequence) or isinstance(shots, (str, bytes)):
        raise PromptError("分镜结构无效")
    if not 1 <= len(shots) <= 6:
        raise PromptError("分镜必须为 1–6 段")
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(float(duration))
        or duration <= 0
    ):
        raise PromptError("请求总时长必须是正数秒")
    for expected_index, shot in enumerate(shots, start=1):
        if not isinstance(shot, ShotPlan):
            raise PromptError("分镜结构无效")
        if (
            not isinstance(shot.index, int)
            or isinstance(shot.index, bool)
            or shot.index != expected_index
        ):
            raise PromptError("分镜编号必须从 1 开始连续递增")
        if (
            not isinstance(shot.duration, (int, float))
            or isinstance(shot.duration, bool)
            or not math.isfinite(float(shot.duration))
            or shot.duration <= 0
        ):
            raise PromptError("每段分镜至少 1 秒")
    total = sum((_seconds_decimal(shot.duration) for shot in shots), Decimal(0))
    if total != _seconds_decimal(duration):
        raise PromptError(f"分镜总时长必须等于 {_format_seconds(duration)} 秒")
    for shot in shots:
        if not isinstance(shot.prompt, str):
            raise PromptError("单段提示词必须是文本")
        if len(shot.prompt) > 512:
            raise PromptError("单段提示词不得超过 512 个字符")
        if "<<<element_1>>>" not in shot.prompt:
            raise PromptError("每段提示词必须包含 <<<element_1>>> 服装参考占位符")
        if GARMENT_PROTECTION_SUFFIX not in shot.prompt:
            raise PromptError("每段提示词必须包含固定服装保护约束")
        for marker in ELEMENT_PRIORITY_MARKERS:
            if marker not in shot.prompt:
                raise PromptError(f"每段提示词必须写明元素优先约束：{marker}")
        if OBJECT_CONTINUITY_SUFFIX not in shot.prompt:
            raise PromptError("每段提示词必须包含固定物品连续性约束")
        if (
            shot.prompt.count(GARMENT_PROTECTION_SUFFIX) != 1
            or shot.prompt.count(OBJECT_CONTINUITY_SUFFIX) != 1
            or not shot.prompt.endswith(_FIXED_PROMPT_TAIL)
        ):
            raise PromptError("两条固定保护约束必须各出现一次，并按服装保护、物品连续性的顺序位于提示词末尾")
        _validate_object_actions(shot.prompt)


def refresh_fixed_tail(prompt: str) -> str:
    """Rebuild one saved shot prompt against the current fixed-tail contract.

    Regenerating a prompt must stay a pipeline operation: the authored action
    text is preserved verbatim, only the fixed protection tail is refreshed.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        raise PromptError("分镜提示词必须是非空文本")
    body = prompt
    for suffix in (
        GARMENT_PROTECTION_SUFFIX,
        LEGACY_GARMENT_PROTECTION_SUFFIX,
        OBJECT_CONTINUITY_SUFFIX,
    ):
        body = body.replace(suffix, "")
    body = body.strip()
    if "<<<element_1>>>" not in body:
        raise PromptError("分镜提示词缺少 <<<element_1>>> 服装参考占位符，无法按当前约束重建")
    return f"{body}{GARMENT_PROTECTION_SUFFIX}{OBJECT_CONTINUITY_SUFFIX}"


def _validate_object_actions(prompt: str) -> None:
    action_text = prompt[: -len(_FIXED_PROMPT_TAIL)]
    markers = list(_OBJECT_BASELINE_MARKER.finditer(action_text))
    for marker in _MALFORMED_OBJECT_BASELINE_MARKER.finditer(action_text):
        if not _OBJECT_BASELINE_MARKER.fullmatch(marker.group(0)):
            raise PromptError("物品基准确认标记格式无效")
    confirmed_objects = {marker.group(1) for marker in markers}
    if len(confirmed_objects) != len(markers):
        raise PromptError("物品基准确认标记重复或无法区分具体物品")

    action_without_markers = _OBJECT_BASELINE_MARKER.sub("", action_text)
    for clause in re.split(r"[。；，,!?！？\n]", action_without_markers):
        mentions = list(_OBJECT_TERM.finditer(clause))
        if not mentions:
            continue
        if _NEW_OBJECT_CONFLICT.search(clause):
            raise PromptError("物品动作与基准状态冲突：禁止新增、突然出现、中途获得或移除物品")
        for mention in mentions:
            alias = mention.group(0)
            if alias == "物品":
                raise PromptError("物品描述过于笼统：请明确具体类别并人工确认基准")
            category = _OBJECT_ALIAS_TO_CATEGORY[alias]
            if category not in confirmed_objects:
                raise PromptError(
                    "物品动作与基准状态冲突：只有主参考图与输出首帧均确认存在的同一类别物品才可参与动作"
                )

    for marker in markers:
        category = marker.group(1)
        if not any(
            _OBJECT_ALIAS_TO_CATEGORY[mention.group(0)] == category
            for mention in _OBJECT_TERM.finditer(action_without_markers)
            if mention.group(0) != "物品"
        ):
            raise PromptError(
                "物品基准确认标记没有对应的具体物品描述，请删除无效标记或补充已确认动作"
            )


def _shot_total_duration(shots: Sequence[ShotPlan]) -> int | float:
    if not isinstance(shots, Sequence) or isinstance(shots, (str, bytes)):
        raise PromptError("分镜结构无效")
    if any(not isinstance(shot, ShotPlan) for shot in shots):
        raise PromptError("分镜结构无效")
    total = sum((_seconds_decimal(shot.duration) for shot in shots), Decimal(0))
    if total == total.to_integral_value():
        return int(total)
    return float(total)


def _seconds_decimal(value: int | float) -> Decimal:
    """Sum user-entered decimal seconds without binary-float artifacts."""
    return Decimal(str(value))


def _format_seconds(value: int | float) -> str:
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    # repr() preserves the shortest round-trippable decimal instead of
    # rounding a measured fractional duration for display or review.
    return repr(value)


def validate_api_duration_contract(shots: Sequence[ShotPlan]) -> int:
    """Fail closed if the current REST API cannot represent planned seconds exactly."""
    duration = _shot_total_duration(shots)
    validate_shots(shots, duration)
    if not float(duration).is_integer() or any(
        not float(shot.duration).is_integer() for shot in shots
    ):
        raise PromptError(
            f"当前可灵 API duration 合同仅能精确表达整数秒；请求总时长 {_format_seconds(duration)} 秒，"
            "分镜未提交。请使用支持精确小数时长的接口合同后再启用付费提交"
        )
    return int(duration)


def render_prompt_markdown(
    shots: Sequence[ShotPlan],
    duration: int | float | None = None,
    *,
    sku: str | None = None,
    analysis: str | Mapping[str, object] | None = None,
    source_dimensions: tuple[int, int] | None = None,
    reference_duration_seconds: float | None = None,
    modification_feedback: str | None = None,
    audio_policy: str = "preserve_original",
    scene_image_manifest: Mapping[str, object] | None = None,
    reference_mapping: Mapping[str, object] | None = None,
) -> str:
    requested_duration = _shot_total_duration(shots) if duration is None else duration
    validate_shots(shots, requested_duration)
    _reject_failed_video_analysis(analysis)
    if audio_policy not in {"preserve_original", "generate_music"}:
        raise PromptError("音频策略无效")
    if _is_continuous_reference(analysis) and len(shots) != 1:
        raise PromptError("参考视频是单一连续镜头，不得拆分为多段分镜")
    if modification_feedback is not None and (
        not isinstance(modification_feedback, str) or not modification_feedback.strip()
    ):
        raise PromptError("修改反馈必须是非空文本")
    if source_dimensions is not None:
        if (
            not isinstance(source_dimensions, tuple)
            or len(source_dimensions) != 2
            or any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in source_dimensions)
        ):
            raise PromptError("主参考图尺寸记录无效")
        source_width, source_height = source_dimensions
        source_ratio = source_width / source_height
        if not 1 / 2.5 <= source_ratio <= 2.5:
            raise PromptError("可灵模型不支持主参考图原始比例，已阻止提交")
        aspect_line = (
            f"- 画幅：{source_width}×{source_height}（参考图比例 {source_ratio:.4f}:1 仅作构图参考）；"
            f"{PORTRAIT_OUTPUT_RULE}"
        )
    else:
        aspect_line = f"- 画幅：{PORTRAIT_OUTPUT_RULE}"
    scene_lines: list[str] = []
    if scene_image_manifest is not None:
        scene_images = scene_image_manifest.get("images")
        if not isinstance(scene_images, Sequence) or isinstance(scene_images, (str, bytes)) or not scene_images:
            raise PromptError("场景参考图清单为空或结构无效")
        parsed_scene_rows: list[tuple[str, int, int, int, int, str]] = []
        scene_ratios: set[tuple[int, int]] = set()
        for row in scene_images:
            if not isinstance(row, Mapping):
                raise PromptError("场景参考图清单行结构无效")
            name, width, height = row.get("filename"), row.get("width"), row.get("height")
            ratio = row.get("ratio")
            if (
                not isinstance(name, str) or not name or Path(name).name != name
                or not isinstance(width, int) or isinstance(width, bool) or width <= 0
                or not isinstance(height, int) or isinstance(height, bool) or height <= 0
                or not isinstance(ratio, Mapping)
            ):
                raise PromptError("场景参考图尺寸或比例字段无效")
            numerator, denominator = ratio.get("numerator"), ratio.get("denominator")
            divisor = math.gcd(width, height)
            if (
                not isinstance(numerator, int) or isinstance(numerator, bool)
                or not isinstance(denominator, int) or isinstance(denominator, bool)
                or numerator <= 0 or denominator <= 0
                or (numerator, denominator) != (width // divisor, height // divisor)
            ):
                raise PromptError("场景参考图精确比例字段无效")
            direction = row.get("direction")
            if direction != "portrait" or height <= width:
                raise PromptError("场景参考图为横版或方形，与仅竖版输出要求冲突")
            scene_ratios.add((numerator, denominator))
            parsed_scene_rows.append((name, width, height, numerator, denominator, direction))
        if len(scene_ratios) != 1:
            raise PromptError("场景参考图比例冲突，必须在分析阶段暂停")
        scene_numerator, scene_denominator = next(iter(scene_ratios))
        scene_lines = [
            "## 场景参考图证据",
            "",
            f"场景参考图共 {len(parsed_scene_rows)} 张，精确比例均为 {scene_numerator}:{scene_denominator}。",
        ]
        scene_lines.extend(
            f"- {name}：{width}×{height}，精确比例 {numerator}:{denominator}，{direction}。"
            for name, width, height, numerator, denominator, direction in parsed_scene_rows
        )
        scene_lines.extend(
            [
                "- 这些场景图决定背景与构图参考范围；成片仅竖版，高度大于宽度；不强求与场景图精确同一比例。",
                "- 场景参考图只约束背景和构图，同时覆盖人物服装身份：背景、环境、陈设、光线方向一律以场景参考图为准，严禁采用动作参考视频里的场景、室内外关系或陈设物；画面中人物的穿着就是该款服装的穿着状态依据，款式、版型、长度、颜色、面料纹理与配饰以此为准，不得替换成其他服装；模特全身穿着（上衣、下装、鞋、袜、帽与配饰）必须与场景参考图完全一致，禁止把不同单品的特征混合成杂交款式；仅当场景参考图中人物穿着与本款服装描述明确冲突时（如借拍场景中人物穿着其他款服装），才以本款服装描述为准；服装结构细节以服装正反面与面料图为补充；动作时间轴与机位运动仍依据动作参考视频。",
                "",
            ]
        )
    reference_lines: list[str] = []
    if reference_mapping is not None:
        if not isinstance(reference_mapping, Mapping):
            raise PromptError("引用映射必须是结构化角色映射")
        for role, names in reference_mapping.items():
            if (
                not isinstance(role, str)
                or not role.strip()
                or not isinstance(names, Sequence)
                or isinstance(names, (str, bytes))
                or not names
                or any(
                    not isinstance(name, str)
                    or not name.strip()
                    or Path(name).name != name
                    or name in {".", ".."}
                    for name in names
                )
            ):
                raise PromptError("引用映射包含无效角色或文件名")
            label = (
                f"<<<element_1>>> ← {role}"
                if role == "主参考图"
                else role
            )
            reference_lines.append(f"- {label}：{'、'.join(names)}")
    lines = [
        "# 可灵图生视频提示词（等待确认）",
        "",
        f"- 款号：{sku or '未指定'}",
        "- 模型：kling-v3",
        "- 模式：pro",
        "- 速度策略：standard（本地固定策略；接口不提供速度字段时不向接口发送该策略字段）",
        aspect_line,
        (
            "- 音频策略：参考视频有音轨时必须保留原音；下载后无音轨即失败。"
            if audio_policy == "preserve_original"
            else f"- 音频策略：{MUSIC_ONLY_AUDIO_RULE}下载后无音轨或时长不一致即失败。"
        ),
        "- 原生音频：关闭" if audio_policy == "preserve_original" else "- 原生音频：开启（仅无参考音轨时使用）",
        "- 元素优先最高优先级：服装以元素为准，忽略参考视频里那个人穿的衣服；参考视频只提供动作、步态、节奏与镜头。",
        f"- 物品连续性最高优先级：{OBJECT_CONTINUITY_SUFFIX}",
        "- 物品动作审核：未写基准确认标记时按不存在处理；只有 `<<<object_baseline:物品类别|主参考图与输出首帧均确认存在>>>` 明确确认的同一物品才可参与动作。",
        f"- 总时长：{_format_seconds(requested_duration)} 秒",
        "",
    ]
    if scene_lines:
        lines.extend(scene_lines)
    if reference_lines:
        lines.extend(["## 参考素材映射", "", *reference_lines, ""])
    lines.extend(["## 参考动作与镜头分析", ""])
    if analysis is None:
        lines.append("未提供参考视频分析内容；请根据每 0.25 秒联系表证据填写完整动作时间轴。")
    elif isinstance(analysis, str):
        lines.append(analysis.strip() or "未提供可用的参考视频分析内容。")
    elif isinstance(analysis, Mapping):
        if any(not isinstance(key, str) for key in analysis):
            raise PromptError("参考视频分析字段名必须是文本")
        for key in sorted(analysis, key=lambda item: str(item)):
            rendered = _render_analysis_value(analysis[key])
            lines.append(f"- {key}：{rendered or '未知'}")
    else:
        raise PromptError("参考视频分析内容必须是文本或字段映射")

    lines.extend(["", "## 精确动作复刻与原速", ""])
    if reference_duration_seconds is not None:
        if (
            not isinstance(reference_duration_seconds, (int, float))
            or isinstance(reference_duration_seconds, bool)
            or not math.isfinite(float(reference_duration_seconds))
            or reference_duration_seconds <= 0
        ):
            raise PromptError("参考视频时长记录无效")
        reference_duration = float(reference_duration_seconds)
        if _seconds_decimal(reference_duration) != _seconds_decimal(requested_duration):
            raise PromptError(
                f"参考动作时长 {_format_seconds(reference_duration)} 秒与分镜总时长 {_format_seconds(requested_duration)} 秒必须完全相等；"
                "参考动作时长是唯一时长来源，不得压缩、加速或减速，不得截断或补帧"
            )
        lines.append(
            f"- 参考时长：{_format_seconds(reference_duration)} 秒；非跳跃动作按原速呈现，"
            "不得加速、减速、截断或补帧；跳跃动作按下条专用规则处理。"
        )
    else:
        lines.append(
            "- 依据每 0.25 秒证据逐项复刻完整动作时间轴并保持原速；无法确认的动作标为未知，"
            "全部非跳跃动作按原顺序保留，不得删减；跳跃动作按下条专用规则处理。"
        )
    lines.append(f"- 跳跃动作规则：{JUMP_ACTION_POLICY}")
    if _is_continuous_reference(analysis):
        lines.append("- 镜头连续性：参考为单一连续镜头，使用一个连续镜头呈现，不得增加真实切镜。")
    else:
        lines.append("- 镜头连续性：只在联系表证明确有切镜时保留切镜；不得为了分镜结构新增切镜。")
    if modification_feedback is not None:
        lines.extend(
            [
                "",
                "## 修改反馈",
                "",
                modification_feedback.strip(),
                "只执行上述明确修改，其余服装结构、颜色、面料和未要求改变的动作保持不变。",
            ]
        )

    lines.extend(
        [
            "",
            "## 分镜脚本",
            "",
        ]
    )
    for shot in shots:
        lines.extend(
            [
                f"### 分镜 {shot.index}（{_format_seconds(shot.duration)} 秒）",
                "",
                shot.prompt,
                "",
            ]
        )
    lines.extend(
        [
            "## 全局画面约束",
            "",
            f"- 画面方向硬限制：{PORTRAIT_OUTPUT_RULE}不补边、不拉伸、不裁切，模特与服装主体完整可见。",
            f"- 转身连续性硬约束：{TURN_CONTINUITY_RULE}。",
            "- 参考视频只提供动作、步态、节奏与镜头；其画面人物的服装、发型细节不得作为任何外观依据。",
            "- 按已确认的分镜展示正面、侧面和背面；镜头平稳，避免遮挡主要卖点。",
            "- 避免肢体畸形、衣服穿模、突然换装、背景跳变及镜头裁切服装。",
            "- 生成结果可能无法保证商标、小字、印花和每个服装细节与实拍完全一致。",
            "",
            "## 提交参数",
            "",
            "- 使用款号服装元素作为角色与服装参考。",
            (
                "- 可灵音频参数：关闭原生音频并保留参考视频原音；"
                if audio_policy == "preserve_original"
                else f"- 可灵音频参数：开启音频生成；{MUSIC_ONLY_AUDIO_RULE}"
            ),
            "- 创建任务前必须由用户完整检查并确认本文件。",
            "",
        ]
    )
    return "\n".join(lines)


def _reject_failed_video_analysis(analysis: object) -> None:
    if not isinstance(analysis, Mapping):
        return
    if "videos" in analysis:
        _validate_canonical_video_manifest(analysis)
        return
    failures = analysis.get("failures")
    status = analysis.get("status")
    if not failures and status not in {"failed", "partial"}:
        return
    details: list[str] = []
    if isinstance(failures, Sequence) and not isinstance(failures, (str, bytes)):
        for failure in failures:
            if isinstance(failure, Mapping):
                path = failure.get("path") or "未知路径"
                error = failure.get("error") or "解码或校验失败"
                details.append(f"{path}：{error}")
    if not details:
        details.append("参考视频分析未完整成功")
    raise PromptError("参考视频分析存在失败，禁止生成提示词：" + "；".join(details))


def _validate_canonical_video_manifest(analysis: Mapping[str, object]) -> None:
    videos = analysis.get("videos")
    if not isinstance(videos, Sequence) or isinstance(videos, (str, bytes)) or not videos:
        raise PromptError("参考视频分析清单结构无效：videos 必须是非空列表")

    path_context = "参考视频"
    for index, video in enumerate(videos, start=1):
        if not isinstance(video, Mapping):
            raise PromptError(f"参考视频分析清单结构无效：第 {index} 项不是字段映射")
        path = video.get("path")
        path_text = path if isinstance(path, str) and path.strip() else f"第 {index} 个参考视频"
        path_context = path_text
        if not isinstance(path, str) or not path.strip():
            raise PromptError(f"参考视频分析清单缺少有效路径：{path_text}")
        if "status" not in video or "error" not in video:
            raise PromptError(f"参考视频分析清单字段不完整：{path_text}：缺少 status/error")
        status = video["status"]
        error = video["error"]
        if status == "failed":
            detail = error if isinstance(error, str) and error.strip() else "未提供失败原因"
            raise PromptError(f"参考视频分析存在失败，禁止生成提示词：{path}：{detail}")
        if status != "success":
            detail = f"状态无效：{status!r}"
            if isinstance(error, str) and error.strip():
                detail += f"；{error}"
            raise PromptError(f"参考视频分析未完整成功，禁止生成提示词：{path}：{detail}")
        if error not in (None, ""):
            raise PromptError(f"参考视频分析清单状态与错误字段不一致：{path}：{error}")
        if not _VIDEO_ROW_KEYS <= set(video) or not set(video) <= _VIDEO_ROW_KEYS | {"role"}:
            raise PromptError(f"参考视频分析清单视频行字段不符合规范：{path}")
        if not _valid_video_row(video):
            raise PromptError(f"参考视频分析清单视频行数据无效：{path}")

    schema_version = analysis.get("schema_version")
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        raise PromptError(f"参考视频分析清单版本无效：{path_context}")
    if set(analysis) != _VIDEO_MANIFEST_KEYS:
        raise PromptError(f"参考视频分析清单顶层字段不符合规范：{path_context}")
    if "status" not in analysis or "failures" not in analysis:
        raise PromptError(f"参考视频分析清单缺少聚合状态或失败列表：{path_context}")
    if analysis.get("status") != "ready":
        raise PromptError(
            f"参考视频分析聚合状态与视频行不一致：{path_context}：{analysis.get('status')!r}"
        )

    semantic_analysis = analysis.get("semantic_analysis")
    if (
        not isinstance(semantic_analysis, Mapping)
        or set(semantic_analysis) != _SEMANTIC_ANALYSIS_KEYS
        or semantic_analysis.get("fields") != list(SEMANTIC_FIELDS)
        or not isinstance(semantic_analysis.get("instruction"), str)
        or not semantic_analysis.get("instruction", "").strip()
    ):
        raise PromptError(f"参考视频分析语义契约不符合规范：{path_context}")
    template = semantic_analysis.get("per_video_template")
    if (
        not isinstance(template, Mapping)
        or set(template) != set(SEMANTIC_FIELDS)
        or any(not isinstance(value, str) for value in template.values())
    ):
        raise PromptError(f"参考视频分析语义模板不符合规范：{path_context}")

    failures = analysis.get("failures")
    if not isinstance(failures, Sequence) or isinstance(failures, (str, bytes)):
        raise PromptError(f"参考视频分析失败列表结构无效：{path_context}")
    if failures:
        details = []
        for failure in failures:
            if not isinstance(failure, Mapping):
                raise PromptError(f"参考视频分析失败列表结构无效：{path_context}")
            failed_path = failure.get("path")
            failed_error = failure.get("error")
            if not isinstance(failed_path, str) or not failed_path.strip():
                failed_path = path_context
            if not isinstance(failed_error, str) or not failed_error.strip():
                failed_error = "聚合失败列表与视频行不一致"
            details.append(f"{failed_path}：{failed_error}")
        raise PromptError("参考视频分析聚合失败列表与视频行不一致：" + "；".join(details))


def _valid_video_row(video: Mapping[str, object]) -> bool:
    digest = video.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64:
        return False
    if any(character not in "0123456789abcdef" for character in digest):
        return False
    if not isinstance(video.get("backend_name"), str) or not video["backend_name"].strip():
        return False
    for key in ("frame_count", "width", "height"):
        value = video.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return False
    for key in ("fps", "duration_seconds"):
        value = video.get(key)
        if (
            not _is_finite_number(value)
            or value <= 0
        ):
            return False
    for key, require_values in (("sample_timestamps", True), ("scene_boundaries", False)):
        values = video.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            return False
        if require_values and not values:
            return False
        if any(
            not _is_finite_number(value)
            or value < 0
            for value in values
        ):
            return False
        if list(values) != sorted(values):
            return False
    if not isinstance(video.get("contact_sheet"), str) or not video["contact_sheet"].strip():
        return False
    if not isinstance(video.get("absolute_path"), str) or not Path(video["absolute_path"]).is_absolute():
        return False
    for key in ("file_size_bytes", "mtime_ns"):
        value = video.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    if (
        not _is_finite_number(video.get("aspect_ratio"))
        or video["aspect_ratio"] <= 0
        or not math.isclose(video["aspect_ratio"], video["width"] / video["height"], rel_tol=0, abs_tol=1e-9)
        or video.get("decode_status") != "complete"
    ):
        return False
    audio_status = video.get("audio_status")
    if audio_status not in {"present", "absent", "unknown"}:
        return False
    audio_duration = video.get("audio_duration_seconds")
    if audio_duration is not None and (
        not _is_finite_number(audio_duration) or audio_duration <= 0
    ):
        return False
    if any(not isinstance(video.get(key), list) for key in ("key_frames", "action_timeline", "shot_table")):
        return False
    if any(
        not isinstance(video.get(key), str) or not video[key].strip()
        for key in ("camera_angle", "person_orientation", "transitions", "final_conclusion")
    ):
        return False
    if "role" in video and video["role"] not in {"action_reference", "scene_reference"}:
        return False
    review = video.get("semantic_review")
    return (
        isinstance(review, Mapping)
        and set(review) == set(SEMANTIC_FIELDS)
        and all(isinstance(value, str) for value in review.values())
    )


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _render_analysis_value(value: object) -> str:
    if value is None:
        return "未知"
    if isinstance(value, str):
        return value.strip() or "未知"
    if isinstance(value, (bool, int, float, list, dict)):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise PromptError("参考视频分析内容必须可确定地序列化") from exc
    raise PromptError("参考视频分析字段必须是文本、数字、列表、映射或空值")


def prompt_sha256(markdown: str) -> str:
    if not isinstance(markdown, str):
        raise PromptError("提示词内容必须是文本")
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()


def build_video_payload(
    prepared: PreparedAssets,
    shots: Sequence[ShotPlan],
    element_id: int,
    external_task_id: str,
    *,
    modification_feedback: str | None = None,
    full_prompt_markdown: str | None = None,
    confirmed_prompt_sha256: str | None = None,
    audio_policy: str = "preserve_original",
) -> dict[str, object]:
    duration = validate_api_duration_contract(shots)
    if audio_policy not in {"preserve_original", "generate_music"}:
        raise PromptError("音频策略无效")
    if modification_feedback is not None and (
        not isinstance(modification_feedback, str) or not modification_feedback.strip()
    ):
        raise PromptError("修改反馈必须是非空文本")
    if not isinstance(full_prompt_markdown, str) or not full_prompt_markdown.strip():
        raise PromptError("必须提供完整已审核最终提示词")
    if (
        not isinstance(confirmed_prompt_sha256, str)
        or not re.fullmatch(r"[0-9a-f]{64}", confirmed_prompt_sha256)
        or prompt_sha256(full_prompt_markdown) != confirmed_prompt_sha256
    ):
        raise PromptError("完整提示词与已确认 SHA-256 不一致")
    if not isinstance(element_id, int) or isinstance(element_id, bool) or element_id < 1:
        raise PromptError("element_id 必须是正整数")
    if (
        not isinstance(external_task_id, str)
        or not external_task_id.strip()
        or external_task_id != external_task_id.strip()
    ):
        raise PromptError("external_task_id 必须是非空文本")
    try:
        first_frame = prepared.first_frame.read_bytes()
    except OSError as exc:
        raise PromptError(f"无法读取主参考图比例首帧文件：{prepared.first_frame}") from exc
    if not first_frame:
        raise PromptError(f"主参考图比例首帧文件为空：{prepared.first_frame}")
    try:
        with Image.open(io.BytesIO(first_frame)) as image:
            if image.format not in {"PNG", "JPEG"}:
                raise PromptError(
                    f"首帧格式仅支持 PNG 或 JPEG：{prepared.first_frame}"
                )
            frame_size = image.size
            image.load()
    except PromptError:
        raise
    except Exception as exc:
        raise PromptError(f"主参考图比例首帧文件无法读取或已损坏：{prepared.first_frame}") from exc
    ratio = frame_size[0] / frame_size[1]
    if frame_size[0] < 1 or frame_size[1] < 1 or not 1 / 2.5 <= ratio <= 2.5:
        raise PromptError("首帧比例超出可灵支持范围，已在付费提交前阻止")
    if frame_size[0] >= frame_size[1]:
        raise PromptError(
            "首帧必须为竖版（高度大于宽度）；最终提交参数固定为 9:16，禁止提交横版视频"
        )
    payload: dict[str, object] = {
        "model_name": "kling-v3",
        "image": base64.b64encode(first_frame).decode("ascii"),
        "negative_prompt": "",
        "element_list": [{"element_id": element_id}],
        "duration": str(int(duration)),
        "aspect_ratio": "9:16",
        "mode": "pro",
        "local_generation_policy": {"speed": "standard"},
        # The legacy image2video endpoint can request native audio, while
        # original-track retention is supplied by the connected MCP Omni
        # bridge.  Keep the API request fail-closed for preserve_original.
        "sound": "on" if audio_policy == "generate_music" else "off",
        "external_task_id": external_task_id,
        "watermark_info": {"enabled": False},
    }
    payload.update({"multi_shot": False, "prompt": full_prompt_markdown})
    return payload


def _is_continuous_reference(analysis: object) -> bool:
    continuity_values: list[str] = []
    if isinstance(analysis, Mapping):
        direct = analysis.get("镜头连续性与真实切镜")
        if isinstance(direct, str):
            continuity_values.append(direct)
        manifests = [analysis]
        video_manifest = analysis.get("video_manifest")
        if isinstance(video_manifest, Mapping):
            manifests.append(video_manifest)
        for manifest in manifests:
            videos = manifest.get("videos")
            if isinstance(videos, Sequence) and not isinstance(videos, (str, bytes)):
                for video in videos:
                    if isinstance(video, Mapping):
                        review = video.get("semantic_review")
                        if isinstance(review, Mapping):
                            value = review.get("镜头连续性与真实切镜")
                            if isinstance(value, str):
                                continuity_values.append(value)
    def clause_prefix(value: str, start: int) -> str:
        prefix = value[:start]
        clauses = re.split(r"[，。；,;!?！？]|但是|不过|然而|可是", prefix)
        return clauses[-1]

    def is_negated(value: str, start: int) -> bool:
        return bool(_NEGATION_BEFORE_CUE.search(clause_prefix(value, start)))

    for value in continuity_values:
        compact = re.sub(r"\s+", "", value)
        # Any un-negated edit/cut event overrides continuity wording in the
        # same semantic review, so a mixed sentence cannot suppress real edits.
        if any(not is_negated(compact, match.start()) for match in _CUT_EDIT_TERMS.finditer(compact)):
            continue
        if any(
            not is_negated(compact, match.start())
            for match in _CONTINUOUS_SHOT_CUES.finditer(compact)
        ):
            return True
    return False
