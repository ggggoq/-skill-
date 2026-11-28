from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import ctypes
from PIL import Image
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Mapping

from .assets import (
    AssetError,
    _inspect_image,
    _inspect_snapshot,
    classify_current_sku_files,
    detect_media_kind,
)
from .models import ProjectPaths
from .task_store import _validate_sku_segment


_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov"})
_ALL_SUFFIXES = _IMAGE_SUFFIXES | _VIDEO_SUFFIXES
_ALLOWED_SOURCE_TEXT_FILES = frozenset({"需求.txt", "审核标准.txt"})
_ROLES = {
    "主参考图": ("主参考图", "image"),
    "服装细节_正面": ("服装细节_正面", "image"),
    "服装细节_背面": ("服装细节_背面", "image"),
    "服装细节_面料": ("服装细节_面料", "image"),
    "服装细节_其他": ("服装细节_其他", "image"),
    "场景参考": ("场景参考", "media"),
    "动作参考图": ("动作参考", "image"),
    "动作参考视频": ("动作参考", "video"),
}
_STANDARD_NAME = re.compile(
    r"^(?P<sku>[^\\/:*?\"<>|]+)_(?P<role>主参考图|服装细节_正面|服装细节_背面|服装细节_面料|服装细节_其他|场景参考|动作参考)_(?P<index>\d{2,})$"
)
_SKU_PREFIX_IN_FILENAME = re.compile(
    r"^(?P<sku>.+?)_(?:主参考图|服装细节(?:_[^_]+)?|场景参考|动作参考)(?:_\d+)?$"
)


class ImportError(ValueError):
    """A safe, user-actionable local folder import failure."""


def preflight_current_sku_folder(
    materials_root: str | os.PathLike[str], sku: str
) -> dict[str, list[object]]:
    """Classify files in exactly one current SKU folder, without sibling traversal."""
    _validate_sku_segment(sku)
    folder = Path(materials_root) / sku
    _reject_reparse(folder)
    if not folder.is_dir():
        raise ImportError("当前款号素材目录不存在")
    return classify_current_sku_files(folder)


def suggest_standard_names(
    source_folder: str | os.PathLike[str],
    sku: str,
    role_assignments: Mapping[str, str],
    *,
    materials_root: str | os.PathLike[str],
) -> dict[str, str]:
    """Build copyable names without changing or copying any source file."""
    _validate_sku_segment(sku)
    source = _validate_source_folder(source_folder)
    files = _ordinary_source_files(source)
    names = {path.name for path in files}
    if set(role_assignments) != names:
        missing = sorted(names - set(role_assignments))
        extra = sorted(set(role_assignments) - names)
        raise ImportError(
            "每个普通素材必须恰好指定一个角色；未指定："
            + ("、".join(missing) if missing else "无")
            + "；多余映射："
            + ("、".join(extra) if extra else "无")
        )
    pending_names = {name for name, role in role_assignments.items() if role == "待确认"}
    invalid_roles = sorted(
        {role for role in role_assignments.values() if role not in _ROLES and role != "待确认"}
    )
    if invalid_roles:
        raise ImportError("素材角色无效或待确认：" + "、".join(invalid_roles))
    main_count = sum(role == "主参考图" for role in role_assignments.values())
    if main_count > 1 or (main_count != 1 and not pending_names):
        raise ImportError("必须明确且只指定一张主参考图")
    for path in files:
        role = role_assignments[path.name]
        if role == "待确认":
            continue
        kind = _ROLES[role][1]
        media_kind = detect_media_kind(path)
        if media_kind is None and path.suffix.lower() in _IMAGE_SUFFIXES:
            media_kind = "image"
        elif media_kind is None and path.suffix.lower() in _VIDEO_SUFFIXES:
            media_kind = "video"
        allowed_kinds = {"image", "video"} if kind == "media" else {kind}
        if media_kind not in allowed_kinds:
            raise ImportError(f"素材角色与文件类型不匹配：{path.name} -> {role}")

    digests = {path.name: _sha256(path) for path in files}
    root = Path(materials_root)
    existing = root / sku
    max_by_role = {role: 0 for role in _ROLES}
    reusable: dict[tuple[str, str], str] = {}
    if existing.exists():
        _reject_reparse(existing)
        if not existing.is_dir():
            raise ImportError("现有款号素材路径不是普通目录")
        for path in existing.iterdir():
            if not path.is_file():
                continue
            match = _STANDARD_NAME.fullmatch(path.stem)
            if not match or match.group("sku") != sku:
                continue
            role = _role_from_name(match.group("role"), path.suffix, path)
            max_by_role[role] = max(max_by_role[role], int(match.group("index")))
            if role.startswith("服装细节_"):
                reusable[(role, _sha256(path))] = path.name

    result: dict[str, str] = {}
    next_by_role = {role: number for role, number in max_by_role.items()}
    for path in files:
        role = role_assignments[path.name]
        if role == "待确认":
            result[path.name] = "待确认"
            continue
        if role.startswith("服装细节_") and (role, digests[path.name]) in reusable:
            result[path.name] = reusable[(role, digests[path.name])]
            continue
        next_by_role[role] += 1
        if role in {"动作参考图", "动作参考视频"}:
            base_role = "动作参考"
        else:
            base_role = role
        target = f"{sku}_{base_role}_{next_by_role[role]:02d}{path.suffix.lower()}"
        if target in result.values() or (existing / target).exists():
            raise ImportError(f"建议文件名发生冲突：{target}")
        result[path.name] = target
    return result


def infer_sku(source_folder: str | os.PathLike[str]) -> str:
    """Infer one logical SKU, including numbered duplicate source-folder names."""
    source = _validate_source_folder(source_folder)
    files = _ordinary_source_files(source)
    candidates = {
        match.group("sku")
        for path in files
        if (match := _SKU_PREFIX_IN_FILENAME.fullmatch(path.stem))
    }
    if len(candidates) > 1:
        raise ImportError("文件名中存在多个冲突款号：" + "、".join(sorted(candidates)))
    folder_name = source.name
    candidate = next(iter(candidates), None)
    if candidate:
        try:
            _validate_sku_segment(candidate)
        except (TypeError, ValueError):
            raise ImportError("从文件名识别出的款号不是有效的 Windows 单级名称") from None
        if folder_name == candidate or re.fullmatch(re.escape(candidate) + r"\s+\d+", folder_name):
            return candidate
        try:
            _validate_sku_segment(folder_name)
        except (TypeError, ValueError):
            return candidate
        raise ImportError(
            f"文件夹名称与素材文件款号冲突：文件夹={folder_name}，文件={candidate}"
        )
    try:
        _validate_sku_segment(folder_name)
    except (TypeError, ValueError):
        raise ImportError("无法从文件夹或文件名唯一识别款号") from None
    if re.fullmatch(r".+\s+\d+", folder_name):
        raise ImportError("带数字副本后缀的文件夹缺少可唯一识别款号的内部文件名")
    return folder_name


def validate_standard_folder(
    source_folder: str | os.PathLike[str], sku: str
) -> dict[str, list[str]]:
    """Validate renamed filenames and return the formal role mapping."""
    _validate_sku_segment(sku)
    source = _validate_source_folder(source_folder)
    files = _ordinary_source_files(source)
    mapping: dict[str, list[str]] = {
        "主参考图": [],
        "服装细节参考": [],
        "场景参考": [],
        "动作参考图": [],
        "动作参考视频": [],
    }
    role_indexes: dict[str, list[int]] = {role: [] for role in _ROLES}
    media_seen: set[Path] = set()
    for path in files:
        match = _STANDARD_NAME.fullmatch(path.stem)
        if not match or match.group("sku") != sku:
            raise ImportError(f"文件名不符合款号与标准角色命名：{path.name}")
        role = _role_from_name(match.group("role"), path.suffix, path)
        kind = _ROLES[role][1]
        media_kind = detect_media_kind(path)
        if media_kind is None and path.suffix.lower() in _IMAGE_SUFFIXES:
            media_kind = "image"
        elif media_kind is None and path.suffix.lower() in _VIDEO_SUFFIXES:
            media_kind = "video"
        allowed_kinds = {"image", "video"} if kind == "media" else {kind}
        if media_kind not in allowed_kinds:
            raise ImportError(f"素材角色与文件内容类型不匹配：{path.name}")
        role_indexes[role].append(int(match.group("index")))
        resolved = path.resolve(strict=True)
        if resolved in media_seen:
            raise ImportError(f"同一素材重复映射：{path.name}")
        media_seen.add(resolved)
        if role == "主参考图":
            mapping["主参考图"].append(path.name)
        elif role.startswith("服装细节_"):
            mapping["服装细节参考"].append(path.name)
        elif role == "场景参考":
            mapping["场景参考"].append(path.name)
        elif role == "动作参考图":
            mapping["动作参考图"].append(path.name)
        else:
            mapping["动作参考视频"].append(path.name)
    if len(mapping["主参考图"]) != 1:
        raise ImportError("必须且只导入一张主参考图")
    if not mapping["动作参考视频"]:
        raise ImportError("至少需要一段动作参考视频")
    for role, indexes in role_indexes.items():
        if indexes and sorted(indexes) != list(range(min(indexes), max(indexes) + 1)):
            raise ImportError(f"{role}编号必须连续递增")
    return mapping


def import_standard_folder(
    source_folder: str | os.PathLike[str],
    sku: str,
    paths: ProjectPaths,
    *,
    role_assignments: Mapping[str, str] | None = None,
    video_probe=None,
) -> dict[str, object]:
    """Import one reference group, optionally normalizing names without touching sources."""
    source = _validate_source_folder(source_folder)
    source_files = _ordinary_source_files(source)
    source_by_name = {path.name: path for path in source_files}
    target_name_by_source = {path.name: path.name for path in source_files}
    detail_role_by_source: dict[str, str] = {}
    role_for_name: dict[str, str] = {}
    if role_assignments is None:
        mapping = validate_standard_folder(source, sku)
        for role, filenames in mapping.items():
            role_for_name.update({filename: role for filename in filenames})
        detail_role_by_source = {
            filename: _role_key_from_stem(Path(filename).stem)
            for filename in role_for_name
            if role_for_name[filename] == "服装细节参考"
        }
    else:
        target_name_by_source = suggest_standard_names(
            source,
            sku,
            role_assignments,
            materials_root=paths.materials,
        )
        pending = [name for name, target_name in target_name_by_source.items() if target_name == "待确认"]
        if pending:
            raise ImportError("以下素材仍待确认，未执行导入：" + "、".join(sorted(pending)))
        mapping = {
            "主参考图": [],
            "服装细节参考": [],
            "场景参考": [],
            "动作参考图": [],
            "动作参考视频": [],
        }
        for source_name, source_role in role_assignments.items():
            if source_role == "主参考图":
                formal_role = "主参考图"
            elif source_role.startswith("服装细节_"):
                formal_role = "服装细节参考"
                detail_role_by_source[source_name] = source_role
            elif source_role == "场景参考":
                formal_role = "场景参考"
            elif source_role == "动作参考图":
                formal_role = "动作参考图"
            else:
                formal_role = "动作参考视频"
            role_for_name[source_name] = formal_role
            mapping[formal_role].append(source_name)
        if len(mapping["主参考图"]) != 1:
            raise ImportError("必须且只导入一张主参考图")
        if not mapping["动作参考视频"]:
            raise ImportError("至少需要一段动作参考视频")

    # Some cameras/exporters leave a valid PNG payload with a .jpg filename.
    # Keep the source untouched, but publish the imported copy with a suffix
    # matching its actual image format so the normal asset validator remains
    # strict for all persisted project files.
    normalized_suffix_by_source: dict[str, str] = {}
    for name, role in role_for_name.items():
        if role not in {"主参考图", "服装细节参考", "场景参考", "动作参考图"}:
            continue
        path = source_by_name[name]
        if role == "场景参考" and detect_media_kind(path) != "image":
            continue
        try:
            snapshot = path.read_bytes()
        except OSError:
            raise ImportError(f"无法读取图片文件：{name}") from None
        canonical_suffix = _canonical_image_suffix(snapshot)
        candidates = [canonical_suffix, path.suffix.lower(), ".jpg", ".jpeg", ".png", ".webp"]
        first_error: AssetError | None = None
        for suffix in dict.fromkeys(candidates):
            try:
                _inspect_snapshot(path.with_suffix(suffix), role, snapshot)
                normalized_suffix_by_source[name] = suffix
                break
            except AssetError as error:
                if first_error is None:
                    first_error = error
        else:
            assert first_error is not None
            raise first_error
        target_name = target_name_by_source.get(name)
        if target_name is not None:
            target_name_by_source[name] = Path(target_name).with_suffix(
                normalized_suffix_by_source[name]
            ).name
    for name, role in role_for_name.items():
        if role in {"主参考图", "服装细节参考", "动作参考图"}:
            source_image = source_by_name[name]
            suffix = normalized_suffix_by_source.get(name, source_image.suffix.lower())
            if suffix == source_image.suffix.lower():
                _inspect_image(source_image, role)
            else:
                try:
                    _inspect_snapshot(
                        source_image.with_suffix(suffix), role, source_image.read_bytes()
                    )
                except OSError:
                    raise ImportError(f"无法读取图片文件：{name}") from None
        elif role == "场景参考":
            if detect_media_kind(source_by_name[name]) == "image":
                source_image = source_by_name[name]
                suffix = normalized_suffix_by_source.get(name, source_image.suffix.lower())
                if suffix == source_image.suffix.lower():
                    _inspect_image(source_image, "场景参考")
                else:
                    try:
                        _inspect_snapshot(
                            source_image.with_suffix(suffix),
                            "场景参考",
                            source_image.read_bytes(),
                        )
                    except OSError:
                        raise ImportError(f"无法读取图片文件：{name}") from None
            elif not _probe_video(source_by_name[name], video_probe):
                raise ImportError(f"场景参考视频无法打开或解码首帧：{name}")
        elif role == "动作参考图":
            _inspect_image(source_by_name[name], "动作参考图")
        elif not _probe_video(source_by_name[name], video_probe):
            raise ImportError(f"动作参考视频无法打开或解码首帧：{name}")

    materials_root = Path(paths.materials)
    target = materials_root / sku
    try:
        materials_root.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise ImportError("无法创建项目素材目录") from None
    _reject_reparse(materials_root)
    _reject_reparse(target)
    try:
        source_real = source.resolve(strict=True)
        materials_real = materials_root.resolve(strict=True)
        target_real = target.resolve(strict=False)
        source_is_direct_child_group = source_real.parent == target_real
        if (
            source_real == target_real
            or (
                (source_real in target_real.parents or target_real in source_real.parents)
                and not source_is_direct_child_group
            )
        ):
            raise ImportError("素材来源与导入目标重叠，拒绝复制")
        target.resolve(strict=False).relative_to(materials_real)
    except ImportError:
        raise
    except (OSError, ValueError):
        raise ImportError("导入目标超出项目素材目录") from None

    if target.exists() and not target.is_dir():
        raise ImportError("现有款号素材路径不是普通目录")
    existing = _existing_standard_files(target, sku) if target.exists() else {}
    existing_details: dict[tuple[str, str], str] = {}
    for filename, role in existing.items():
        if role.startswith("服装细节_"):
            existing_details[(role, _sha256(target / filename))] = filename

    entries: list[dict[str, object]] = []
    target_for_source: dict[str, str] = {}
    reused: set[str] = set()
    staged_names: set[str] = set()
    try:
        staging = Path(tempfile.mkdtemp(prefix=f".kling-import-{sku}-", dir=materials_root))
    except OSError:
        raise ImportError("无法创建本次导入临时目录") from None
    created_files: list[tuple[Path, tuple[int, int]]] = []
    created_target_dir = False
    old_request: bytes | None = None
    old_request_identity: tuple[int, int] | None = None
    request_published_identity: tuple[int, int] | None = None
    group_path: Path | None = None
    group_identity: tuple[int, int] | None = None
    try:
        for path in source_files:
            role_key = detail_role_by_source.get(path.name)
            if role_key is None:
                try:
                    role_key = _role_key_from_stem(path.stem)
                except ImportError:
                    role_key = ""
            role = role_for_name[path.name]
            digest = _sha256(path)
            reused_name = (
                existing_details.get((role_key, digest))
                if role == "服装细节参考"
                else None
            )
            if reused_name:
                target_name = reused_name
                reused.add(target_name)
            else:
                target_name = target_name_by_source[path.name]
                if (target / target_name).exists():
                    raise ImportError(f"目标素材已存在且不能覆盖：{target_name}")
                if target_name in staged_names:
                    raise ImportError(f"本次导入目标文件名重复：{target_name}")
                staged_names.add(target_name)
                stage_path = staging / target_name
                shutil.copyfile(path, stage_path)
                if _sha256(stage_path) != digest:
                    raise ImportError(f"素材复制后哈希不一致：{path.name}")
            target_for_source[path.name] = target_name
            entries.append(
                {
                    "source_name": path.name,
                    "role": role,
                    "target_name": target_name,
                    "sha256": digest,
                    "reused": bool(reused_name),
                }
            )

        all_names = set(existing)
        all_names.update(target_for_source.values())
        all_roles: dict[str, list[str]] = {
            "主参考图": [],
            "服装细节参考": [],
            "场景参考": [],
            "动作参考图": [],
            "动作参考视频": [],
        }
        for name in sorted(all_names, key=lambda item: (item.casefold(), item)):
            role = existing.get(name)
            if role is None:
                role = next(
                    entry["role"]
                    for entry in entries
                    if entry["target_name"] == name
                )
            if role.startswith("服装细节_"):
                role = "服装细节参考"
            if role in all_roles:
                all_roles[role].append(name)
        request_text = _render_request_text(all_roles)
        request_path = target / "需求.txt"
        review_path = target / "审核标准.txt"
        if request_path.exists():
            _reject_reparse(request_path)
            old_request_identity = _file_identity(request_path)
            try:
                old_request = request_path.read_bytes()
            except OSError:
                raise ImportError("无法读取现有需求.txt") from None
            if not old_request.decode("utf-8-sig", errors="replace").startswith(
                "# 由可灵工作流自动生成；请勿手工改写素材映射。"
            ):
                raise ImportError("现有需求.txt 不是可安全更新的自动映射文件")
        if review_path.exists():
            _reject_reparse(review_path)
            if not review_path.is_file():
                raise ImportError("审核标准.txt 不是普通文件")

        # Publish only after every source and staged file passed validation.
        if not target.exists():
            target.mkdir()
            created_target_dir = True
        for name in sorted(staged_names):
            staged_path = staging / name
            final_path = target / name
            _reject_reparse(final_path)
            try:
                os.link(staged_path, final_path)
            except FileExistsError:
                raise ImportError(f"导入发布时目标素材发生冲突：{name}") from None
            except OSError:
                # Exclusive creation remains safe on filesystems without hard links.
                try:
                    with staged_path.open("rb") as source_stream, final_path.open("xb") as target_stream:
                        created_files.append((final_path, _file_identity(final_path)))
                        shutil.copyfileobj(source_stream, target_stream)
                        target_stream.flush()
                        os.fsync(target_stream.fileno())
                except OSError:
                    raise ImportError(f"无法排他发布素材：{name}") from None
            if not any(item[0] == final_path for item in created_files):
                created_files.append((final_path, _file_identity(final_path)))

        request_temp = staging / "需求.txt"
        request_temp.write_text(request_text, encoding="utf-8", newline="\n")
        if request_path.exists():
            current_identity = _file_identity(request_path)
            if current_identity != old_request_identity or request_path.read_bytes() != old_request:
                raise ImportError("需求.txt 在导入期间发生变化，已停止")
            os.replace(request_temp, request_path)
            request_published_identity = _file_identity(request_path)
        else:
            try:
                os.link(request_temp, request_path)
            except FileExistsError:
                raise ImportError("需求.txt 在导入期间发生冲突，已停止") from None
            except OSError:
                _publish_exclusive_copy(request_temp, request_path, created_files, "需求.txt")
            request_published_identity = _file_identity(request_path)
            if not any(item[0] == request_path for item in created_files):
                created_files.append((request_path, request_published_identity))

        if not review_path.exists():
            review_temp = staging / "审核标准.txt"
            review_temp.write_text(_default_review_standard(), encoding="utf-8", newline="\n")
            try:
                os.link(review_temp, review_path)
            except FileExistsError:
                raise ImportError("审核标准.txt 在导入期间发生冲突，已停止") from None
            except OSError:
                _publish_exclusive_copy(review_temp, review_path, created_files, "审核标准.txt")
            if not any(item[0] == review_path for item in created_files):
                created_files.append((review_path, _file_identity(review_path)))

        history_sku = paths.history / sku
        history_sku.mkdir(parents=True, exist_ok=True)
        _reject_reparse(history_sku)
        group_number = _next_group_number(history_sku)
        group_id = f"group-{group_number:04d}"
        group_path = history_sku / f"{group_id}.json"
        group_record = {
            "schema_version": 1,
            "group_id": group_id,
            "sku": sku,
            "source_folder": str(source),
            "files": entries,
            "reference_files": {
                role: [target_for_source[name] for name in source_mapping]
                for role, source_mapping in mapping.items()
            },
            "video_version": None,
            "imported_at": datetime.now(timezone.utc).isoformat(),
            "status": "imported",
        }
        payload = (json.dumps(group_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        try:
            with group_path.open("xb") as stream:
                group_identity = _file_identity(group_path)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ImportError("参考组编号发生并发冲突，请重新检查导入状态") from None
        except OSError:
            raise ImportError("无法保存参考组历史清单") from None
        return {
            "ok": True,
            "sku": sku,
            "group_id": group_id,
            "group_manifest": str(group_path),
            "materials_folder": str(target),
            "reused_files": sorted(reused),
            "mapping": {
                role: [target_for_source[name] for name in filenames]
                for role, filenames in mapping.items()
            },
            "name_mapping": dict(target_for_source),
        }
    except Exception:
        for path, identity in reversed(created_files):
            _unlink_owned(path, identity)
        if request_published_identity is not None and old_request is not None:
            if _same_identity(request_path, request_published_identity):
                restore = staging / "需求-恢复.txt"
                restore.write_bytes(old_request)
                os.replace(restore, request_path)
        if group_path is not None and group_identity is not None:
            _unlink_owned(group_path, group_identity)
        if created_target_dir:
            try:
                target.rmdir()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def import_and_prepare(
    source_folder: str | os.PathLike[str],
    sku: str,
    workflow,
    *,
    role_assignments: Mapping[str, str] | None = None,
    video_probe=None,
) -> dict[str, object]:
    """Import a role-reviewed folder, normalize names, then analyze only that group."""
    paths = getattr(workflow, "paths", None)
    prepare = getattr(workflow, "prepare", None)
    if not isinstance(paths, ProjectPaths) or not callable(prepare):
        raise ImportError("离线分析工作流未正确配置")
    imported = import_standard_folder(
        source_folder,
        sku,
        paths,
        role_assignments=role_assignments,
        video_probe=video_probe,
    )
    manifest_path = Path(str(imported["group_manifest"]))
    prepared = prepare_existing_reference_group(manifest_path, sku, workflow)
    return {
        **imported,
        **prepared,
        "status": "analysis_prepared",
    }


def prepare_existing_reference_group(
    manifest_path: str | os.PathLike[str],
    sku: str,
    workflow,
) -> dict[str, object]:
    """Create a fresh offline version for one already-imported reference group.

    This deliberately reuses the immutable group manifest instead of importing
    the source folder again, so a model/policy change cannot create duplicate
    group IDs for the same numbered source directory.
    """
    paths = getattr(workflow, "paths", None)
    prepare = getattr(workflow, "prepare", None)
    if not isinstance(paths, ProjectPaths) or not callable(prepare):
        raise ImportError("离线分析工作流未正确配置")
    try:
        _validate_sku_segment(sku)
    except (TypeError, ValueError):
        raise ImportError("款号格式无效") from None
    path = Path(manifest_path)
    try:
        _reject_reparse(paths.history)
        _reject_reparse(paths.history / sku)
        _reject_reparse(path)
        history_sku = (paths.history / sku).resolve(strict=True)
        resolved = path.resolve(strict=True)
        if resolved.parent != history_sku or not re.fullmatch(r"group-\d{4,}\.json", resolved.name):
            raise ImportError("参考组清单路径无效")
        metadata = resolved.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ImportError("参考组清单文件无效")
        original = resolved.read_bytes()
        group_record = json.loads(original.decode("utf-8"))
    except ImportError:
        raise
    except (OSError, UnicodeError, ValueError):
        raise ImportError("无法读取参考组清单") from None
    group_id = group_record.get("group_id") if isinstance(group_record, dict) else None
    if (
        not isinstance(group_record, dict)
        or group_record.get("sku") != sku
        or not isinstance(group_id, str)
        or group_id != resolved.stem
        or not re.fullmatch(r"group-\d{4,}", group_id)
        or not isinstance(group_record.get("source_folder"), str)
        or not isinstance(group_record.get("reference_files"), dict)
    ):
        raise ImportError("参考组清单结构无效")

    run_dir = Path(prepare(sku, reference_group=resolved))
    version = run_dir.name
    group_record["video_version"] = version
    versions = group_record.get("video_versions")
    if not isinstance(versions, list):
        versions = []
    versions = [item for item in versions if not isinstance(item, Mapping) or item.get("version") != version]
    versions.append({"version": version, "group_id": group_record.get("group_id")})
    group_record["video_versions"] = versions
    group_record["status"] = "analysis_prepared"
    payload = (json.dumps(group_record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    try:
        if resolved.read_bytes() != original:
            raise ImportError("参考组清单在分析期间发生变化，已停止")
        descriptor, temp_name = tempfile.mkstemp(prefix=".group-version-", suffix=".tmp", dir=resolved.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, resolved)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    except ImportError:
        raise
    except OSError:
        raise ImportError("无法将参考组关联到离线分析版本") from None

    metadata = workflow._read_metadata(run_dir)
    metadata["reference_group_sha256"] = hashlib.sha256(payload).hexdigest()
    metadata["reference_group_version"] = version
    workflow._write_metadata(run_dir, metadata)
    return {
        "group_id": group_id,
        "group_manifest": str(resolved),
        "run_dir": str(run_dir),
        "version": version,
        "status": "analysis_prepared",
    }


def _probe_video(path: Path, probe) -> bool:
    if probe is not None:
        try:
            return bool(probe(path))
        except Exception:
            return False
    try:
        import cv2

        capture = cv2.VideoCapture(str(path))
        try:
            if not capture.isOpened():
                return False
            ok, frame = capture.read()
            return bool(ok and frame is not None and getattr(frame, "size", 0) > 0)
        finally:
            capture.release()
    except Exception:
        return False


def _role_key_from_stem(stem: str) -> str:
    match = _STANDARD_NAME.fullmatch(stem)
    if not match:
        raise ImportError(f"文件名不符合标准角色格式：{stem}")
    return _role_from_name(match.group("role"))


def _existing_standard_files(target: Path, sku: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in target.iterdir():
        _reject_reparse(path)
        if not path.is_file():
            continue
        match = _STANDARD_NAME.fullmatch(path.stem)
        if match and match.group("sku") == sku and detect_media_kind(path) in {"image", "video"}:
            result[path.name] = _role_from_name(match.group("role"), path.suffix, path)
    return result


def _render_request_text(mapping: Mapping[str, list[str]]) -> str:
    def render(role: str) -> str:
        values = mapping[role]
        return "、".join(values) if values else "无"

    return (
        "# 由可灵工作流自动生成；请勿手工改写素材映射。\n"
        "素材映射按文件名和当前款号目录内的参考组生成。\n"
        f"主参考图：{render('主参考图')}\n"
        f"服装细节参考：{render('服装细节参考')}\n"
        f"场景参考：{render('场景参考')}\n"
        f"动作参考图：{render('动作参考图')}\n"
        f"动作参考视频：{render('动作参考视频')}\n"
    )


def _default_review_standard() -> str:
    return (
        "审核标准\n"
        "A. 人物与服装：核对脸部、发型、体态、服装款式颜色、面料纹理和细节是否一致。\n"
        "B. 场景与光线：核对背景空间、主体位置、主要物件和光线连续性。\n"
        "C. 动作复刻：逐时间码对照本参考组动作，检查身体朝向、转身、左右手、持物换手、步伐、视线、整理衣服和插兜。\n"
        "D. 镜头：检查景别、构图、机位、运镜和真实切镜；连续参考不得擅自切镜。\n"
        "E. 技术质量：检查时长、比例、清晰度、黑屏、冻结、畸变和画面闪烁。\n"
        "离线审核和人工终审是不同状态；只有明确审核通过后才能归档。\n"
    )


def _next_group_number(history_sku: Path) -> int:
    maximum = 0
    for path in history_sku.glob("group-*.json"):
        match = re.fullmatch(r"group-(\d{4,})\.json", path.name)
        if match:
            maximum = max(maximum, int(match.group(1)))
    return maximum + 1


def _file_identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ImportError(f"导入输出不是普通文件：{path.name}")
    return (int(metadata.st_dev), int(metadata.st_ino))


def _same_identity(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (int(metadata.st_dev), int(metadata.st_ino)) == identity


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    if _same_identity(path, identity):
        try:
            path.unlink()
        except OSError:
            pass


def _publish_exclusive_copy(
    source: Path,
    target: Path,
    created: list[tuple[Path, tuple[int, int]]],
    label: str,
) -> None:
    try:
        with source.open("rb") as source_stream, target.open("xb") as target_stream:
            created.append((target, _file_identity(target)))
            shutil.copyfileobj(source_stream, target_stream)
            target_stream.flush()
            os.fsync(target_stream.fileno())
    except OSError:
        raise ImportError(f"无法排他创建{label}") from None


def _role_from_name(
    value: str, suffix: str | None = None, path: Path | None = None
) -> str:
    if value == "动作参考":
        if path is not None:
            detected = detect_media_kind(path)
            if detected == "image":
                return "动作参考图"
            if detected == "video":
                return "动作参考视频"
        if suffix and suffix.lower() in _IMAGE_SUFFIXES:
            return "动作参考图"
        return "动作参考视频"
    return value


def _canonical_image_suffix(snapshot: bytes) -> str:
    try:
        with Image.open(io.BytesIO(snapshot)) as image:
            image_format = (image.format or "").upper()
    except Exception:
        return ".jpg"
    return {
        "JPEG": ".jpg",
        "MPO": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "GIF": ".gif",
        "TIFF": ".tif",
        "BMP": ".bmp",
    }.get(image_format, ".jpg")


def _validate_source_folder(value: str | os.PathLike[str]) -> Path:
    try:
        raw = os.fspath(value)
        windows = PureWindowsPath(raw)
        if not isinstance(raw, str) or not raw.strip() or windows.drive.startswith("\\\\"):
            raise ImportError("只接受普通本地素材文件夹，不接受网络路径")
        source = Path(raw)
        if not source.is_absolute() or ".." in source.parts:
            raise ImportError("素材文件夹必须是绝对本地路径且不能包含父目录跳转")
        if os.name == "nt":
            try:
                if ctypes.windll.kernel32.GetDriveTypeW(str(source.anchor)) == 4:
                    raise ImportError("不接受映射网络驱动器中的素材文件夹")
            except ImportError:
                raise
            except Exception:
                raise ImportError("无法确认素材文件夹是否位于本地磁盘") from None
        _reject_reparse(source)
        resolved = source.resolve(strict=True)
        if not resolved.is_dir():
            raise ImportError("素材来源必须是普通文件夹")
        return resolved
    except ImportError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError):
        raise ImportError("无法安全读取素材文件夹") from None


def _ordinary_source_files(source: Path) -> list[Path]:
    try:
        children = list(source.iterdir())
    except OSError:
        raise ImportError("无法读取素材文件夹") from None
    if not children:
        raise ImportError("素材文件夹为空")
    files: list[Path] = []
    unsupported: list[str] = []
    for path in children:
        _reject_reparse(path)
        try:
            metadata = path.lstat()
        except OSError:
            raise ImportError("无法检查素材文件夹中的文件") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise ImportError(f"素材文件夹只接受普通文件：{path.name}")
        if path.name.startswith((".", "~$")) or path.suffix.lower() in {".tmp", ".temp", ".part"}:
            raise ImportError(f"发现隐藏或临时文件：{path.name}")
        if path.name in _ALLOWED_SOURCE_TEXT_FILES:
            continue
        if path.suffix.lower() in _ALL_SUFFIXES or detect_media_kind(path) in {"image", "video"}:
            files.append(path)
        else:
            unsupported.append(path.name)
    if unsupported:
        raise ImportError("发现不支持的文件：" + "、".join(sorted(unsupported)))
    return sorted(files, key=lambda path: (path.name.casefold(), path.name))


def _reject_reparse(path: Path) -> None:
    current = Path(path)
    for component in (current, *current.parents):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise ImportError("无法检查路径是否包含链接或重解析点") from None
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ImportError("素材路径包含链接或重解析点，已拒绝")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        raise ImportError(f"无法计算素材哈希：{path.name}") from None
    return digest.hexdigest()
