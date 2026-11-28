"""Independent Muse Video Pro bridge; all preparation paths are offline."""
from __future__ import annotations

import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
from typing import Callable, Mapping

from PIL import Image

from .client import DownloadTargetExists, _publish_temp_exclusive
from .muse_client import MODEL, MuseClientError, _safe_https_url


IMAGE_SECONDS = "10"
IMAGE_ASPECT_RATIO = "9:16"
SUPPORTED_SECONDS = (5, 8, 10, 20, 30, 40, 50, 60)
DONE = frozenset({"complete", "completed", "succeeded", "success", "done", "finished"})
FAILED = frozenset({"failed", "failure", "error", "cancelled", "canceled", "rejected"})
_SKU = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_VERSION = re.compile(r"v\d{3,}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_TASK_BINDING_FIELDS = ("schema_version", "sku", "version", "model", "mode", "prompt_sha256",
                        "roles", "asset_fingerprints", "seconds", "aspect_ratio")
_PRINT_DETAILS = re.compile(r"印花|绣花|刺绣|图案|花纹|花色|印字|embroidery|pattern|print", re.IGNORECASE)
_DURATION_TOLERANCE_SECONDS = 0.5


class MuseBridgeError(RuntimeError):
    """Safe local errors that never interpolate media, prompt, URL, or secret data."""


@dataclass(frozen=True)
class PreparedRequest:
    mode: str
    body: dict = field(repr=False, compare=False)
    prompt_sha256: str
    roles: tuple[str, ...]
    asset_fingerprints: tuple[dict[str, object], ...]
    source_paths: tuple[str, ...] = field(repr=False)
    body_sha256: str
    preflight: dict[str, object] = field(default_factory=dict)


def _read_stable(path: str | Path) -> bytes:
    try:
        source = Path(path)
        before = source.lstat()
        if (not stat.S_ISREG(before.st_mode) or source.is_symlink()
                or getattr(before, "st_file_attributes", 0) & 0x400
                or before.st_size <= 0):
            raise MuseBridgeError("素材必须是非空的普通文件")
        with source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read()
            after = os.fstat(handle.fileno())
        latest = source.lstat()
        if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (latest.st_dev, latest.st_ino, latest.st_size, latest.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or len(data) != before.st_size):
            raise MuseBridgeError("素材读取期间发生变化，请重新预检")
        return data
    except MuseBridgeError:
        raise
    except (OSError, ValueError, TypeError):
        raise MuseBridgeError("素材无法安全读取") from None


def _image_fingerprint(path: str | Path, role: str) -> tuple[dict[str, object], bytes]:
    data = _read_stable(path)
    try:
        with Image.open(__import__("io").BytesIO(data)) as image:
            if image.format not in {"JPEG", "PNG", "WEBP"}:
                raise ValueError
            image.verify()
        with Image.open(__import__("io").BytesIO(data)) as image:
            image.load()
    except Exception:
        raise MuseBridgeError("图片素材无效或格式不受支持") from None
    fingerprint = {
        "role": role,
        "sha256": hashlib.sha256(data).hexdigest(),
        "size_bytes": len(data),
    }
    return fingerprint, data


def _compose_prompt(original: bytes, mode: str) -> str:
    try:
        base = original.decode("utf-8")
    except UnicodeDecodeError:
        raise MuseBridgeError("提示词文件必须是 UTF-8") from None
    if not base.strip():
        raise MuseBridgeError("提示词不能为空")
    if _PRINT_DETAILS.search(base):
        raise MuseBridgeError("提示词不得描述具体图案；背面细节只由反面平铺图约束")
    if mode == "image_three_roles":
        rules = (
            "【三图职责与成片要求】\n"
            "1. 模特图（images第1张）是人物身份、脸、发型、前面服装、服装底色、面料质感、光线、场景和已有配饰的唯一基准。\n"
            "2. 反面平铺（images第2张）仅约束服装背面样式、结构及原有绣花/印花；不得迁移其底色、光线、面料质感或背景，提示词不描述具体图案。\n"
            "3. 动作分镜（images第3张）仅约束动作方向、速度、顺序、镜头和构图；不得借用分镜中的人物、服装和背景。\n"
            "不得改变用户指定的动作或速度。背景音乐后续由本地流程替换，生成音乐不作为最终配乐。"
        )
    elif mode == "image_four_roles":
        rules = (
            "【四图职责与成片要求】\n"
            "1. 模特图（images第1张）是人物身份、脸、发型、前面服装轮廓、服装底色、面料质感、光线、场景和已有配饰的唯一基准。\n"
            "2. 背面参考（images第2张）是服装背面图案的唯一权威来源：图案的内容、元素数量、每个元素的姿态与朝向、配色、文字内容与排布位置一律以其为准，逐项照搬；同时约束该图案在背部的垂直位置、水平居中度与相对大小。不得迁移其中的人物、头发、挎包、下装、鞋、背景与光线；不得新增、删减或替换任何元素，不得把图案放大、下移或改为其他形象，也不得渲染成立体贴布、贴纸或带白色描边的独立图层，提示词不描述具体图案。\n"
            "3. 动作分镜（images第3张）仅约束动作方向、速度、顺序、镜头和构图；不得借用分镜中的人物、服装和背景。\n"
            "4. 正面细节（images第4张）只约束前胸局部装饰元素的造型、材质、颜色、尺寸，以及该元素相对门襟与衣身的位置；该元素必须按参考原样逐项复现，不得替换成其他形象、不得改变尺寸比例、不得挪动位置，也不得扩散、复制或衍生到参考未覆盖的区域。\n"
            "四张图的编号与顺序固定，不得互换角色。\n"
            "不得改变用户指定的动作或速度。背景音乐后续由本地流程替换，生成音乐不作为最终配乐。"
        )
    elif mode == "image_back_pair":
        rules = (
            "【四图职责与成片要求（背面双参考）】\n"
            "1. 模特图（images第1张）是人物身份、脸、发型、前面服装轮廓、服装底色、面料质感、光线、场景和已有配饰的唯一基准。\n"
            "2. 背面平铺原图（images第2张）是背面图案的唯一权威来源：图案的内容、元素数量、每个元素的姿态与朝向、配色、文字内容与排布位置，一律以该图为准，逐项照搬。\n"
            "3. 背面实拍（images第3张，真人穿着状态）只约束该图案在背部的穿着位置、水平居中度、相对大小，以及面料在人体上的自然褶皱；其图案内容仍以第2张为准，两张冲突时以第2张为准。\n"
            "4. 动作分镜（images第4张）仅约束动作方向、速度、顺序、镜头和构图；不得借用分镜中的人物、服装和背景。\n"
            "背面图案必须原样复现：不得新增参考图中不存在的任何道具、手持物、植物、饰品或文字，不得改变任何元素的数量、姿态、朝向和颜色，不得改写文字内容与字体，不得改变图案的整体构图，不得镜像或左右翻转，不得调换任何元素的左右位置。\n"
            "背面图案必须是直接印在面料上的平面印花，与面料同面、无厚度；不得渲染成立体贴布、贴纸或补片，不得带白色描边、外发光或独立底色块。\n"
            "四张图的编号与顺序固定，不得互换角色。\n"
            "不得改变用户指定的动作或速度。背景音乐后续由本地流程替换，生成音乐不作为最终配乐。"
        )
    elif mode == "image_back_pair_front":
        rules = (
            "【四图职责与成片要求（背面双参考 + 正面细节）】\n"
            "1. 模特图（images第1张）是人物身份、脸、发型、前面服装轮廓、服装底色、面料质感、光线、场景和已有配饰的唯一基准。\n"
            "2. 背面平铺原图（images第2张）是背面图案的唯一权威来源：图案的内容、元素数量、每个元素的姿态与朝向、配色、文字内容与排布位置，一律以该图为准，逐项照搬。\n"
            "3. 背面实拍（images第3张，真人穿着状态）只约束该图案在背部的穿着位置、水平居中度、相对大小，以及面料在人体上的自然褶皱；其图案内容仍以第2张为准，两张冲突时以第2张为准。\n"
            "4. 正面细节（images第4张）只约束前胸局部装饰元素的造型、材质、颜色、尺寸，以及该元素相对门襟与衣身的位置；该元素必须按参考原样逐项复现，不得替换成其他形象、不得改变尺寸比例、不得挪动位置，也不得扩散、复制或衍生到参考未覆盖的区域。\n"
            "背面图案必须原样复现：不得新增参考图中不存在的任何道具、手持物、植物、饰品或文字，不得改变任何元素的数量、姿态、朝向和颜色，不得改写文字内容与字体，不得改变图案的整体构图，不得镜像或左右翻转，不得调换任何元素的左右位置。\n"
            "背面图案必须是直接印在面料上的平面印花，与面料同面、无厚度；不得渲染成立体贴布、贴纸或补片，不得带白色描边、外发光或独立底色块。\n"
            "四张图的编号与顺序固定，不得互换角色。\n"
            "本模式不提供动作分镜参考图：动作方向、节奏、镜头与构图完全以文字时间线为准，不得自行增删动作、切镜或改变时序。\n"
            "不得改变用户指定的动作或速度。背景音乐后续由本地流程替换，生成音乐不作为最终配乐。"
        )
    elif mode == "video_continuation":
        rules = (
            "【三类素材职责与续写边界】\n"
            "1. 模特图（images第1张）是人物身份、脸、发型、前面服装、服装底色、面料质感、光线、场景和已有配饰的唯一基准。\n"
            "2. 反面平铺（images第2张）仅约束服装背面样式、结构及原有绣花/印花；不得迁移其底色、光线、面料质感或背景，提示词不描述具体图案。\n"
            "3. 源视频用于Muse视频续写，目标秒数表示续写后成片总时长；续写语义不等于已验证的动作迁移，不能保证只迁移动作/镜头或替换演员、服装与场景。不得自动改变用户指定的动作、速度或时长。\n"
            "背景音乐后续由本地流程替换，生成音乐不作为最终配乐。"
        )
    else:
        raise MuseBridgeError("不支持的 Muse 输入模式")
    return base + "\n\n" + rules


def _prepare_images(prompt_bytes: bytes, paths: tuple[str | Path, ...], roles: tuple[str, ...],
                    *, mode: str, seconds: int, source_duration: float | None = None) -> PreparedRequest:
    if not isinstance(prompt_bytes, bytes):
        raise MuseBridgeError("提示词必须以原始 UTF-8 字节提供")
    if len(paths) != len(roles):
        raise MuseBridgeError("参考素材角色数量不匹配")
    prompt = _compose_prompt(prompt_bytes, mode)
    fingerprints: list[dict[str, object]] = []
    images: list[str] = []
    source_paths: list[str] = []
    for path, role in zip(paths, roles):
        fingerprint, data = _image_fingerprint(path, role)
        fingerprints.append(fingerprint)
        images.append(base64.b64encode(data).decode("ascii"))
        source_paths.append(str(Path(path).absolute()))
    body = {
        "model": MODEL,
        "prompt": prompt,
        "aspect_ratio": "9:16",
        "seconds": str(seconds),
        "mute_voice": True,
        "images": images,
    }
    warnings: list[str] = []
    if seconds > 40:
        warnings.append("本地技能推荐 5–40 秒以获得更稳定产出；按原请求保留目标时长。")
    preflight: dict[str, object] = {
        "requested_seconds": seconds,
        "aspect_ratio": "9:16",
        "warnings": warnings,
        "verified_action_transfer": False,
    }
    if source_duration is not None:
        preflight.update({
            "source_duration_seconds": float(source_duration),
            "available_continuation_seconds": seconds - float(source_duration),
            "continuation_semantics": "skill_defined_continuation_not_verified_action_transfer",
        })
        if float(source_duration) > seconds - 2:
            warnings.append("续写余量少于建议的2秒；不会自动裁剪源视频或改变目标时长。")
    return PreparedRequest(
        mode=mode,
        body=body,
        prompt_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        roles=roles,
        asset_fingerprints=tuple(fingerprints),
        source_paths=tuple(source_paths),
        body_sha256=hashlib.sha256(_json_bytes(body)).hexdigest(),
        preflight=preflight,
    )


def prepare_image_request(prompt_bytes: bytes, model_image: str | Path,
                          reverse_flat: str | Path, action_storyboard: str | Path,
                          front_detail: str | Path | None = None,
                          back_wear: str | Path | None = None) -> PreparedRequest:
    """Prepare the fixed 10-second, 9:16 image path without network or credentials."""
    if back_wear is not None:
        if front_detail is not None:
            return _prepare_images(
                prompt_bytes,
                (model_image, reverse_flat, back_wear, front_detail),
                ("model_image", "reverse_flat", "back_wear", "front_detail"),
                mode="image_back_pair_front",
                seconds=10,
            )
        return _prepare_images(
            prompt_bytes,
            (model_image, reverse_flat, back_wear, action_storyboard),
            ("model_image", "reverse_flat", "back_wear", "action_storyboard"),
            mode="image_back_pair",
            seconds=10,
        )
    if front_detail is None:
        return _prepare_images(
            prompt_bytes,
            (model_image, reverse_flat, action_storyboard),
            ("model_image", "reverse_flat", "action_storyboard"),
            mode="image_three_roles",
            seconds=10,
        )
    return _prepare_images(
        prompt_bytes,
        (model_image, reverse_flat, action_storyboard, front_detail),
        ("model_image", "reverse_flat", "action_storyboard", "front_detail"),
        mode="image_four_roles",
        seconds=10,
    )


def prepare_video_continuation_request(prompt_bytes: bytes, model_image: str | Path,
                                       reverse_flat: str | Path, source_video: str | Path,
                                       *, requested_seconds: int, source_duration_seconds: float) -> PreparedRequest:
    """Offline preflight for Muse continuation; no upload or action-transfer claim."""
    if type(requested_seconds) is not int or requested_seconds not in SUPPORTED_SECONDS:
        raise MuseBridgeError("目标秒数不在 Muse 本地支持列表中")
    if requested_seconds < 20:
        raise MuseBridgeError("Muse视频续写要求目标总时长至少 20 秒；不会自动更改秒数")
    try:
        duration = float(source_duration_seconds)
    except (TypeError, ValueError):
        raise MuseBridgeError("源视频实测时长无效") from None
    if not math.isfinite(duration) or duration <= 0:
        raise MuseBridgeError("源视频实测时长无效")
    if duration >= requested_seconds:
        raise MuseBridgeError("源视频实测时长必须短于目标总时长；不会自动裁剪或变速")
    prepared = _prepare_images(
        prompt_bytes,
        (model_image, reverse_flat),
        ("model_image", "reverse_flat"),
        mode="video_continuation",
        seconds=requested_seconds,
        source_duration=duration,
    )
    video_data = _read_stable(source_video)
    video_path = str(Path(source_video).absolute())
    fingerprint = {
        "role": "action_video_continuation",
        "sha256": hashlib.sha256(video_data).hexdigest(),
        "size_bytes": len(video_data),
    }
    return PreparedRequest(
        mode=prepared.mode,
        body=prepared.body,
        prompt_sha256=prepared.prompt_sha256,
        roles=("model_image", "reverse_flat", "action_video_continuation"),
        asset_fingerprints=prepared.asset_fingerprints + (fingerprint,),
        source_paths=prepared.source_paths + (video_path,),
        body_sha256=prepared.body_sha256,
        preflight=prepared.preflight,
    )


def extract_model_catalog_status(http_status: int, payload: object) -> dict[str, object]:
    """Recognize a model as an exact registry key or exact model-list value."""
    result = {
        "http_status": http_status if type(http_status) is int else None,
        "model": MODEL,
        "model_status": "http_error",
        "credential_verified": False,
        "generation_permission_verified": False,
    }
    if type(http_status) is not int or not 200 <= http_status < 300:
        return result

    identity_fields = {"id", "model", "model_id", "model_name", "name"}
    registry_fields = {"models", "model_list", "supported_models", "items", "data", "matches", "channels"}

    def found(node: object, *, in_registry: bool = False, depth: int = 0) -> bool:
        if depth > 20:
            return False
        if isinstance(node, list):
            for item in node:
                if isinstance(item, str) and item == MODEL:
                    return True
                if found(item, in_registry=True, depth=depth + 1):
                    return True
        elif isinstance(node, dict):
            for key, value in node.items():
                normalized = str(key).casefold()
                if key == MODEL:
                    return True
                if normalized in identity_fields and value == MODEL:
                    return True
                if normalized in registry_fields and found(value, in_registry=True, depth=depth + 1):
                    return True
                if isinstance(value, (dict, list)) and in_registry and found(value, in_registry=True, depth=depth + 1):
                    return True
        return False

    if found(payload):
        result["model_status"] = "listed"
    else:
        result["model_status"] = "not_established"
    return result


def _identity(value: object, *, version: bool = False) -> str:
    regex = _VERSION if version else _SKU
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise MuseBridgeError("款号或版本号格式无效")
    return value


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


class MuseTaskStore:
    """Atomic `.muse_bridge` records, exclusive version locks, and task receipts."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).absolute()
        self.base = self.root / ".muse_bridge"

    def _path(self, category: str, sku: str, version: str, suffix: str = ".json") -> Path:
        return self.base / category / _identity(sku) / f"{_identity(version, version=True)}{suffix}"

    def _ensure_dir(self, target: Path) -> None:
        try:
            relative = target.absolute().relative_to(self.root)
        except ValueError:
            raise MuseBridgeError("Muse记录路径超出项目目录") from None
        current = self.root
        for part in relative.parts:
            current = current / part
            try:
                info = current.lstat()
            except FileNotFoundError:
                try:
                    current.mkdir()
                except FileExistsError:
                    pass
                info = current.lstat()
            except OSError:
                raise MuseBridgeError("Muse记录目录不可用") from None
            if (not stat.S_ISDIR(info.st_mode) or current.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400):
                raise MuseBridgeError("Muse记录目录不安全")

    def _read_json(self, path: Path) -> dict | None:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 1024 * 1024):
                raise MuseBridgeError("Muse记录文件不安全")
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except FileNotFoundError:
            return None
        except MuseBridgeError:
            raise
        except (OSError, ValueError, UnicodeError):
            raise MuseBridgeError("Muse记录文件无效") from None

    def _write_json(self, path: Path, value: Mapping[str, object]) -> None:
        self._ensure_dir(path.parent)
        try:
            if path.exists() and (path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode)):
                raise MuseBridgeError("Muse记录目标不安全")
            raw = _json_bytes(dict(value))
            temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
            descriptor = os.open(temp, flags, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
                raise
            os.replace(temp, path)
        except MuseBridgeError:
            raise
        except (OSError, ValueError, TypeError):
            raise MuseBridgeError("Muse记录保存失败") from None

    def _write_bytes_once(self, path: Path, raw: bytes) -> None:
        self._ensure_dir(path.parent)
        if path.exists():
            try:
                if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode) or path.read_bytes() != raw:
                    raise MuseBridgeError("该版本已有不同的提示词审核文件")
                return
            except MuseBridgeError:
                raise
            except OSError:
                raise MuseBridgeError("提示词审核文件不可读") from None
        temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(temp, flags, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if path.exists():
                if path.read_bytes() != raw:
                    raise MuseBridgeError("该版本已有不同的提示词审核文件")
                temp.unlink(missing_ok=True)
                return
            os.replace(temp, path)
        except MuseBridgeError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            raise MuseBridgeError("提示词审核文件保存失败") from None

    @contextmanager
    def lock(self, sku: str, version: str):
        path = self._path("locks", sku, version, ".lock")
        self._ensure_dir(path.parent)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            raise MuseBridgeError("该款号版本正在处理；请勿并行提交") from None
        except OSError:
            raise MuseBridgeError("无法取得 Muse 版本锁") from None
        try:
            os.close(descriptor)
            yield
        finally:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def save_review(self, prepared: PreparedRequest, sku: str, version: str) -> dict[str, object]:
        path = self._path("previews", sku, version)
        prompt_path = path.with_name(f"{_identity(version, version=True)}.prompt.txt")
        prompt_bytes = prepared.body["prompt"].encode("utf-8")
        record = {
            "schema_version": 1,
            "sku": _identity(sku),
            "version": _identity(version, version=True),
            "mode": prepared.mode,
            "model": MODEL,
            "seconds": prepared.body["seconds"],
            "aspect_ratio": prepared.body["aspect_ratio"],
            "prompt_sha256": prepared.prompt_sha256,
            "roles": list(prepared.roles),
            "asset_fingerprints": list(prepared.asset_fingerprints),
            "source_paths": list(prepared.source_paths),
            "preflight": prepared.preflight,
        }
        existing = self._read_json(path)
        if existing is not None:
            if existing != record or self._read_prompt(prompt_path) != prompt_bytes:
                raise MuseBridgeError("该版本已有不同的提示词或素材预检记录")
            return self._review_result(path, prompt_path, prepared.prompt_sha256)
        self._write_bytes_once(prompt_path, prompt_bytes)
        self._write_json(path, record)
        return self._review_result(path, prompt_path, prepared.prompt_sha256)

    def _read_prompt(self, path: Path) -> bytes | None:
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or path.is_symlink()
                    or getattr(info, "st_file_attributes", 0) & 0x400 or info.st_size > 1024 * 1024):
                raise MuseBridgeError("提示词审核文件不安全")
            return path.read_bytes()
        except FileNotFoundError:
            return None
        except MuseBridgeError:
            raise
        except OSError:
            raise MuseBridgeError("提示词审核文件不可读") from None

    def _review_result(self, path: Path, prompt_path: Path, sha256: str) -> dict[str, object]:
        return {
            "state": "review_saved",
            "prompt_sha256": sha256,
            "review_record_path": path.relative_to(self.root).as_posix(),
            "prompt_review_path": prompt_path.relative_to(self.root).as_posix(),
        }

    def review_record(self, sku: str, version: str) -> dict | None:
        return self._read_json(self._path("previews", sku, version))

    def task_record(self, sku: str, version: str) -> dict | None:
        task_path = self._path("tasks", sku, version)
        receipt_path = self._path("receipts", sku, version)
        record = self._read_json(task_path)
        receipt = self._read_json(receipt_path)
        if record is None and receipt is None:
            return None
        if record is not None:
            if (record.get("sku") != sku or record.get("version") != version
                    or record.get("model") != MODEL or record.get("schema_version") != 1):
                raise MuseBridgeError("Muse任务记录与款号、版本或模型不匹配")
        if receipt is not None:
            if (receipt.get("sku") != sku or receipt.get("version") != version
                    or receipt.get("model") != MODEL or receipt.get("schema_version") != 1
                    or not isinstance(receipt.get("task_id"), str)
                    or not _TASK_ID.fullmatch(receipt["task_id"])):
                raise MuseBridgeError("Muse任务回执无效或与款号版本不匹配")
            if record is None:
                preview = self.review_record(sku, version)
                if (not _receipt_has_full_binding(receipt)
                        or not _bindings_match(receipt, preview or {})):
                    raise MuseBridgeError("孤立任务回执缺少可核验的任务输入绑定；禁止再次提交")
                record = {key: receipt[key] for key in _TASK_BINDING_FIELDS}
                record.update(state="task_known", task_id=receipt["task_id"],
                              generation_attempts=1, upload_attempts=0, failure_category=None)
                if isinstance(receipt.get("source_video_url_sha256"), str):
                    record["source_video_url_sha256"] = receipt["source_video_url_sha256"]
                return record
            for key in _TASK_BINDING_FIELDS:
                if key in receipt and receipt[key] != record.get(key):
                    raise MuseBridgeError("Muse任务回执与主记录输入绑定冲突")
            main_id, receipt_id = record.get("task_id"), receipt.get("task_id")
            if main_id and main_id != receipt_id:
                raise MuseBridgeError("Muse主记录与任务回执的任务号冲突")
            if not main_id and _receipt_has_full_binding(receipt) and _bindings_match(receipt, record):
                record["task_id"] = receipt_id
                record["state"] = "task_known"
        elif record.get("task_id") and not _TASK_ID.fullmatch(str(record["task_id"])):
            raise MuseBridgeError("Muse主记录任务号格式无效")
        return record

    def create_task(self, sku: str, version: str, record: Mapping[str, object]) -> None:
        path = self._path("tasks", sku, version)
        receipt_path = self._path("receipts", sku, version)
        if (path.exists() or path.is_symlink() or receipt_path.exists() or receipt_path.is_symlink()):
            raise MuseBridgeError("该款号版本已有任务记录，禁止再次提交")
        self._write_json(path, record)

    def update_task(self, sku: str, version: str, record: Mapping[str, object]) -> None:
        path = self._path("tasks", sku, version)
        if self._read_json(path) is None:
            raise MuseBridgeError("Muse任务记录缺失")
        self._write_json(path, record)

    def persist_task_id(self, sku: str, version: str, record: Mapping[str, object], task_id: str) -> str:
        if not _TASK_ID.fullmatch(task_id):
            raise MuseBridgeError("Muse任务号格式无效")
        main_path = self._path("tasks", sku, version)
        receipt_path = self._path("receipts", sku, version)
        current_main = self._read_json(main_path)
        current_receipt = self._read_json(receipt_path)
        if current_main is None or not _bindings_match(record, current_main):
            raise MuseBridgeError("任务创建记录与提交内容不匹配")
        if current_receipt is not None and (
                current_receipt.get("task_id") != task_id
                or not _bindings_match(current_receipt, record)):
            raise MuseBridgeError("现有任务回执与本次创建结果冲突")
        receipt = {key: record[key] for key in _TASK_BINDING_FIELDS if key in record}
        receipt["task_id"] = task_id
        receipt["state"] = "task_known"
        receipt["generation_attempts"] = record.get("generation_attempts", 1)
        receipt["upload_attempts"] = record.get("upload_attempts", 0)
        if "source_video_url_sha256" in record:
            receipt["source_video_url_sha256"] = record["source_video_url_sha256"]
        receipt_ok = False
        main_ok = False
        try:
            self._write_json(receipt_path, receipt)
            receipt_ok = True
        except Exception:
            pass
        try:
            self._write_json(main_path, record)
            main_ok = True
        except Exception:
            pass
        if not receipt_ok and not main_ok:
            raise MuseBridgeError("任务号未能写入主记录或安全回执")
        if receipt_ok and main_ok:
            return "both"
        return "receipt" if receipt_ok else "main"


def _receipt_has_full_binding(receipt: Mapping[str, object]) -> bool:
    return all(key in receipt for key in _TASK_BINDING_FIELDS)


def _bindings_match(left: Mapping[str, object], right: Mapping[str, object]) -> bool:
    return _receipt_has_full_binding(left) and all(left.get(key) == right.get(key) for key in _TASK_BINDING_FIELDS)


def _nested(payload: object) -> dict:
    if not isinstance(payload, dict):
        return {}
    return payload.get("data") if isinstance(payload.get("data"), dict) else payload


def _task_id(payload: object) -> str:
    values, invalid = _collect_task_ids(payload)
    return values[0] if not invalid and len(values) == 1 else ""


def _collect_task_ids(payload: object) -> tuple[list[str], bool]:
    if not isinstance(payload, dict):
        return [], False
    found: list[str] = []
    invalid = False
    nodes = [payload]
    node = payload
    for _ in range(8):
        nested = node.get("data") if isinstance(node, dict) else None
        if not isinstance(nested, dict):
            break
        nodes.append(nested)
        node = nested
    for item in nodes:
        for key in ("task_id", "taskId", "id"):
            if key not in item:
                continue
            value = item[key]
            if value is None or value == "":
                continue
            if (isinstance(value, (str, int)) and not isinstance(value, bool)
                    and _TASK_ID.fullmatch(str(value).strip())):
                candidate = str(value).strip()
                if candidate not in found:
                    found.append(candidate)
            else:
                invalid = True
    return found, invalid


def _task_ids(payload: object) -> list[str]:
    return _collect_task_ids(payload)[0]


def _task_echo_conflict(payload: object) -> bool:
    values, invalid = _collect_task_ids(payload)
    return invalid or len(values) > 1


def _remote_status(payload: object) -> str:
    if not isinstance(payload, dict):
        return "unknown"
    nodes = [payload]
    node = payload
    for _ in range(8):
        nested = node.get("data") if isinstance(node, dict) else None
        if not isinstance(nested, dict):
            break
        nodes.append(nested)
        node = nested
    for key in ("task_state", "task_status"):
        for item in nodes:
            value = item.get(key)
            if isinstance(value, str) and value.strip() and not value.isdigit():
                return value.strip().casefold()
    for item in nodes:
        for key in ("state", "status"):
            value = item.get(key)
            if isinstance(value, str) and value.strip() and not value.isdigit():
                normalized = value.strip().casefold()
                if normalized in DONE or normalized in (FAILED - {"error"}) or normalized in {
                        "queued", "pending", "processing", "running", "in_progress", "in-progress"}:
                    return normalized
    if any(item.get("success") is False for item in nodes):
        return "api_error"
    if any(isinstance(item.get("status"), str) and item["status"].strip().casefold() == "error"
           for item in nodes):
        return "api_error"
    return "unknown"


def _output_video_url(payload: object, excluded_url_sha256: object = None) -> str:
    if not isinstance(payload, dict):
        return ""
    excluded_hash = excluded_url_sha256 if isinstance(excluded_url_sha256, str) else ""
    candidates: set[str] = set()
    direct_fields = {"video_url", "output_video_url", "download_url", "output_url"}
    output_containers = {"output", "outputs", "result", "results", "download"}
    nested_containers = output_containers | {"video", "file"}

    def add(node: Mapping[str, object], *, allow_generic_url: bool) -> None:
        fields = direct_fields | ({"url"} if allow_generic_url else set())
        for key in fields:
            value = node.get(key)
            if not isinstance(value, str) or not _safe_https_url(value):
                continue
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            if not excluded_hash or digest != excluded_hash:
                candidates.add(value)

    def visit_output(node: object, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(node, dict):
            add(node, allow_generic_url=True)
            for key in nested_containers:
                child = node.get(key)
                if isinstance(child, (dict, list)):
                    visit_output(child, depth + 1)
        elif isinstance(node, list):
            for child in node:
                visit_output(child, depth + 1)

    # The root and the documented data envelope may expose a direct video URL.
    add(payload, allow_generic_url=False)
    data = payload.get("data")
    if isinstance(data, dict):
        add(data, allow_generic_url=False)
    # Nested media URLs are accepted only inside explicit output/result containers.
    for container in output_containers:
        child = payload.get(container)
        if isinstance(child, (dict, list)):
            visit_output(child)
        if isinstance(data, dict):
            child = data.get(container)
            if isinstance(child, (dict, list)):
                visit_output(child)
    return next(iter(candidates)) if len(candidates) == 1 else ""


def _candidate_extension(url: str) -> str:
    suffix = Path(urlsplit_path(url)).suffix.lower()
    return suffix if suffix in {".mp4", ".webm", ".mov", ".m4v"} else ".mp4"


def urlsplit_path(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).path


def _probe_candidate(path: Path) -> dict[str, object]:
    capture = None
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            raise ValueError
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        declared_frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        decoded_frames = 0
        while True:
            ok, _frame = capture.read()
            if not ok:
                break
            decoded_frames += 1
        if (width <= 0 or height <= 0 or not math.isfinite(fps) or fps <= 0 or decoded_frames <= 0
                or declared_frames <= 0 or decoded_frames != declared_frames):
            raise ValueError
        return {"duration": decoded_frames / fps, "width": width, "height": height, "decoded": True}
    except Exception:
        raise MuseBridgeError("候选视频无法完整解码") from None
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def probe_video_source(path: str | Path) -> float:
    """Decode a local source clip to obtain its duration without uploading it."""
    try:
        result = _probe_candidate(Path(path))
        return float(result["duration"])
    except MuseBridgeError:
        raise MuseBridgeError("源视频无法完整解码或读取时长") from None


class MuseBridge:
    def __init__(self, root: str | Path, *, client, credential_store,
                 media_probe: Callable[[Path], Mapping[str, object]] | None = None,
                 audit_log=None) -> None:
        self.root = Path(root).absolute()
        self.client = client
        self.credentials = credential_store
        self.store = MuseTaskStore(self.root)
        self.media_probe = _probe_candidate if media_probe is None else media_probe
        self.audit_log = audit_log

    def review(self, prepared: PreparedRequest, *, sku: str, version: str) -> dict[str, object]:
        return self.store.save_review(prepared, sku, version)

    def _assert_reviewed(self, prepared: PreparedRequest, sku: str, version: str,
                         confirmed_prompt_sha256: str) -> None:
        self._validate_prepared_body(prepared)
        review = self.store.review_record(sku, version)
        if (not review or review.get("sku") != sku or review.get("version") != version
                or review.get("schema_version") != 1 or review.get("model") != MODEL
                or not isinstance(confirmed_prompt_sha256, str)
                or confirmed_prompt_sha256 != prepared.prompt_sha256
                or review.get("prompt_sha256") != prepared.prompt_sha256
                or review.get("mode") != prepared.mode
                or review.get("asset_fingerprints") != list(prepared.asset_fingerprints)
                or review.get("roles") != list(prepared.roles)
                or review.get("seconds") != prepared.body.get("seconds")
                or review.get("aspect_ratio") != prepared.body.get("aspect_ratio")):
            raise MuseBridgeError("提示词 SHA 或素材与已审核预检不一致")
        prompt_path = self.store._path("previews", sku, version).with_name(f"{version}.prompt.txt")
        prompt_bytes = self.store._read_prompt(prompt_path)
        if (prompt_bytes is None or hashlib.sha256(prompt_bytes).hexdigest() != prepared.prompt_sha256
                or prompt_bytes != prepared.body["prompt"].encode("utf-8")):
            raise MuseBridgeError("已审核提示词文件缺失或 SHA 不匹配")

    def _validate_prepared_body(self, prepared: PreparedRequest) -> None:
        try:
            body = prepared.body
            if hashlib.sha256(_json_bytes(body)).hexdigest() != prepared.body_sha256:
                raise MuseBridgeError("实际请求内容与已预检内容不一致")
            if not isinstance(body, dict) or set(body) != {
                    "model", "prompt", "aspect_ratio", "seconds", "mute_voice", "images"}:
                raise MuseBridgeError("请求字段与已审核范围不一致")
            if (body.get("model") != MODEL or body.get("aspect_ratio") != IMAGE_ASPECT_RATIO
                    or type(body.get("seconds")) is not str
                    or type(body.get("mute_voice")) is not bool or body.get("mute_voice") is not True
                    or type(body.get("prompt")) is not str
                    or hashlib.sha256(body["prompt"].encode("utf-8")).hexdigest() != prepared.prompt_sha256):
                raise MuseBridgeError("请求参数与已审核内容不一致")
            expected_roles = {
                "image_three_roles": ("model_image", "reverse_flat", "action_storyboard"),
                "image_four_roles": ("model_image", "reverse_flat", "action_storyboard", "front_detail"),
                "image_back_pair": ("model_image", "reverse_flat", "back_wear", "action_storyboard"),
                "image_back_pair_front": ("model_image", "reverse_flat", "back_wear", "front_detail"),
                "video_continuation": ("model_image", "reverse_flat", "action_video_continuation"),
            }.get(prepared.mode)
            image_count = {
                "image_three_roles": 3,
                "image_four_roles": 4,
                "image_back_pair": 4,
                "image_back_pair_front": 4,
                "video_continuation": 2,
            }.get(prepared.mode, 0)
            if (expected_roles is None or prepared.roles != expected_roles
                    or len(prepared.asset_fingerprints) != len(expected_roles)
                    or len(prepared.source_paths) != len(expected_roles)
                    or not isinstance(body.get("images"), list)
                    or len(body["images"]) != image_count):
                raise MuseBridgeError("素材角色与已审核范围不一致")
            if prepared.mode in ("image_three_roles", "image_four_roles", "image_back_pair",
                                 "image_back_pair_front") and body["seconds"] != IMAGE_SECONDS:
                raise MuseBridgeError("三图预检时长与固定要求不一致")
            if prepared.mode == "video_continuation" and (
                    not body["seconds"].isdigit() or int(body["seconds"]) not in SUPPORTED_SECONDS
                    or int(body["seconds"]) < 20):
                raise MuseBridgeError("视频续写时长与已审核范围不一致")
            for index, encoded in enumerate(body["images"]):
                if type(encoded) is not str:
                    raise MuseBridgeError("请求图片内容与已审核素材不一致")
                decoded = base64.b64decode(encoded, validate=True)
                source = _read_stable(prepared.source_paths[index])
                fingerprint = prepared.asset_fingerprints[index]
                if (decoded != source or fingerprint.get("role") != expected_roles[index]
                        or fingerprint.get("sha256") != hashlib.sha256(source).hexdigest()
                        or fingerprint.get("size_bytes") != len(source)):
                    raise MuseBridgeError("请求图片内容与已审核素材不一致")
            if prepared.mode == "video_continuation":
                source = _read_stable(prepared.source_paths[-1])
                fingerprint = prepared.asset_fingerprints[-1]
                if (fingerprint.get("role") != "action_video_continuation"
                        or fingerprint.get("sha256") != hashlib.sha256(source).hexdigest()
                        or fingerprint.get("size_bytes") != len(source)):
                    raise MuseBridgeError("源视频与已审核素材不一致")
        except MuseBridgeError:
            raise
        except Exception:
            raise MuseBridgeError("请求内容或素材无法与已审核记录核对") from None

    def _credential_present(self) -> bool:
        try:
            status = self.credentials.status()
        except Exception:
            raise MuseBridgeError("无法读取凭据状态") from None
        if isinstance(status, dict) and status.get("api-key") != "PRESENT":
            try:
                self.credentials.set_interactive()
                status = self.credentials.status()
            except Exception:
                raise MuseBridgeError("Muse 凭据输入已取消或保存失败") from None
        if not isinstance(status, dict) or status.get("api-key") != "PRESENT":
            raise MuseBridgeError("Muse 与现有 Aimaxhug 凭据均未配置")
        return True

    def _base_record(self, prepared: PreparedRequest, sku: str, version: str) -> dict[str, object]:
        return {
            "schema_version": 1,
            "sku": sku,
            "version": version,
            "model": MODEL,
            "mode": prepared.mode,
            "state": "creation_unknown",
            "prompt_sha256": prepared.prompt_sha256,
            "roles": list(prepared.roles),
            "asset_fingerprints": list(prepared.asset_fingerprints),
            "seconds": prepared.body["seconds"],
            "aspect_ratio": prepared.body["aspect_ratio"],
            "generation_attempts": 1,
            "upload_attempts": 0,
            "failure_category": None,
        }

    def submit(self, prepared: PreparedRequest, *, sku: str, version: str,
               confirmed_prompt_sha256: str, confirm_paid_request: bool = False,
               confirm_media_upload: bool = False,
               confirm_video_continuation: bool = False) -> dict[str, object]:
        sku = _identity(sku)
        version = _identity(version, version=True)
        if not confirm_paid_request:
            raise MuseBridgeError("必须显式确认付费提交")
        self._assert_reviewed(prepared, sku, version, confirmed_prompt_sha256)
        if prepared.mode == "video_continuation":
            if not confirm_media_upload:
                raise MuseBridgeError("视频续写必须显式确认上传原始源视频")
            if not confirm_video_continuation:
                raise MuseBridgeError("视频续写提交必须显式确认其为续写模式，不代表已验证动作迁移")
        self._credential_present()
        prepare_credentials = getattr(self.client, "prepare_credentials", None)
        if callable(prepare_credentials):
            try:
                prepare_credentials()
            except Exception:
                raise MuseBridgeError("提交前无法使用凭据") from None

        with self.store.lock(sku, version):
            if self.store.task_record(sku, version) is not None:
                raise MuseBridgeError("该款号版本已有任务记录，禁止再次提交")
            record = self._base_record(prepared, sku, version)
            body = dict(prepared.body)
            if prepared.mode == "video_continuation":
                try:
                    video_path = Path(prepared.source_paths[-1])
                    video_bytes = _read_stable(video_path)
                    if hashlib.sha256(video_bytes).hexdigest() != prepared.asset_fingerprints[-1]["sha256"]:
                        raise MuseBridgeError("源视频与预检指纹不一致")
                    mime_type = mimetypes.guess_type(video_path.name)[0] or "video/mp4"
                    if not mime_type.startswith("video/"):
                        mime_type = "video/mp4"
                except MuseBridgeError:
                    raise
                record.update(state="upload_unknown", upload_attempts=1)
                self.store.create_task(sku, version, record)
                try:
                    video_url = self.client.upload_video(video_bytes, video_path.name, mime_type)
                    if not _safe_https_url(video_url):
                        raise MuseClientError("upload_unknown")
                except Exception as exc:
                    category = exc.kind if isinstance(exc, MuseClientError) else "upload_unknown"
                    record["state"] = "upload_error" if category == "upload_http_error" else "upload_unknown"
                    record["failure_category"] = category
                    self.store.update_task(sku, version, record)
                    return {"state": record["state"], "failure_category": record["failure_category"]}
                body["video_url"] = video_url
                record["source_video_url_sha256"] = hashlib.sha256(video_url.encode("utf-8")).hexdigest()
                record.update(state="creation_unknown", failure_category=None)
                self.store.update_task(sku, version, record)
            else:
                self.store.create_task(sku, version, record)

            if self.audit_log is not None:
                try:
                    self.audit_log.set_paid_creation_status("creation_unknown")
                except Exception:
                    return {"state": "creation_unknown", "failure_category": "audit_state_unavailable"}

            try:
                http_status, payload = self.client.post_video(body)
            except Exception as exc:
                record.update(state="creation_unknown", failure_category=(
                    exc.kind if isinstance(exc, MuseClientError) else "transport_unknown"))
                self.store.update_task(sku, version, record)
                return {"state": "creation_unknown", "failure_category": record["failure_category"]}

            task_id = _task_id(payload)
            if not task_id:
                if type(http_status) is int and not 200 <= http_status < 300:
                    category = "http_error"
                else:
                    category = "task_id_ambiguous" if _task_echo_conflict(payload) else "task_id_missing"
                record.update(state="creation_unknown", failure_category=category)
                if type(http_status) is int:
                    record["http_status"] = http_status
                self.store.update_task(sku, version, record)
                return {"state": "creation_unknown", "failure_category": category,
                        "http_status": http_status if type(http_status) is int else None}

            record.update(state="task_known", task_id=task_id, failure_category=None)
            if type(http_status) is int:
                record["http_status"] = http_status
            try:
                persisted = self.store.persist_task_id(sku, version, record, task_id)
            except MuseBridgeError:
                return {"state": "task_id_persistence_failed", "task_id": task_id}
            if self.audit_log is not None:
                try:
                    self.audit_log.set_paid_creation_status("task_known")
                except Exception:
                    return {"state": "task_known_audit_state_pending", "task_id": task_id}
            result_state = "task_known" if persisted == "both" else f"task_known_{persisted}_saved"
            return {"state": result_state, "task_id": task_id,
                    "http_status": http_status if type(http_status) is int else None}

    def resume(self, sku: str, version: str) -> dict[str, object]:
        sku = _identity(sku)
        version = _identity(version, version=True)
        with self.store.lock(sku, version):
            main_record = self.store._read_json(self.store._path("tasks", sku, version))
            record = self.store.task_record(sku, version)
            if record is None:
                raise MuseBridgeError("Muse任务记录不存在")
            if main_record is None:
                self.store._write_json(self.store._path("tasks", sku, version), record)
            elif not main_record.get("task_id") and record.get("task_id"):
                self.store.update_task(sku, version, record)
            if record.get("state") == "candidate_pending_review":
                return {"state": "candidate_pending_review", "candidate_path": record.get("candidate_path")}
            task_id = record.get("task_id")
            if not isinstance(task_id, str) or not _TASK_ID.fullmatch(task_id):
                raise MuseBridgeError("没有已保存的任务号；创建结果未知，禁止重新提交")
            try:
                self._credential_present()
            except MuseBridgeError:
                return {"state": "credential_error", "failure_category": "credential_unavailable"}
            prepare_credentials = getattr(self.client, "prepare_credentials", None)
            if callable(prepare_credentials):
                try:
                    prepare_credentials()
                except Exception:
                    return {"state": "credential_error", "failure_category": "credential_unavailable"}
            try:
                http_status, payload = self.client.get_task(task_id)
            except Exception as exc:
                result = {"state": "transport_error", "failure_category": (
                    exc.kind if isinstance(exc, MuseClientError) else "transport_error")}
                record["last_query_state"] = result["state"]
                record["last_query_failure_category"] = result["failure_category"]
                self.store.update_task(sku, version, record)
                return result
            if type(http_status) is not int or not 200 <= http_status < 300:
                record["last_query_state"] = "http_error"
                record["last_query_http_status"] = http_status if type(http_status) is int else None
                self.store.update_task(sku, version, record)
                return {"state": "http_error", "http_status": http_status if type(http_status) is int else None}

            echoes = _task_ids(payload)
            status = _remote_status(payload)
            media_url = _output_video_url(payload, record.get("source_video_url_sha256"))
            if status == "unknown" and media_url:
                status = "completed"
            is_terminal = status in DONE or status in FAILED
            if _task_echo_conflict(payload):
                record["last_query_state"] = "identity_mismatch"
                record["last_query_http_status"] = http_status
                self.store.update_task(sku, version, record)
                return {"state": "identity_mismatch", "http_status": http_status}
            if task_id not in echoes:
                state = "identity_mismatch" if echoes else ("echo_missing" if is_terminal or media_url else "processing")
                record["last_query_state"] = state
                record["last_query_http_status"] = http_status
                self.store.update_task(sku, version, record)
                return {"state": state, "http_status": http_status}

            if status in FAILED:
                record.update(state="remote_failed", remote_status=status,
                              last_query_http_status=http_status)
                self.store.update_task(sku, version, record)
                return {"state": "remote_failed", "remote_status": status, "http_status": http_status}
            if status == "api_error":
                record["last_query_state"] = "api_error"
                record["last_query_http_status"] = http_status
                self.store.update_task(sku, version, record)
                return {"state": "api_error", "http_status": http_status}
            if status not in DONE:
                record.update(state="processing", remote_status=status,
                              last_query_http_status=http_status)
                self.store.update_task(sku, version, record)
                return {"state": "processing", "remote_status": status, "http_status": http_status}
            if not media_url:
                record.update(state="remote_completed_no_media", remote_status=status,
                              last_query_http_status=http_status)
                self.store.update_task(sku, version, record)
                return {"state": "remote_completed_no_media", "http_status": http_status}

            return self._download_validate_publish(sku, version, record, media_url, http_status)

    def _download_validate_publish(self, sku: str, version: str, record: dict[str, object],
                                   media_url: str, http_status: int) -> dict[str, object]:
        extension = _candidate_extension(media_url)
        staging = self.root / ".muse_bridge" / "staging" / sku / f"{version}-{uuid.uuid4().hex}{extension}"
        self.store._ensure_dir(staging.parent)
        try:
            downloaded = self.client.download_candidate(media_url, staging, allowed_root=self.root / ".muse_bridge")
            downloaded = Path(downloaded)
            if (downloaded.absolute() != staging.absolute() or downloaded.is_symlink()
                    or not downloaded.is_file() or downloaded.stat().st_size <= 0):
                raise MuseBridgeError("候选视频文件为空")
            metrics = self.media_probe(downloaded)
            duration = float(metrics.get("duration"))
            width = int(metrics.get("width"))
            height = int(metrics.get("height"))
            decoded = metrics.get("decoded") is True
            expected_seconds = int(record["seconds"])
            valid = (decoded and width > 0 and height > 0 and width * 16 == height * 9
                     and math.isfinite(duration)
                     and abs(duration - expected_seconds) <= _DURATION_TOLERANCE_SECONDS)
            if not valid:
                record.update(state="technical_validation_failed", remote_status="completed",
                              last_query_http_status=http_status)
                self.store.update_task(sku, version, record)
                return {"state": "technical_validation_failed"}
            candidate_dir = self.root / "02_待审核" / "Muse" / sku
            self.store._ensure_dir(candidate_dir)
            destination = candidate_dir / f"{version}{extension}"
            if destination.exists() or destination.is_symlink():
                record.update(state="technical_validation_failed", failure_category="candidate_collision")
                self.store.update_task(sku, version, record)
                return {"state": "technical_validation_failed"}
            try:
                _publish_temp_exclusive(downloaded, destination)
            except (FileExistsError, DownloadTargetExists):
                record.update(state="technical_validation_failed", failure_category="candidate_collision")
                self.store.update_task(sku, version, record)
                return {"state": "technical_validation_failed"}
            relative = destination.relative_to(self.root).as_posix()
            record.update(
                state="candidate_pending_review",
                remote_status="completed",
                candidate_path=relative,
                candidate_duration_seconds=duration,
                candidate_width=width,
                candidate_height=height,
                last_query_http_status=http_status,
            )
            self.store.update_task(sku, version, record)
            return {"state": "candidate_pending_review", "candidate_path": relative,
                    "duration_seconds": duration, "width": width, "height": height}
        except Exception:
            record.update(state="technical_validation_failed", failure_category="candidate_validation")
            self.store.update_task(sku, version, record)
            return {"state": "technical_validation_failed"}
        finally:
            try:
                if staging.exists():
                    staging.unlink()
            except OSError:
                pass


def _summary(prepared: PreparedRequest, review: Mapping[str, object] | None = None) -> dict[str, object]:
    result: dict[str, object] = {
        "state": "validated",
        "submitted": False,
        "model": MODEL,
        "mode": prepared.mode,
        "seconds": prepared.body["seconds"],
        "aspect_ratio": prepared.body["aspect_ratio"],
        "roles": list(prepared.roles),
        "prompt_sha256": prepared.prompt_sha256,
        "asset_fingerprints": list(prepared.asset_fingerprints),
        "preflight": prepared.preflight,
    }
    if review:
        result.update({key: review[key] for key in ("review_record_path", "prompt_review_path") if key in review})
    return result


def _read_prompt_file(value: str) -> bytes:
    return _read_stable(value)


def _add_common(parser, *, video: bool) -> None:
    parser.add_argument("--sku", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--prompt-file", required=True)
    parser.add_argument("--model-image", required=True)
    parser.add_argument("--reverse-flat", required=True)
    if video:
        parser.add_argument("--source-video", required=True)
        parser.add_argument("--seconds", required=True, type=int, choices=SUPPORTED_SECONDS)
    else:
        parser.add_argument("--action-storyboard", required=True)
        parser.add_argument("--front-detail", help="可选的第四张正面细节特写；提供时启用四图模式")
        parser.add_argument("--back-wear", help="可选的第四张真人背面实拍；提供时启用背面双参考模式（与 --front-detail 互斥）")


def _prepared_from_args(args) -> PreparedRequest:
    prompt = _read_prompt_file(args.prompt_file)
    if args.command.endswith("video"):
        source_duration = probe_video_source(Path(args.source_video))
        return prepare_video_continuation_request(
            prompt,
            args.model_image,
            args.reverse_flat,
            args.source_video,
            requested_seconds=args.seconds,
            source_duration_seconds=source_duration,
        )
    return prepare_image_request(prompt, args.model_image, args.reverse_flat, args.action_storyboard,
                                 front_detail=getattr(args, "front_detail", None),
                                 back_wear=getattr(args, "back_wear", None))


def _build_parser():
    import argparse

    class RedactingArgumentParser(argparse.ArgumentParser):
        def error(self, message):
            # argparse includes the rejected value in `message`; never echo it.
            raise MuseBridgeError("命令参数缺失或无效") from None

    parser = RedactingArgumentParser(description="Muse Video Pro 独立项目入口")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=RedactingArgumentParser)
    sub.add_parser("list-models", help="只读查询公开模型目录，不验证凭据或生成权限")
    sub.add_parser("credential-status", help="只显示 Muse/Aimaxhug 凭据 PRESENT/ABSENT")
    sub.add_parser("set-credential", help="打开 Muse 专用遮罩凭据输入框")
    for command in ("preview-image", "submit-image"):
        item = sub.add_parser(command)
        _add_common(item, video=False)
        if command.startswith("submit"):
            item.add_argument("--prompt-sha256", required=True)
            item.add_argument("--confirm-paid-request", action="store_true")
    for command in ("preview-video", "submit-video"):
        item = sub.add_parser(command)
        _add_common(item, video=True)
        if command.startswith("submit"):
            item.add_argument("--prompt-sha256", required=True)
            item.add_argument("--confirm-paid-request", action="store_true")
            item.add_argument("--confirm-media-upload", action="store_true")
            item.add_argument("--confirm-video-continuation", action="store_true")
    resume = sub.add_parser("resume", help="只查询已保存任务号，不会创建任务")
    resume.add_argument("--sku", required=True)
    resume.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    """Dedicated CLI. Only `list-models`, submit, and resume can use network."""
    import sys

    from .muse_credentials import MuseCredentialError, MuseCredentialStore
    from .muse_client import MuseClient
    from .processing_log import ProcessingLog

    root = Path(os.environ.get("MUSE_BRIDGE_PROJECT_ROOT", Path.cwd())).absolute()
    raw_args = list(sys.argv[1:] if argv is None else argv)
    command_arg = raw_args[0] if raw_args else ""
    command = f"muse-{command_arg}" if re.fullmatch(r"[a-z][a-z0-9-]{0,54}", command_arg) else "muse-cli"
    mode = "paid" if command_arg in {"submit-image", "submit-video"} else (
        "read_only" if command_arg in {"list-models", "resume"} else "offline")
    paid_authorized = "--confirm-paid-request" in raw_args
    sku_hint = raw_args[raw_args.index("--sku") + 1] if "--sku" in raw_args and raw_args.index("--sku") + 1 < len(raw_args) else None
    version_hint = raw_args[raw_args.index("--version") + 1] if "--version" in raw_args and raw_args.index("--version") + 1 < len(raw_args) else None
    try:
        audit = ProcessingLog.start(root, command=command, sku=sku_hint, version=version_hint,
                                    network_mode=mode, paid_authorization_present=paid_authorized)
    except Exception:
        print(json.dumps({"state": "failed", "message": "无法创建本地安全处理日志"}, ensure_ascii=False))
        return 2

    output: dict[str, object] | None = None
    code = 2
    status = "failure"
    parsed_args = None
    try:
        try:
            parsed_args = _build_parser().parse_args(raw_args)
        except SystemExit as exc:
            code = int(exc.code) if isinstance(exc.code, int) else 2
            status = "success" if code == 0 else "refused"
            output = {"state": "help"} if code == 0 else {"state": "failed", "message": "命令参数缺失或无效"}

        if parsed_args is not None and parsed_args.command == "list-models":
            with MuseClient(api_key_provider=lambda: "") as client:
                try:
                    http_status, payload = client.get_model_catalog()
                    result = extract_model_catalog_status(http_status, payload)
                except MuseClientError:
                    result = {"http_status": None, "model": MODEL, "model_status": "transport_error",
                              "credential_verified": False, "generation_permission_verified": False}
            output = result
            code = 0 if result["model_status"] == "listed" else 1
            status = "success" if code == 0 else "failure"

        if parsed_args is not None and parsed_args.command in {"credential-status", "set-credential"}:
            credentials = MuseCredentialStore()
            if parsed_args.command == "credential-status":
                credential_status = credentials.status()
                output = {"api-key": "PRESENT" if credential_status.get("api-key") == "PRESENT" else "ABSENT"}
            else:
                credentials.set_interactive()
                output = {"state": "credential_saved", "api-key": "PRESENT"}
            code = 0
            status = "success"

        if parsed_args is not None and parsed_args.command not in {"list-models", "credential-status", "set-credential"}:
            credentials = MuseCredentialStore()
            client = MuseClient(api_key_provider=credentials.read)
            bridge = MuseBridge(root, client=client, credential_store=credentials, audit_log=audit)
            try:
                if parsed_args.command in {"preview-image", "preview-video"}:
                    prepared = _prepared_from_args(parsed_args)
                    review = bridge.review(prepared, sku=parsed_args.sku, version=parsed_args.version)
                    output = _summary(prepared, review)
                    code = 0
                    status = "success"
                elif parsed_args.command in {"submit-image", "submit-video"}:
                    prepared = _prepared_from_args(parsed_args)
                    output = bridge.submit(
                        prepared,
                        sku=parsed_args.sku,
                        version=parsed_args.version,
                        confirmed_prompt_sha256=parsed_args.prompt_sha256,
                        confirm_paid_request=parsed_args.confirm_paid_request,
                        confirm_media_upload=getattr(parsed_args, "confirm_media_upload", False),
                        confirm_video_continuation=getattr(parsed_args, "confirm_video_continuation", False),
                    )
                    code = 0 if output.get("state") in {
                        "task_known", "task_known_main_saved", "task_known_receipt_saved"} else 2
                    status = "success" if code == 0 else "failure"
                elif parsed_args.command == "resume":
                    output = bridge.resume(parsed_args.sku, parsed_args.version)
                    code = 0 if output.get("state") in {"processing", "candidate_pending_review"} else 1
                    status = "success" if code == 0 else "failure"
            finally:
                client.close()
    except (MuseBridgeError, MuseCredentialError) as exc:
        output = {"state": "failed", "message": str(exc)}
        code = 2
        status = "refused"
    except MuseClientError as exc:
        output = {"state": "failed", "message": str(exc)}
        code = 2
        status = "failure"
    except (OSError, ValueError, TypeError):
        output = {"state": "failed", "message": "本地文件或参数处理失败"}
        code = 2
        status = "failure"

    safe_sku = getattr(parsed_args, "sku", None) if parsed_args is not None else None
    safe_version = getattr(parsed_args, "version", None) if parsed_args is not None else None
    if not isinstance(safe_sku, str) or not _SKU.fullmatch(safe_sku):
        safe_sku = None
    if not isinstance(safe_version, str) or not _VERSION.fullmatch(safe_version):
        safe_version = None
    try:
        audit.record_step(
            model="local", role="query", reason="执行 Muse 工作流命令",
            result={"success": "命令成功", "refused": "命令被安全闸门拒绝", "failure": "命令执行失败"}[status],
            sku=safe_sku,
            version=safe_version,
        )
    except Exception:
        code = 2
        status = "failure"
        if output is None or not isinstance(output.get("task_id"), str):
            output = {"state": "failed", "message": "安全处理日志写入失败"}
    try:
        audit.finish(status=status, exit_code=code)
    except Exception:
        code = 2
        if output is None or not isinstance(output.get("task_id"), str):
            output = {"state": "failed", "message": "安全处理日志写入失败"}
    if output is not None:
        print(json.dumps(output, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

