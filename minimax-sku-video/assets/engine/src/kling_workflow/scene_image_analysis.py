"""Safe, content-addressed technical analysis for selected scene reference images."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, UnidentifiedImageError

from .assets import ImageAsset


SCENE_IMAGE_ANALYZER_VERSION = "scene-image-technical-v1"
SCENE_IMAGE_DECODE_CONFIG: dict[str, object] = {
    "decoder": "Pillow",
    "decoder_policy": "full-load-no-exif-rotation",
    "ratio": "gcd-reduced-integer",
}
_SCHEMA_VERSION = 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


class SceneImageAnalysisError(ValueError):
    """Safe, actionable failure while validating selected scene images."""


class _IncompleteSceneCache(ValueError):
    """A correctly scoped cache entry is incomplete and must be reanalyzed."""


def analyze_scene_images(
    images: Sequence[ImageAsset],
    *,
    sku: str,
    reference_group_id: str,
    allowed_root: str | Path,
    cache_root: str | Path,
    required: bool = False,
) -> dict[str, object]:
    """Analyze all selected scene images, reusing only complete scoped cache entries."""

    if not isinstance(images, Sequence) or isinstance(images, (str, bytes)):
        raise SceneImageAnalysisError("场景参考图名单结构无效")
    if required and not images:
        raise SceneImageAnalysisError("当前参考组要求场景参考图，但参考组场景参考图名单为空")
    if any(not isinstance(item, ImageAsset) or item.role != "场景参考" for item in images):
        raise SceneImageAnalysisError("场景参考图名单含有非场景图片")
    ordered = sorted(images, key=lambda item: (item.path.name.casefold(), item.path.name))

    root = Path(allowed_root)
    cache = Path(cache_root)
    pending_cache: list[tuple[Path, bytes]] = []
    rows: list[dict[str, object]] = []
    ratios: set[tuple[int, int]] = set()
    for asset in ordered:
        path = Path(asset.path)
        snapshot, digest, file_size, mtime_ns = _read_stable(path, root, asset.sha256)
        key = _cache_key(sku, reference_group_id, digest)
        cache_path = cache / f"{key}.json"
        publish_cache = True
        if cache_path.exists():
            try:
                decoded = _read_cache(cache_path, key, sku, reference_group_id, digest)
                cache_status = "hit"
            except _IncompleteSceneCache:
                try:
                    decoded = _decode_scene_image(snapshot)
                except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
                    raise SceneImageAnalysisError(f"场景参考图无法完整解码：{path.name}") from None
                cache_status = "miss"
                # Preserve the incomplete historical entry. A later analyzer
                # version/configuration creates a fresh content-addressed key.
                publish_cache = False
        else:
            try:
                decoded = _decode_scene_image(snapshot)
            except (OSError, UnidentifiedImageError, Image.DecompressionBombError, ValueError):
                raise SceneImageAnalysisError(f"场景参考图无法完整解码：{path.name}") from None
            cache_status = "miss"

        width = decoded.get("width")
        height = decoded.get("height")
        numerator = decoded.get("ratio_numerator")
        denominator = decoded.get("ratio_denominator")
        if any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (width, height, numerator, denominator)):
            raise SceneImageAnalysisError(f"场景参考图尺寸或精确比例无效：{path.name}")
        ratio = (numerator, denominator)
        ratios.add(ratio)
        direction = _direction(width, height)
        if direction != "portrait":
            label = "横版" if direction == "landscape" else "方形"
            raise SceneImageAnalysisError(f"场景参考图为{label}，与仅竖版输出要求冲突：{path.name}（{width}×{height}，{numerator}:{denominator}）")
        if cache_status == "miss" and publish_cache:
            entry = {
                "schema_version": _SCHEMA_VERSION,
                "cache_key": key,
                "sku": sku,
                "reference_group_id": reference_group_id,
                "image_sha256": digest,
                "analyzer_version": SCENE_IMAGE_ANALYZER_VERSION,
                "decode_config": SCENE_IMAGE_DECODE_CONFIG,
                "decoded": decoded,
            }
            pending_cache.append((cache_path, _json_bytes(entry)))
        rows.append(
            {
                "absolute_path": str(path.resolve(strict=True)),
                "reference_group_id": reference_group_id,
                "filename": path.name,
                "sha256": digest,
                "file_size_bytes": file_size,
                "mtime_ns": mtime_ns,
                "format": decoded["format"],
                "width": width,
                "height": height,
                "ratio": {"numerator": numerator, "denominator": denominator},
                "direction": direction,
                "decode_status": "complete",
                "error_summary": None,
                "cache_status": cache_status,
                "analyzer_version": SCENE_IMAGE_ANALYZER_VERSION,
                "decode_config": dict(SCENE_IMAGE_DECODE_CONFIG),
            }
        )

    if len(ratios) > 1:
        details = "；".join(
            f"{row['filename']}={row['ratio']['numerator']}:{row['ratio']['denominator']}"
            for row in rows
        )
        raise SceneImageAnalysisError(f"当前参考组内场景参考图比例冲突：{details}")

    for path, payload in pending_cache:
        _publish_cache(path, payload)
    ratio = next(iter(ratios), None)
    result: dict[str, object] = {
        "schema_version": _SCHEMA_VERSION,
        "sku": sku,
        "reference_group_id": reference_group_id,
        "analyzer_version": SCENE_IMAGE_ANALYZER_VERSION,
        "decode_config": dict(SCENE_IMAGE_DECODE_CONFIG),
        "images": rows,
        "summary": {
            "count": len(rows),
            "ratio": (
                {"numerator": ratio[0], "denominator": ratio[1]}
                if ratio is not None else None
            ),
        },
    }
    result["manifest_sha256"] = scene_image_manifest_sha256(result)
    return result


def scene_image_manifest_sha256(manifest: Mapping[str, object]) -> str:
    """Hash the canonical scene-image evidence without its own digest field."""

    value = dict(manifest)
    value.pop("manifest_sha256", None)
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _read_stable(path: Path, allowed_root: Path, expected_digest: str) -> tuple[bytes, str, int, int]:
    try:
        _reject_reparse(path)
        resolved_root = allowed_root.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_root)
        before = path.stat()
        if not stat.S_ISREG(path.lstat().st_mode):
            raise SceneImageAnalysisError(f"场景参考图不是普通文件：{path.name}")
        snapshot = path.read_bytes()
        after = path.stat()
        resolved_after = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise SceneImageAnalysisError(f"场景参考图缺失或路径超出当前参考组：{path.name}") from None
    digest = hashlib.sha256(snapshot).hexdigest()
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after or resolved_after != resolved_path or after.st_size != len(snapshot):
        raise SceneImageAnalysisError(f"场景参考图读取期间发生变化：{path.name}")
    if not isinstance(expected_digest, str) or not _DIGEST_RE.fullmatch(expected_digest) or digest != expected_digest:
        raise SceneImageAnalysisError(f"场景参考图 SHA-256 与导入记录不一致：{path.name}")
    return snapshot, digest, int(after.st_size), int(after.st_mtime_ns)


def _decode_scene_image(snapshot: bytes) -> dict[str, object]:
    with Image.open(io.BytesIO(snapshot)) as image:
        image.load()
        width, height = image.size
        image_format = image.format
    if not image_format or width <= 0 or height <= 0:
        raise ValueError("invalid decoded image")
    divisor = math.gcd(width, height)
    return {
        "format": image_format,
        "width": int(width),
        "height": int(height),
        "ratio_numerator": int(width // divisor),
        "ratio_denominator": int(height // divisor),
    }


def _cache_key(sku: str, group_id: str, digest: str) -> str:
    value = {
        "sku": sku,
        "reference_group_id": group_id,
        "image_sha256": digest,
        "analyzer_version": SCENE_IMAGE_ANALYZER_VERSION,
        "decode_config": SCENE_IMAGE_DECODE_CONFIG,
    }
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _read_cache(path: Path, key: str, sku: str, group_id: str, digest: str) -> dict[str, object]:
    try:
        _reject_reparse(path)
        entry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, SceneImageAnalysisError):
        raise SceneImageAnalysisError(f"场景参考图缓存损坏或不可安全读取：{path.name}") from None
    if (
        not isinstance(entry, dict)
        or entry.get("schema_version") != _SCHEMA_VERSION
        or entry.get("cache_key") != key
        or entry.get("sku") != sku
        or entry.get("reference_group_id") != group_id
        or entry.get("image_sha256") != digest
        or entry.get("analyzer_version") != SCENE_IMAGE_ANALYZER_VERSION
        or entry.get("decode_config") != SCENE_IMAGE_DECODE_CONFIG
    ):
        raise SceneImageAnalysisError(f"场景参考图缓存字段不完整或版本冲突：{path.name}")
    decoded = entry.get("decoded")
    if not isinstance(decoded, dict):
        raise _IncompleteSceneCache(path.name)
    if set(decoded) != {"format", "width", "height", "ratio_numerator", "ratio_denominator"}:
        raise _IncompleteSceneCache(path.name)
    return dict(decoded)


def _direction(width: int, height: int) -> str:
    if height > width:
        return "portrait"
    if width > height:
        return "landscape"
    return "square"


def _reject_reparse(path: Path) -> None:
    for component in (path, *path.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise SceneImageAnalysisError("无法检查场景参考图或缓存路径") from None
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE:
            raise SceneImageAnalysisError("场景参考图或缓存路径经过链接/重解析点")


def _publish_cache(path: Path, payload: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _reject_reparse(path)
        if path.exists():
            if path.read_bytes() != payload:
                raise SceneImageAnalysisError(f"场景参考图缓存内容冲突，已保留原缓存：{path.name}")
            return
        descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _reject_reparse(path)
            if os.name == "nt":
                os.rename(temp, path)
            else:
                os.link(temp, path)
                temp.unlink()
        except FileExistsError:
            if not path.is_file() or path.read_bytes() != payload:
                raise SceneImageAnalysisError(f"场景参考图缓存发布冲突：{path.name}") from None
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass
    except SceneImageAnalysisError:
        raise
    except OSError as exc:
        raise SceneImageAnalysisError(f"无法安全发布场景参考图缓存（{type(exc).__name__}）") from None


def _json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
