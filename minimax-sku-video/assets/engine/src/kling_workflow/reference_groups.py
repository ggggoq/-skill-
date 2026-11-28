"""Safe selection of numbered reference folders for one SKU."""

from __future__ import annotations

import re
import stat
import json
from pathlib import Path
from typing import Any

from .task_store import _validate_sku_segment


_NUMBERED_GROUP = re.compile(r"^(?P<sku>.+?)\s+(?P<number>[0-9]+)$")
_NUMERIC_GROUP = re.compile(r"^(?P<number>[0-9]+)$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    return bool(getattr(metadata, "st_file_attributes", 0) & _REPARSE_ATTRIBUTE)


def _safe_directory(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not _is_reparse(path)


def _requested_number(value: str | int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("参考组编号必须是正整数")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        number = int(value.strip())
    else:
        raise ValueError("参考组编号必须是正整数")
    if number < 1:
        raise ValueError("参考组编号必须是正整数")
    return number


def select_numbered_reference_groups(
    materials_root: str | Path,
    sku: str,
    requested_number: str | int | None = None,
) -> dict[str, Any]:
    """Select direct ``<sku> <number>`` or numeric children for one SKU.

    The function is intentionally read-only.  A missing explicit child is
    returned as ``missing`` so callers can pause for confirmation instead of
    silently falling back to the parent or another group.
    """

    _validate_sku_segment(sku)
    number = _requested_number(requested_number)
    root = Path(materials_root)
    parent = root / sku
    parent_display = str(parent.resolve(strict=False))
    result: dict[str, Any] = {
        "mode": "one" if number is not None else "all",
        "sku": sku,
        "parent": parent_display,
        "groups": [],
        "missing": None,
    }
    if parent.is_symlink() or (parent.exists() and _is_reparse(parent)):
        result["error"] = "款号参考目录包含链接或重解析点"
        return result
    if not parent.is_dir():
        if number is not None:
            result["missing"] = f"{sku} {number}"
        return result

    discovered: list[dict[str, Any]] = []
    seen_numbers: set[int] = set()
    for child in sorted(parent.iterdir(), key=lambda path: (path.name.casefold(), path.name)):
        match = _NUMBERED_GROUP.fullmatch(child.name)
        numeric_match = _NUMERIC_GROUP.fullmatch(child.name)
        is_candidate = (
            (match is not None and match.group("sku") == sku)
            or numeric_match is not None
        )
        if is_candidate and (
            child.is_symlink() or _is_reparse(child) or not child.is_dir()
        ):
            result["error"] = f"参考组目录包含链接、重解析点或非目录：{child.name}"
            return result
        if not _safe_directory(child):
            continue
        if not is_candidate:
            continue
        child_number = int(
            match.group("number") if match is not None else numeric_match.group("number")
        )
        if child_number in seen_numbers:
            result["error"] = f"同一款号存在重复参考组编号：{child_number}"
            return result
        seen_numbers.add(child_number)
        discovered.append(
            {
                "number": child_number,
                "name": child.name,
                "path": str(child.resolve(strict=True)),
            }
        )

    discovered.sort(key=lambda item: (item["number"], item["name"].casefold(), item["name"]))
    if number is None:
        result["groups"] = discovered
        return result

    selected = [item for item in discovered if item["number"] == number]
    if not selected:
        result["missing"] = f"{sku} {number}"
        return result
    result["groups"] = selected
    return result


def resolve_reference_group_order(
    materials_root: str | Path,
    history_root: str | Path,
    sku: str,
) -> dict[str, Any]:
    """Bind every numbered source folder to its immutable imported group ID."""

    selection = select_numbered_reference_groups(materials_root, sku)
    result: dict[str, Any] = {
        "sku": sku,
        "group_numbers": [],
        "group_order": [],
        "conflicts": [],
        "error": selection.get("error"),
    }
    groups = selection.get("groups")
    if result["error"] or not isinstance(groups, list):
        result["error"] = result["error"] or "参考组顺序结构无效"
        return result
    result["group_numbers"] = [int(item["number"]) for item in groups]
    history_sku = Path(history_root) / sku
    if not _safe_directory(history_sku):
        result["conflicts"] = [str(item["name"]) for item in groups]
        return result

    manifests: list[dict[str, Any]] = []
    for manifest_path in sorted(history_sku.glob("group-*.json"), key=lambda p: p.name):
        if not manifest_path.is_file() or _is_reparse(manifest_path):
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_path = Path(payload["source_folder"]).resolve(strict=True)
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            continue
        group_id = payload.get("group_id") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or payload.get("sku") != sku
            or not isinstance(group_id, str)
            or not re.fullmatch(r"group-\d{4,}", group_id)
        ):
            continue
        manifests.append({"group_id": group_id, "source_path": source_path})

    seen_ids: set[str] = set()
    for group in groups:
        try:
            selected_path = Path(str(group["path"])).resolve(strict=True)
        except (OSError, ValueError, KeyError, TypeError):
            result["conflicts"].append(str(group.get("name", "")))
            continue
        matches = [item for item in manifests if item["source_path"] == selected_path]
        if len(matches) != 1 or matches[0]["group_id"] in seen_ids:
            result["conflicts"].append(str(group.get("name", "")))
            continue
        group_id = matches[0]["group_id"]
        seen_ids.add(group_id)
        result["group_order"].append(
            {
                "number": int(group["number"]),
                "name": str(group["name"]),
                "group_id": group_id,
            }
        )
    return result


def find_prepared_reference_group_runs(
    materials_root: str | Path,
    history_root: str | Path,
    sku: str,
    requested_number: str | int | None = None,
) -> dict[str, Any]:
    """Resolve selected numbered groups to exactly one waiting local run each.

    This is a local, read-only preflight.  It deliberately matches the source
    folder recorded in the group manifest and verifies the corresponding
    version sidecar before a caller is allowed to construct a paid client.
    Missing or ambiguous preparation never falls back to another group.
    """

    selection = select_numbered_reference_groups(
        materials_root, sku, requested_number=requested_number
    )
    result: dict[str, Any] = {
        **selection,
        "ready": False,
        "candidates": [],
        "missing_preparation": [],
        "conflicts": [],
        "group_numbers": [],
        "group_order": [],
        "order_conflicts": [],
        "legacy": False,
    }
    if selection.get("error") or selection.get("missing"):
        return result
    groups = selection.get("groups")
    if not isinstance(groups, list):
        result["error"] = "参考组选择结果结构无效"
        return result
    if not groups:
        # No numbered children means the caller may use the legacy flat-SKU
        # path.  It must not be interpreted as an empty successful batch.
        result["legacy"] = True
        return result

    order_selection = (
        selection
        if requested_number is None
        else select_numbered_reference_groups(materials_root, sku)
    )
    order_groups = order_selection.get("groups")
    if order_selection.get("error") or not isinstance(order_groups, list):
        result["error"] = "无法安全解析全部参考组顺序"
        return result
    result["group_numbers"] = [
        int(item["number"])
        for item in order_groups
        if isinstance(item, dict) and isinstance(item.get("number"), int)
    ]

    history_sku = Path(history_root) / sku
    if not _safe_directory(history_sku):
        result["missing_preparation"] = [str(item["name"]) for item in groups]
        return result

    manifests: list[tuple[Path, dict[str, Any]]] = []
    for manifest_path in sorted(history_sku.glob("group-*.json"), key=lambda p: p.name):
        if not manifest_path.is_file() or _is_reparse(manifest_path):
            continue
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("sku") != sku:
            continue
        source_folder = payload.get("source_folder")
        if not isinstance(source_folder, str):
            continue
        try:
            source_path = Path(source_folder).resolve(strict=True)
        except (OSError, ValueError):
            continue
        manifests.append((manifest_path, {**payload, "_source_path": source_path}))

    for group in order_groups:
        try:
            selected_path = Path(str(group["path"])).resolve(strict=True)
        except (OSError, ValueError, KeyError, TypeError):
            result["order_conflicts"].append(str(group.get("name", "")))
            continue
        matches = [
            item for item in manifests if item[1].get("_source_path") == selected_path
        ]
        if len(matches) != 1:
            result["order_conflicts"].append(str(group.get("name", "")))
            continue
        group_id = matches[0][1].get("group_id")
        if not isinstance(group_id, str) or not re.fullmatch(r"group-\d{4,}", group_id):
            result["order_conflicts"].append(str(group.get("name", "")))
            continue
        result["group_order"].append(
            {
                "number": int(group["number"]),
                "name": str(group["name"]),
                "group_id": group_id,
            }
        )

    candidates: list[dict[str, Any]] = []
    for group in groups:
        try:
            selected_path = Path(str(group["path"])).resolve(strict=True)
        except (OSError, ValueError, KeyError, TypeError):
            result["missing_preparation"].append(str(group.get("name", "")))
            continue
        matches = [
            item for item in manifests if item[1].get("_source_path") == selected_path
        ]
        if len(matches) != 1:
            result["missing_preparation"].append(str(group.get("name", "")))
            continue
        manifest_path, manifest = matches[0]
        group_id = manifest.get("group_id")
        if not isinstance(group_id, str) or not re.fullmatch(r"group-\d{4,}", group_id):
            result["missing_preparation"].append(str(group.get("name", "")))
            continue
        versions: list[str] = []
        raw_versions = manifest.get("video_versions")
        if isinstance(raw_versions, list):
            for item in raw_versions:
                if isinstance(item, dict) and isinstance(item.get("version"), str):
                    if re.fullmatch(r"v\d{3,}", item["version"]):
                        versions.append(item["version"])
        raw_version = manifest.get("video_version")
        if isinstance(raw_version, str) and re.fullmatch(r"v\d{3,}", raw_version):
            versions.append(raw_version)
        waiting: list[dict[str, Any]] = []
        for version in sorted(set(versions), key=lambda item: int(item[1:]), reverse=True):
            record_path = history_sku / f"{version}.json"
            metadata_path = history_sku / ".workflow" / version / "workflow.json"
            try:
                record = json.loads(record_path.read_text(encoding="utf-8"))
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            prompt_sha = record.get("prompt_sha256") if isinstance(record, dict) else None
            if (
                not isinstance(record, dict)
                or record.get("sku") != sku
                or record.get("status") != "等待用户确认"
                or not isinstance(prompt_sha, str)
                or not re.fullmatch(r"[0-9a-fA-F]{64}", prompt_sha)
                or not isinstance(metadata, dict)
                or metadata.get("sku") != sku
                or metadata.get("reference_group_id") != group_id
                or metadata.get("prompt_sha256") != prompt_sha
            ):
                continue
            waiting.append(
                {
                    "number": int(group["number"]),
                    "name": str(group["name"]),
                    "path": str(selected_path),
                    "group_id": group_id,
                    "manifest": str(manifest_path.resolve()),
                    "version": version,
                    "run_dir": str((history_sku / ".workflow" / version).resolve()),
                    "prompt_sha": prompt_sha,
                }
            )
        if len(waiting) == 1:
            candidates.append(waiting[0])
        elif len(waiting) == 0:
            result["missing_preparation"].append(str(group.get("name", "")))
        else:
            result["conflicts"].append(str(group.get("name", "")))

    candidates.sort(key=lambda item: (item["number"], item["name"]))
    result["candidates"] = candidates
    result["ready"] = not result["missing_preparation"] and not result["conflicts"] and len(candidates) == len(groups)
    return result

