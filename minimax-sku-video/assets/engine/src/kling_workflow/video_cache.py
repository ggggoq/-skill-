from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from PIL import Image


_ANALYSIS_FIELDS = frozenset(
    {
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
        "absolute_path",
        "file_size_bytes",
        "mtime_ns",
        "aspect_ratio",
        "decode_status",
        "key_frames",
        "action_timeline",
        "shot_table",
        "camera_angle",
        "person_orientation",
        "transitions",
        "final_conclusion",
        "semantic_analysis_status",
        "semantic_review",
    }
)


@dataclass(frozen=True)
class CacheLookup:
    hit: bool
    record: dict[str, object] | None = None

    @property
    def semantic_hit(self) -> bool:
        return self.hit and isinstance(self.record, Mapping) and self.record.get("semantic_analysis_status") == "semantic_complete"


class VideoAnalysisCache:
    """Content-addressed local cache for reusable video analysis evidence."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def load(
        self,
        video_path: str | Path,
        analysis_version: str,
        decode_config: Mapping[str, object],
    ) -> CacheLookup:
        if not isinstance(analysis_version, str) or not analysis_version.strip():
            return CacheLookup(False)
        source = Path(video_path)
        try:
            digest = _sha256_file(source)
            key = _cache_key(digest, analysis_version, decode_config)
            entry_path = self.root / f"{key}.json"
            metadata = entry_path.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                return CacheLookup(False)
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            if (
                not isinstance(entry, dict)
                or entry.get("schema_version") != 1
                or entry.get("source_sha256") != digest
                or entry.get("analysis_version") != analysis_version
                or entry.get("decode_config") != _normalize_config(decode_config)
            ):
                return CacheLookup(False)
            record = entry.get("record")
            if not _is_complete_record(record, digest):
                return CacheLookup(False)
            sheet = Path(str(record["contact_sheet"]))
            sheet_metadata = sheet.lstat()
            if not stat.S_ISREG(sheet_metadata.st_mode):
                return CacheLookup(False)
            try:
                sheet.resolve(strict=True).relative_to(self.root.resolve(strict=True))
                with Image.open(sheet) as image:
                    image.verify()
            except (OSError, RuntimeError, ValueError):
                return CacheLookup(False)
            bound = dict(record)
            source_stat = source.stat()
            bound.update(
                {
                    "path": str(source.resolve(strict=True)),
                    "absolute_path": str(source.resolve(strict=True)),
                    "file_size_bytes": int(source_stat.st_size),
                    "mtime_ns": int(source_stat.st_mtime_ns),
                }
            )
            return CacheLookup(True, bound)
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return CacheLookup(False)

    def store(
        self,
        record: object,
        analysis_version: str,
        decode_config: Mapping[str, object],
    ) -> Path:
        value = record.to_dict() if callable(getattr(record, "to_dict", None)) else record
        if not isinstance(value, Mapping):
            raise ValueError("video analysis cache record must be a mapping")
        normalized = dict(value)
        source_path = Path(str(normalized.get("path", "")))
        digest = _sha256_file(source_path)
        if not _is_complete_record(normalized, digest):
            raise ValueError("video analysis cache record is incomplete or stale")
        source_stat = source_path.stat()
        if (
            normalized.get("absolute_path") != str(source_path.resolve(strict=True))
            or normalized.get("file_size_bytes") != source_stat.st_size
            or normalized.get("mtime_ns") != source_stat.st_mtime_ns
        ):
            raise ValueError("video analysis cache source identity metadata is stale")
        if not isinstance(analysis_version, str) or not analysis_version.strip():
            raise ValueError("analysis_version must be non-empty")
        config = _normalize_config(decode_config)
        key = _cache_key(digest, analysis_version, config)
        source_sheet = Path(str(normalized["contact_sheet"]))
        sheet_bytes = source_sheet.read_bytes()
        if not sheet_bytes:
            raise ValueError("video analysis cache contact sheet is empty")

        self.root.mkdir(parents=True, exist_ok=True)
        sheet_path = self.root / f"{key}.png"
        entry_path = self.root / f"{key}.json"
        if not sheet_path.exists():
            _write_exclusive(sheet_path, sheet_bytes)
        cached_record = dict(normalized)
        cached_record["contact_sheet"] = str(sheet_path)
        entry = {
            "schema_version": 1,
            "source_sha256": digest,
            "analysis_version": analysis_version,
            "decode_config": config,
            "record": cached_record,
        }
        payload = json.dumps(
            entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        if not entry_path.exists():
            _write_exclusive(entry_path, payload)
        return entry_path

    def promote_semantic_review(
        self,
        video_path: str | Path,
        analysis_version: str,
        decode_config: Mapping[str, object],
        semantic_review: Mapping[str, object],
    ) -> bool:
        """Promote a matching decode-only entry after a real structured review."""
        temp_path: Path | None = None
        try:
            from .video_analysis import SEMANTIC_FIELDS

            source = Path(video_path)
            digest = _sha256_file(source)
            config = _normalize_config(decode_config)
            key = _cache_key(digest, analysis_version, config)
            entry_path = self.root / f"{key}.json"
            entry_metadata = entry_path.lstat()
            if not stat.S_ISREG(entry_metadata.st_mode):
                return False
            entry = json.loads(entry_path.read_text(encoding="utf-8"))
            if (
                not isinstance(entry, dict)
                or entry.get("schema_version") != 1
                or entry.get("source_sha256") != digest
                or entry.get("analysis_version") != analysis_version
                or entry.get("decode_config") != config
            ):
                return False
            record = entry.get("record")
            if (
                not _is_complete_record(record, digest)
                or record.get("semantic_analysis_status") != "semantic_pending"
                or not _valid_semantic_review(semantic_review, SEMANTIC_FIELDS)
            ):
                return False
            source_stat = source.stat()
            resolved_path = str(source.resolve(strict=True))
            if record.get("file_size_bytes") != source_stat.st_size:
                return False
            timeline = _timeline_from_review(str(semantic_review["动作时间轴"]))
            shot_table = (
                _shot_table_from_timeline(timeline, float(record["duration_seconds"]))
                if timeline is not None
                else None
            )
            if timeline is None or shot_table is None:
                return False
            upgraded = dict(record)
            upgraded.update(
                {
                    "path": resolved_path,
                    "absolute_path": resolved_path,
                    "file_size_bytes": int(source_stat.st_size),
                    "mtime_ns": int(source_stat.st_mtime_ns),
                    "semantic_analysis_status": "semantic_complete",
                    "semantic_review": dict(semantic_review),
                    "action_timeline": timeline,
                    "shot_table": shot_table,
                    "camera_angle": _join_review(
                        "机位高度", "景别", "构图", "运镜", review=semantic_review
                    ),
                    "person_orientation": _join_review(
                        "身体朝向", "转身方向与角度", review=semantic_review
                    ),
                    "transitions": str(semantic_review["镜头连续性与真实切镜"]),
                    "final_conclusion": str(semantic_review["未知与冲突"]),
                }
            )
            if not _is_complete_record(upgraded, digest):
                return False
            entry["record"] = upgraded
            payload = json.dumps(
                entry, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=f".{key}.", suffix=".tmp", dir=self.root, delete=False
            ) as stream:
                temp_path = Path(stream.name)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, entry_path)
            temp_path = None
            return True
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except OSError:
                    pass
            return False


def _normalize_config(config: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(config, Mapping):
        raise ValueError("decode_config must be a mapping")
    normalized = json.loads(
        json.dumps(config, ensure_ascii=False, sort_keys=True, allow_nan=False)
    )
    if not isinstance(normalized, dict):
        raise ValueError("decode_config must be a mapping")
    return normalized


def _cache_key(
    digest: str, analysis_version: str, decode_config: Mapping[str, object]
) -> str:
    basis = json.dumps(
        [digest, analysis_version, _normalize_config(decode_config)],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(basis).hexdigest()


def _is_complete_record(record: object, expected_digest: str) -> bool:
    if not isinstance(record, Mapping) or not _ANALYSIS_FIELDS <= set(record):
        return False
    if record.get("status") != "success" or record.get("sha256") != expected_digest:
        return False
    numeric_fields = ("frame_count", "fps", "width", "height", "duration_seconds")
    if any(
        isinstance(record.get(field), bool)
        or not isinstance(record.get(field), (int, float))
        or not math.isfinite(float(record.get(field)))
        or record.get(field) <= 0
        for field in numeric_fields
    ):
        return False
    if any(
        not isinstance(record.get(field), int) or isinstance(record.get(field), bool)
        for field in ("frame_count", "width", "height")
    ):
        return False
    if not isinstance(record.get("sample_timestamps"), list) or not isinstance(
        record.get("scene_boundaries"), list
    ):
        return False
    if not isinstance(record.get("backend_name"), str) or not record["backend_name"].strip():
        return False
    absolute_path = record.get("absolute_path")
    if not isinstance(absolute_path, str) or not Path(absolute_path).is_absolute():
        return False
    for field in ("file_size_bytes", "mtime_ns"):
        value = record.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return False
    aspect_ratio = record.get("aspect_ratio")
    if (
        isinstance(aspect_ratio, bool)
        or not isinstance(aspect_ratio, (int, float))
        or not math.isfinite(float(aspect_ratio))
        or aspect_ratio <= 0
        or not math.isclose(float(aspect_ratio), record["width"] / record["height"], rel_tol=0, abs_tol=1e-9)
    ):
        return False
    if record.get("decode_status") != "complete":
        return False
    semantic_status = record.get("semantic_analysis_status")
    semantic_review = record.get("semantic_review")
    if semantic_status not in {"semantic_pending", "semantic_complete"} or not isinstance(semantic_review, Mapping):
        return False
    if not all(isinstance(record.get(field), list) for field in (
        "key_frames", "action_timeline", "shot_table"
    )):
        return False
    for field in ("camera_angle", "person_orientation", "transitions", "final_conclusion"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            return False
    if semantic_status == "semantic_complete":
        from .video_analysis import SEMANTIC_FIELDS

        if not _valid_semantic_review(semantic_review, SEMANTIC_FIELDS):
            return False
        if not record["action_timeline"] or any(
            not isinstance(item, Mapping)
            or isinstance(item.get("timestamp_seconds"), bool)
            or not isinstance(item.get("timestamp_seconds"), (int, float))
            or not isinstance(item.get("action"), str)
            or not item["action"].strip()
            for item in record["action_timeline"]
        ):
            return False
        if not record["shot_table"] or any(
            not isinstance(item, Mapping)
            or any(
                isinstance(item.get(field), bool)
                or not isinstance(item.get(field), (int, float))
                for field in ("start", "end")
            )
            or item["end"] <= item["start"]
            or not isinstance(item.get("description"), str)
            or not item["description"].strip()
            for item in record["shot_table"]
        ):
            return False
        if any(
            "待人工语义分析" in str(record[field]) or "待复核" in str(record[field])
            for field in ("action_timeline", "shot_table", "camera_angle", "person_orientation", "transitions", "final_conclusion")
        ):
            return False
    for field in ("sample_timestamps", "scene_boundaries"):
        values = record[field]
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or item < 0
            for item in values
        ):
            return False
    return isinstance(record.get("contact_sheet"), str) and bool(record["contact_sheet"])


def _valid_semantic_review(review: object, fields: object) -> bool:
    if not isinstance(review, Mapping) or not isinstance(fields, (tuple, list)) or set(review) != set(fields):
        return False
    for value in review.values():
        if not isinstance(value, str) or not value.strip():
            return False
        normalized = re.sub(r"\s+", "", value).casefold()
        if any(
            token in normalized
            for token in (
                "待复核", "待填写", "待分析", "待人工语义分析", "待补充", "待确认",
                "根据证据填写", "按联系表记录", "具体记录", "观察结果",
            )
        ):
            return False
    anchors = ("动作时间轴", "机位高度", "身体朝向", "镜头连续性与真实切镜", "未知与冲突")
    if any(re.sub(r"\s+", "", str(review[field])).casefold() in {"未知", "无", "n/a"} for field in anchors):
        return False
    return _timeline_from_review(str(review["动作时间轴"])) is not None


def _timeline_from_review(value: str) -> list[dict[str, object]] | None:
    matches = list(re.finditer(r"(?P<time>\d+(?:\.\d+)?)\s*秒\s*(?P<action>[^，。；;]+)", value))
    if not matches:
        return None
    result = [
        {"timestamp_seconds": float(match.group("time")), "action": match.group("action").strip()}
        for match in matches
    ]
    if not all(item["action"] for item in result):
        return None
    timestamps = [float(item["timestamp_seconds"]) for item in result]
    if any(not math.isfinite(value) or value < 0 for value in timestamps):
        return None
    if any(right <= left for left, right in zip(timestamps, timestamps[1:])):
        return None
    return result


def _shot_table_from_timeline(
    timeline: list[dict[str, object]], duration: float
) -> list[dict[str, object]] | None:
    if not math.isfinite(duration) or duration <= 0:
        return None
    rows: list[dict[str, object]] = []
    for index, item in enumerate(timeline):
        start = float(item["timestamp_seconds"])
        end = (
            float(timeline[index + 1]["timestamp_seconds"])
            if index + 1 < len(timeline)
            else duration
        )
        if start >= duration or end <= start:
            return None
        rows.append({"start": start, "end": end, "description": str(item["action"])})
    return rows


def _join_review(*fields: str, review: Mapping[str, object]) -> str:
    return "；".join(f"{field}：{review[field]}" for field in fields)


def _sha256_file(path: Path) -> str:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("cache source must be a regular local file")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _write_exclusive(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise
