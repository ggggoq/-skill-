from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from .models import ProjectPaths


ALLOWED_SOURCE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
ALLOWED_VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
API_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})
MIN_SIDE = 300
MAX_BYTES = 10 * 1024 * 1024
MIN_RATIO = 1 / 2.5
MAX_RATIO = 2.5
MAX_FIRST_FRAME_SIDE = 4096
_JPEG_QUALITY_STEPS = (95, 90, 85, 80, 75, 70, 65, 60, 55, 50, 45, 40)
_FORMAT_BY_SUFFIX = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}
_TRANSFORM_PARAMETERS = {
    "api_images": {
        "webp": "png",
        "oversize": "quality-stepped-jpeg",
        "minimum_long_side": 1080,
        "jpeg_quality_steps": list(_JPEG_QUALITY_STEPS),
    },
    "first_frame": {
        "size": "source-proportional-with-4096px-maximum-side",
        "fit": "contain-without-padding",
        "resampling": "lanczos",
    },
}
_PROJECT_ROLES = (
    "主参考图",
    "服装细节参考",
    "场景参考",
    "动作参考图",
    "动作参考视频",
)
_PROJECT_IMAGE_ROLES = frozenset(_PROJECT_ROLES[:2])
_PROJECT_SCENE_ROLE = "场景参考"
_PROJECT_ACTION_IMAGE_ROLE = "动作参考图"
_PROJECT_MEDIA_SUFFIXES = ALLOWED_SOURCE_SUFFIXES | frozenset({".mp4", ".mov"})
_PROJECT_MAPPING_PATTERN = re.compile(
    r"^\s*(主参考图|服装细节参考|场景参考|动作参考图|动作参考视频)\s*[:：]\s*(.*?)\s*$"
)


class AssetError(ValueError):
    """A user-actionable problem with a SKU image or its prepared copy."""


def classify_current_sku_files(folder: str | Path) -> dict[str, list[object]]:
    """Classify only the immediate files in one SKU folder; ambiguous media needs review."""
    directory = Path(folder)
    if not directory.is_dir():
        raise AssetError("当前款号素材目录不存在")
    result: dict[str, list[object]] = {
        "garment_front": [],
        "garment_back": [],
        "action_reference": [],
        "scene_reference": [],
        "fabric_reference": [],
        "unrelated": [],
        "needs_review": [],
    }
    image_suffixes = {".jpg", ".jpeg", ".png", ".webp"}
    video_suffixes = {".mp4", ".mov"}
    for path in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file() or path.is_symlink():
            continue
        name = path.stem.casefold()
        suffix = path.suffix.casefold()
        is_image = suffix in image_suffixes
        is_video = suffix in video_suffixes
        if not is_image and not is_video:
            detected = detect_media_kind(path)
            is_image = detected == "image"
            is_video = detected == "video"
        if is_image and any(token in name for token in ("正面", "前片", "front")):
            result["garment_front"].append(path)
        elif is_image and any(token in name for token in ("背面", "后背", "背影", "back")):
            result["garment_back"].append(path)
        elif any(token in name for token in ("场景", "环境", "scene")) and (is_image or is_video):
            result["scene_reference"].append(
                {"path": path, "media_type": "image" if is_image else "video"}
            )
        elif any(token in name for token in ("动作", "action", "pose")) and (is_image or is_video):
            result["action_reference"].append(path)
        elif any(token in name for token in ("面料", "材质", "纹理", "fabric", "texture")) and is_image:
            result["fabric_reference"].append(path)
        elif suffix in {".txt", ".md"}:
            result["unrelated"].append(path)
        elif is_image or is_video:
            result["needs_review"].append(path)
        else:
            result["unrelated"].append(path)
    return result


@dataclass(frozen=True)
class ImageAsset:
    path: Path
    role: str
    sha256: str
    width: int
    height: int
    _snapshot: bytes | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class PreparedImage:
    source: Path
    api_path: Path
    role: str
    source_sha256: str
    derived_sha256: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class PreparedAssets:
    sku: str
    frontal: PreparedImage
    references: tuple[PreparedImage, ...]
    first_frame: Path


@dataclass(frozen=True)
class SkuMaterials:
    sku: str
    folder: Path
    images: tuple[ImageAsset, ...]
    action_videos: tuple[Path, ...]
    request_text: str
    review_standard: str
    action_reference_images: tuple[ImageAsset, ...] = ()
    scene_videos: tuple[Path, ...] = ()


def scan_project_materials(
    paths: ProjectPaths,
    sku: str,
    *,
    reference_files: Mapping[str, list[str]] | None = None,
) -> SkuMaterials:
    """Validate one Gemini-style SKU folder and its explicit media mapping."""
    _validate_sku(sku)
    folder = paths.materials / sku
    if not folder.is_dir():
        raise AssetError(f"款号 {sku} 的素材目录不存在：{folder}")
    request_path = folder / "需求.txt"
    review_path = folder / "审核标准.txt"
    request_text = _read_required_text(request_path, "需求.txt")
    review_standard = _read_required_text(review_path, "审核标准.txt")

    mapping: dict[str, list[str]] = {}
    for line in request_text.splitlines():
        match = _PROJECT_MAPPING_PATTERN.match(line)
        if not match:
            continue
        role, raw = match.groups()
        if role in mapping:
            raise AssetError(f"需求文件中的素材角色重复：{role}")
        value = raw.strip()
        if value.startswith("[") and value.endswith("]"):
            value = value[1:-1].strip()
        if value in {"", "無", "无", "没有", "不提供", "可选", "选填"}:
            mapping[role] = []
        else:
            mapping[role] = [
                item.strip().strip("\"'")
                for item in re.split(r"[、,，;；\n]+", value)
                if item.strip()
            ]
    missing = [
        role
        for role in _PROJECT_ROLES
        if role not in mapping and role != _PROJECT_ACTION_IMAGE_ROLE
    ]
    if missing:
        raise AssetError(f"需求文件缺少明确素材映射：{'、'.join(missing)}")
    mapping.setdefault(_PROJECT_ACTION_IMAGE_ROLE, [])
    declared_mapping = mapping
    if reference_files is not None:
        if not isinstance(reference_files, Mapping) or set(reference_files) != set(_PROJECT_ROLES):
            raise AssetError("参考组素材角色映射无效")
        selected: dict[str, list[str]] = {}
        for role in _PROJECT_ROLES:
            names = reference_files[role]
            if not isinstance(names, list) or any(
                not isinstance(name, str) or not name for name in names
            ):
                raise AssetError(f"参考组素材角色映射无效：{role}")
            if any(name not in mapping[role] for name in names):
                raise AssetError(f"参考组素材未包含在正式需求映射中：{role}")
            selected[role] = list(names)
        mapping = selected
    if len(mapping["主参考图"]) != 1:
        raise AssetError("需求文件必须映射且只映射一张主参考图")

    seen: dict[Path, str] = {}
    images: list[ImageAsset] = []
    videos: list[Path] = []
    scene_videos: list[Path] = []
    action_reference_images: list[ImageAsset] = []
    resolved_folder = folder.resolve(strict=True)
    for role in _PROJECT_ROLES:
        for raw_name in mapping[role]:
            relative = Path(raw_name)
            windows_relative = PureWindowsPath(raw_name)
            if (
                not raw_name
                or "\x00" in raw_name
                or relative.is_absolute()
                or windows_relative.is_absolute()
                or windows_relative.drive
                or any(part in {"", ".", ".."} for part in relative.parts)
            ):
                raise AssetError(f"素材路径必须是款号文件夹内的相对路径：{raw_name}")
            path = folder / relative
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(resolved_folder)
                metadata = path.lstat()
            except (OSError, RuntimeError, ValueError) as exc:
                raise AssetError(f"素材不存在或超出款号文件夹：{raw_name}") from exc
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise AssetError(f"素材必须是款号文件夹内的普通文件：{raw_name}")
            suffix = path.suffix.lower()
            # Conventional suffixes remain a cheap compatibility path for
            # older offline fixtures; every non-conventional suffix is
            # classified from its bytes, so the user is not limited to a
            # fixed extension list in real projects.
            if suffix in ALLOWED_SOURCE_SUFFIXES:
                media_kind = "image"
            elif suffix in ALLOWED_VIDEO_SUFFIXES:
                media_kind = "video"
            else:
                media_kind = detect_media_kind(path)
            if media_kind is None:
                raise AssetError(f"素材无法识别为可解码图片或视频：{raw_name}")
            if role in _PROJECT_IMAGE_ROLES and media_kind != "image":
                raise AssetError(f"{role}只能映射图片：{raw_name}")
            if role == _PROJECT_SCENE_ROLE and media_kind not in {"image", "video"}:
                raise AssetError(f"{role}只能映射图片或视频：{raw_name}")
            if role == "动作参考视频" and media_kind != "video":
                raise AssetError(f"动作参考视频必须是可解码视频：{raw_name}")
            if resolved in seen:
                raise AssetError(f"同一素材重复映射：{seen[resolved]} 与 {role}")
            seen[resolved] = role
            if role in _PROJECT_IMAGE_ROLES:
                images.append(_inspect_image(path, role))
            elif role == _PROJECT_SCENE_ROLE and media_kind == "image":
                images.append(_inspect_image(path, role))
            elif role == _PROJECT_SCENE_ROLE:
                scene_videos.append(path)
            elif role == _PROJECT_ACTION_IMAGE_ROLE:
                action_reference_images.append(_inspect_image(path, role))
            else:
                videos.append(path)

    declared_media_names = {
        name for names in declared_mapping.values() for name in names
    }
    # Numbered reference groups live in direct child directories of the SKU
    # folder.  Their files are intentionally out of scope here; the selected
    # group manifest binds only the normalized files for the current task.
    declared_media = {
        (folder / name).resolve(strict=False)
        for name in declared_media_names
    }
    all_media: set[Path] = set()
    normalized_project_name = re.compile(
        re.escape(sku) + r"_(?:主参考图|服装细节(?:_[^_]+)?|场景参考|动作参考)(?:_\d+)?$"
    )
    for path in folder.iterdir():
        if not path.is_file() or path.name in {"需求.txt", "审核标准.txt"}:
            continue
        resolved = path.resolve()
        if (
            reference_files is not None
            and resolved not in declared_media
            and normalized_project_name.fullmatch(path.stem)
        ):
            # Other imported numbered groups share one SKU directory.  Their
            # normalized copies remain on disk but are outside this group's
            # selected manifest and must not be treated as current inputs.
            continue
        # Only media files participate in the unmapped-material gate.  A SKU
        # folder may also contain ordinary workflow text (for example
        # 修改反馈.txt); treating every file as a media input would make
        # versioned modification flows fail before they can read that text.
        # Unknown extensions are still accepted when their bytes decode as an
        # image or video, so this does not reintroduce an extension whitelist.
        kind = None if path.suffix.lower() in _PROJECT_MEDIA_SUFFIXES else detect_media_kind(path)
        if kind is not None or path.suffix.lower() in _PROJECT_MEDIA_SUFFIXES:
            all_media.add(resolved)
    unmapped = sorted(all_media - declared_media, key=lambda item: str(item).casefold())
    if unmapped:
        names = "、".join(path.relative_to(resolved_folder).as_posix() for path in unmapped)
        raise AssetError(f"发现未映射素材，不能按文件名或顺序猜测：{names}")

    return SkuMaterials(
        sku=sku,
        folder=folder,
        images=tuple(images),
        action_videos=tuple(videos),
        request_text=request_text,
        review_standard=review_standard,
        action_reference_images=tuple(action_reference_images),
        scene_videos=tuple(scene_videos),
    )


def _read_required_text(path: Path, label: str) -> str:
    try:
        if not path.is_file():
            raise AssetError(f"款号文件夹内缺少 {label}")
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise AssetError(f"无法读取款号文件夹内的 {label}") from exc
    if not text:
        raise AssetError(f"款号文件夹内的 {label} 不能为空")
    return text


def sha256_file(path: Path) -> str:
    """Return a streaming SHA-256 digest for a file."""
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AssetError(f"无法读取图片文件：{path}") from exc
    return digest.hexdigest()


def scan_sku_images(paths: ProjectPaths, sku: str) -> tuple[ImageAsset, ...]:
    _validate_sku(sku)
    sku_dir = paths.root / "02款号参考图" / sku
    if not sku_dir.is_dir():
        raise AssetError(f"款号 {sku} 的参考图目录不存在：{sku_dir}")

    try:
        files = sorted(
            (path for path in sku_dir.iterdir() if path.is_file()),
            key=lambda path: (path.name.casefold(), path.name),
        )
    except OSError as exc:
        raise AssetError(f"无法读取款号 {sku} 的参考图目录：{sku_dir}") from exc

    detected = {
        path: (
            "image"
            if path.suffix.lower() in ALLOWED_SOURCE_SUFFIXES
            else "video"
            if path.suffix.lower() in ALLOWED_VIDEO_SUFFIXES
            else detect_media_kind(path)
        )
        for path in files
    }
    unsupported = [
        path
        for path, kind in detected.items()
        if kind is None and path.suffix.lower() not in ALLOWED_VIDEO_SUFFIXES
    ]
    if unsupported:
        names = "、".join(path.name for path in unsupported)
        raise AssetError(f"款号 {sku} 含不支持或无法识别的图片或视频文件：{names}")

    files = [path for path in files if detected[path] == "image"]
    if not 2 <= len(files) <= 4:
        raise AssetError(f"款号 {sku} 必须有 2–4 张参考图；最多 4 张，不能静默丢弃图片")

    fronts = [path for path in files if "正面" in path.stem]
    if len(fronts) != 1:
        raise AssetError(f"款号 {sku} 必须恰好一张文件名包含“正面”的图片")

    return tuple(
        _inspect_image(path, "frontal" if path == fronts[0] else "reference")
        for path in files
    )


def prepare_api_images(
    assets: tuple[ImageAsset, ...], run_dir: Path, *,
    max_images: int = 4, allow_action_images: bool = False,
) -> PreparedAssets:
    ordered = tuple(
        sorted(assets, key=lambda asset: (asset.path.name.casefold(), asset.path.name))
    )
    project_roles = any(asset.role in _PROJECT_IMAGE_ROLES for asset in ordered)
    min_images = 1 if project_roles else 2
    if not min_images <= len(ordered) <= max_images:
        expected = f"{min_images}–{max_images}"
        raise AssetError(f"款号必须有 {expected} 张参考图；最多 {max_images} 张")
    allowed_roles = {"frontal", "reference", *_PROJECT_IMAGE_ROLES, _PROJECT_SCENE_ROLE}
    if allow_action_images:
        allowed_roles.add("动作参考图")
    if any(asset.role not in allowed_roles for asset in ordered):
        raise AssetError("参考图角色无效")
    fronts = [asset for asset in ordered if asset.role in {"frontal", "主参考图"}]
    if len(fronts) != 1:
        raise AssetError("参考图必须恰好有一张正面图")
    try:
        parents = {asset.path.parent.resolve() for asset in ordered}
    except (OSError, RuntimeError) as exc:
        raise AssetError(f"无法解析款号源素材目录：{ordered[0].path.parent}") from exc
    if len(parents) != 1:
        raise AssetError("一个款号的参考图必须来自同一文件夹")

    sku = ordered[0].path.parent.name
    frontal_asset = fronts[0]
    reference_assets = tuple(asset for asset in ordered if asset is not frontal_asset)
    all_assets = (frontal_asset, *reference_assets)

    prepared_payloads: list[tuple[ImageAsset, Path, bytes, str]] = []
    for index, asset in enumerate(all_assets, start=1):
        validated_asset = _validated_snapshot(asset)
        current_hash = sha256_file(asset.path)
        if current_hash != asset.sha256:
            raise AssetError(f"源图在扫描后发生变化，请重新扫描：{asset.path}")
        payload, suffix = _make_api_copy(validated_asset)
        if sha256_file(asset.path) != current_hash:
            raise AssetError(f"源图在扫描后发生变化，请重新扫描：{asset.path}")
        if suffix.lower() not in API_SUFFIXES:
            raise AssetError(f"生成了不支持的 API 图片格式：{asset.path}")
        if len(payload) > MAX_BYTES:
            raise AssetError(f"无法将图片处理到 10 MB 以内：{asset.path}")
        api_dir = Path(run_dir) / "api_images"
        api_path = api_dir / f"{index:02d}-{asset.role}-{asset.sha256[:12]}{suffix}"
        prepared_payloads.append((asset, api_path, payload, hashlib.sha256(payload).hexdigest()))

    frontal_payload = prepared_payloads[0][2]
    first_frame_bytes = _make_first_frame(frontal_payload, frontal_asset.path)
    first_frame_path = Path(run_dir) / f"first-frame-{frontal_asset.sha256[:12]}.png"
    outputs = tuple((api_path, payload) for _, api_path, payload, _ in prepared_payloads) + (
        (first_frame_path, first_frame_bytes),
    )
    output_paths = tuple(path for path, _ in outputs)
    _check_output_safety(ordered, Path(run_dir), output_paths)
    _publish_outputs(ordered, Path(run_dir), outputs)

    prepared_images = tuple(
        PreparedImage(
            source=asset.path,
            api_path=api_path,
            role=asset.role,
            source_sha256=asset.sha256,
            derived_sha256=derived_hash,
            width=asset.width,
            height=asset.height,
        )
        for asset, api_path, _, derived_hash in prepared_payloads
    )
    return PreparedAssets(
        sku=sku,
        frontal=prepared_images[0],
        references=prepared_images[1:],
        first_frame=first_frame_path,
    )


def element_fingerprint(prepared: PreparedAssets) -> str:
    rows = [
        {
            "role": item.role,
            "source": item.source_sha256,
            "derived": item.derived_sha256,
            "width": item.width,
            "height": item.height,
        }
        for item in (prepared.frontal, *prepared.references)
    ]
    canonical = json.dumps(
        {
            "sku": prepared.sku,
            "images": rows,
            "first_frame": "native-aspect-proportional",
            "transforms": _TRANSFORM_PARAMETERS,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_sku(sku: str) -> None:
    invalid = set('<>:"/\\|?*')
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    }
    stem = sku.split(".", maxsplit=1)[0].rstrip(" .").upper() if isinstance(sku, str) else ""
    if (
        not isinstance(sku, str)
        or not sku
        or sku in {".", ".."}
        or sku.endswith((" ", "."))
        or any(character in invalid or ord(character) < 32 for character in sku)
        or stem in reserved
    ):
        raise AssetError("款号必须是有效的 Windows 单级目录名")


def _check_output_safety(
    assets: tuple[ImageAsset, ...], run_dir: Path, output_paths: tuple[Path, ...]
) -> None:
    if ".." in run_dir.parts or any(".." in path.parts for path in output_paths):
        raise AssetError(f"输出路径包含父目录跳转，拒绝写入：{run_dir}")

    _reject_reparse_components(run_dir)
    for output_path in output_paths:
        _reject_reparse_components(output_path)

    try:
        source_dir = assets[0].path.parent.resolve(strict=True)
        output_root = run_dir.resolve(strict=False)
        resolved_outputs = tuple(path.resolve(strict=False) for path in output_paths)
    except (OSError, RuntimeError) as exc:
        raise AssetError(f"无法安全解析输出路径：{run_dir}") from exc

    if _paths_overlap(source_dir, output_root):
        raise AssetError(f"输出路径与款号源素材目录重合或包含源目录，拒绝写入：{run_dir}")

    source_files = tuple(asset.path for asset in assets)
    for source in source_files:
        try:
            source_resolved = source.resolve(strict=True)
            if _same_existing_file(run_dir, source) or _is_within(source_resolved, output_root):
                raise AssetError(f"输出路径与源素材文件重合，拒绝写入：{run_dir}")
            for output_path, resolved_output in zip(output_paths, resolved_outputs, strict=True):
                if (
                    resolved_output == source_resolved
                    or _is_within(resolved_output, source_dir)
                    or _same_existing_file(output_path, source)
                ):
                    raise AssetError(f"衍生输出路径与源素材文件重合，拒绝写入：{output_path}")
        except (OSError, RuntimeError) as exc:
            raise AssetError(f"无法核对源素材与输出路径：{source}") from exc

    try:
        if run_dir.exists() and not run_dir.is_dir():
            raise AssetError(f"输出路径不是文件夹，拒绝写入：{run_dir}")
    except OSError as exc:
        raise AssetError(f"无法检查输出路径：{run_dir}") from exc


def _reject_reparse_components(path: Path) -> None:
    if ".." in path.parts:
        raise AssetError(f"输出路径包含父目录跳转，拒绝写入：{path}")
    absolute = path if path.is_absolute() else Path.cwd() / path
    components = tuple(reversed((absolute, *absolute.parents)))
    for component in components:
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise AssetError(f"无法检查输出路径组件：{component}") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise AssetError(f"输出路径经过符号链接或 Windows 重解析点，拒绝写入：{component}")


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _same_existing_file(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AssetError(f"无法比较文件路径：{first}") from exc


def _inspect_image(path: Path, role: str) -> ImageAsset:
    try:
        snapshot = path.read_bytes()
    except OSError as exc:
        raise AssetError(f"无法读取图片文件：{path}") from exc
    return _inspect_snapshot(path, role, snapshot)


def _inspect_snapshot(path: Path, role: str, snapshot: bytes) -> ImageAsset:
    try:
        with Image.open(io.BytesIO(snapshot)) as image:
            image_format = image.format
            width, height = image.size
            image.verify()
        with Image.open(io.BytesIO(snapshot)) as image:
            orientation = image.getexif().get(274, 1)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetError(f"图片无法读取或已损坏：{path}") from exc

    if not image_format:
        raise AssetError(f"图片格式无法识别：{path}")
    if min(width, height) < MIN_SIDE:
        raise AssetError(f"图片边长不得小于 {MIN_SIDE} 像素：{path}")
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    ratio = width / height
    if not MIN_RATIO <= ratio <= MAX_RATIO:
        raise AssetError(f"图片长宽比例必须在 1:2.5 到 2.5:1 之间：{path}")

    return ImageAsset(
        path=path,
        role=role,
        sha256=hashlib.sha256(snapshot).hexdigest(),
        width=width,
        height=height,
        _snapshot=snapshot,
    )


def _validated_snapshot(asset: ImageAsset) -> ImageAsset:
    if asset._snapshot is None:
        validated = _inspect_image(asset.path, asset.role)
    else:
        validated = _inspect_snapshot(asset.path, asset.role, asset._snapshot)
    if (
        validated.sha256 != asset.sha256
        or validated.width != asset.width
        or validated.height != asset.height
        or validated.role != asset.role
    ):
        raise AssetError(f"扫描图片记录与其已校验内容不一致：{asset.path}")
    return validated


def _make_api_copy(asset: ImageAsset) -> tuple[bytes, str]:
    source_suffix = asset.path.suffix.lower()
    try:
        snapshot = asset._snapshot
        if snapshot is None:
            snapshot = _validated_snapshot(asset)._snapshot
        if snapshot is None:
            raise AssetError(f"无法取得已校验的图片快照：{asset.path}")
        with Image.open(io.BytesIO(snapshot)) as opened:
            image = ImageOps.exif_transpose(opened).copy()
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetError(f"图片无法读取或已损坏：{asset.path}") from exc

    if len(snapshot) > MAX_BYTES:
        return _compress_as_jpeg(image, asset.path), ".jpg"

    if source_suffix not in {".jpg", ".jpeg", ".png"}:
        try:
            buffer = io.BytesIO()
            output_format = (
                "PNG"
                if source_suffix == ".webp"
                or "A" in image.getbands()
                or image.mode in {"LA", "P"}
                else "JPEG"
            )
            if output_format == "JPEG":
                image = _to_rgb(image)
            image.save(buffer, format=output_format, optimize=True)
            payload = buffer.getvalue()
        except OSError as exc:
            raise AssetError(f"无法将图片转换为 PNG：{asset.path}") from exc
        if len(payload) > MAX_BYTES:
            return _compress_as_jpeg(image, asset.path), ".jpg"
        return payload, ".png" if output_format == "PNG" else ".jpg"

    return snapshot, source_suffix


def detect_media_kind(path: str | Path) -> str | None:
    """Detect image/video by decodable content, independent of filename suffix."""

    candidate = Path(path)
    try:
        with Image.open(candidate) as image:
            image.verify()
        return "image"
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError):
        pass
    try:
        import cv2

        capture = cv2.VideoCapture(str(candidate))
        try:
            if not capture.isOpened():
                return None
            ok, frame = capture.read()
            return "video" if ok and frame is not None else None
        finally:
            capture.release()
    except Exception:
        return None


def _compress_as_jpeg(image: Image.Image, source_path: Path) -> bytes:
    try:
        rgb = _to_rgb(image)
        width, height = rgb.size
        longest_side = max(width, height)
        scales = [1.0]
        current_long = longest_side
        while current_long > 1080:
            next_long = max(1080, int(current_long * 0.85))
            if next_long == current_long:
                break
            current_long = next_long
            scales.append(current_long / longest_side)

        for scale in scales:
            if scale == 1.0:
                candidate = rgb
            else:
                size = (max(1, round(width * scale)), max(1, round(height * scale)))
                candidate = rgb.resize(size, Image.Resampling.LANCZOS)
            for quality in _JPEG_QUALITY_STEPS:
                buffer = io.BytesIO()
                candidate.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
                payload = buffer.getvalue()
                if len(payload) <= MAX_BYTES:
                    return payload
    except OSError as exc:
        raise AssetError(f"无法压缩图片：{source_path}") from exc

    raise AssetError(
        f"无法将图片压缩到 10 MB 以内，且不能把长边缩到 1080 像素以下：{source_path}"
    )


def _to_rgb(image: Image.Image) -> Image.Image:
    if image.mode in {"RGBA", "LA"} or "transparency" in image.info:
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return image.convert("RGB")


def _make_first_frame(payload: bytes, source_path: Path) -> bytes:
    try:
        with Image.open(io.BytesIO(payload)) as opened:
            image = ImageOps.exif_transpose(opened).copy()
        rgb = _to_rgb(image)
        longest_side = max(rgb.size)
        if longest_side > MAX_FIRST_FRAME_SIDE:
            scale = MAX_FIRST_FRAME_SIDE / longest_side
            rgb = rgb.resize(
                (max(1, round(rgb.width * scale)), max(1, round(rgb.height * scale))),
                Image.Resampling.LANCZOS,
            )
        while True:
            buffer = io.BytesIO()
            rgb.save(buffer, format="PNG", optimize=True)
            payload = buffer.getvalue()
            if len(payload) <= MAX_BYTES:
                return payload
            if max(rgb.size) <= MIN_SIDE:
                raise AssetError(f"无法在保留主参考图比例的前提下将首帧压缩到 10 MB 以内：{source_path}")
            scale = min(0.9, (MAX_BYTES / len(payload)) ** 0.5 * 0.95)
            next_size = (
                max(MIN_SIDE, round(rgb.width * scale)),
                max(MIN_SIDE, round(rgb.height * scale)),
            )
            if next_size == rgb.size:
                raise AssetError(f"无法在保留主参考图比例的前提下压缩首帧：{source_path}")
            rgb = rgb.resize(next_size, Image.Resampling.LANCZOS)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AssetError(f"无法从正面参考图制作首帧：{source_path}") from exc


def _corner_median(image: Image.Image) -> tuple[int, int, int]:
    points = (
        image.getpixel((0, 0)),
        image.getpixel((image.width - 1, 0)),
        image.getpixel((0, image.height - 1)),
        image.getpixel((image.width - 1, image.height - 1)),
    )
    channels = [sorted(point[channel] for point in points) for channel in range(3)]
    return tuple((values[1] + values[2]) // 2 for values in channels)


def _publish_outputs(
    assets: tuple[ImageAsset, ...],
    run_dir: Path,
    outputs: tuple[tuple[Path, bytes], ...],
) -> None:
    output_paths = tuple(path for path, _ in outputs)
    expected_hashes = {path: hashlib.sha256(payload).hexdigest() for path, payload in outputs}
    reusable: set[Path] = set()
    for path, _ in outputs:
        if _existing_output_matches(path, expected_hashes[path], assets):
            reusable.add(path)

    needed = tuple((path, payload) for path, payload in outputs if path not in reusable)
    if not needed:
        return

    created_dirs: list[Path] = []
    staged: list[tuple[Path, Path, str]] = []
    published: list[tuple[Path, tuple[int, int]]] = []
    succeeded = False
    current_path = needed[0][0]
    try:
        for directory in dict.fromkeys(path.parent for path, _ in needed):
            _ensure_directory(directory, created_dirs)
        _check_output_safety(assets, run_dir, output_paths)
        for path, payload in needed:
            current_path = path
            if _existing_output_matches(path, expected_hashes[path], assets):
                reusable.add(path)
                continue
            temp_path, identity = _write_unique_temp(path, payload)
            staged.append((temp_path, path, identity))

        for temp_path, path, identity in staged:
            current_path = path
            _check_output_safety(assets, run_dir, output_paths)
            published.append((path, identity))
            try:
                _publish_exclusive(temp_path, path)
            except FileExistsError:
                if _existing_output_matches(path, expected_hashes[path], assets):
                    _remove_temp(temp_path)
                    continue
                raise AssetError(f"现有输出内容冲突，已保留原文件：{path}")
            _remove_temp(temp_path)
        succeeded = True
    except OSError as exc:
        raise AssetError(f"无法保存衍生图片：{current_path}") from exc
    finally:
        if not succeeded:
            for temp_path, _, _ in staged:
                _remove_temp(temp_path)
            for path, identity in reversed(published):
                _remove_owned_file(path, identity)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass


def _existing_output_matches(
    path: Path, expected_hash: str, assets: tuple[ImageAsset, ...]
) -> bool:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise AssetError(f"无法检查现有衍生输出：{path}") from exc

    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise AssetError(f"衍生输出不能是链接或重解析点：{path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise AssetError(f"现有输出不是普通文件，已保留原内容：{path}")
    if any(_same_existing_file(path, asset.path) for asset in assets):
        raise AssetError(f"衍生输出与源素材文件别名相同，拒绝写入：{path}")
    if sha256_file(path) != expected_hash:
        raise AssetError(f"现有输出内容冲突，已保留原文件：{path}")
    return True


def _ensure_directory(path: Path, created_dirs: list[Path]) -> None:
    missing: list[Path] = []
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise AssetError(f"无法定位输出文件夹：{path}")
            current = current.parent
            continue
        except OSError as exc:
            raise AssetError(f"无法检查输出文件夹：{current}") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise AssetError(f"输出文件夹经过链接或重解析点：{current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise AssetError(f"输出路径组件不是文件夹：{current}")
        break

    for directory in reversed(missing):
        try:
            directory.mkdir()
            created_dirs.append(directory)
        except FileExistsError:
            try:
                metadata = directory.lstat()
            except OSError as exc:
                raise AssetError(f"无法检查输出文件夹：{directory}") from exc
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400
            ):
                raise AssetError(f"输出文件夹路径冲突，拒绝写入：{directory}")
        except OSError as exc:
            raise AssetError(f"无法创建输出文件夹：{directory}") from exc


def _write_unique_temp(target: Path, payload: bytes) -> tuple[Path, tuple[int, int]]:
    descriptor: int | None = None
    temp_path: Path | None = None
    succeeded = False
    try:
        descriptor, raw_path = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp-", dir=target.parent
        )
        temp_path = Path(raw_path)
        stream = None
        try:
            stream = os.fdopen(descriptor, "wb")
        finally:
            if stream is None:
                unowned_descriptor = descriptor
                descriptor = None
                os.close(unowned_descriptor)
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        identity = _file_identity(temp_path)
        succeeded = True
        return temp_path, identity
    except OSError as exc:
        raise AssetError(f"无法创建衍生图片临时文件：{target}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not succeeded and temp_path is not None:
            _remove_temp(temp_path)


def _publish_exclusive(temp_path: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(temp_path, target)
    else:
        os.link(temp_path, target)


def _file_identity(path: Path) -> tuple[int, int]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AssetError(f"无法检查衍生图片临时文件：{path}") from exc
    return metadata.st_dev, metadata.st_ino


def _remove_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_owned_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity:
            path.unlink()
    except OSError:
        pass
