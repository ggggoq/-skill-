from __future__ import annotations

import hashlib
import heapq
import io
import math
import os
import re
import shutil
import stat
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Iterable

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from .media_audio import probe_audio


_ALLOWED_SUFFIXES = frozenset({".mp4", ".mov"})
_SAMPLE_INTERVAL_SECONDS = 0.25
_SCENE_BOUNDARY_THRESHOLD = 0.60
_MIN_SAMPLE_GAP_SECONDS = 0.25
_THUMBNAIL_SIZE = (320, 240)
_MAX_INTERVAL_SAMPLES = 240
_MAX_SCENE_SAMPLES = 15
_MAX_ANALYSIS_FRAMES = 1_000_000
ANALYSIS_VERSION = "dense-quarter-second-v1"
DECODE_CONFIG = {
    "sample_interval_seconds": _SAMPLE_INTERVAL_SECONDS,
    "scene_boundary_threshold": _SCENE_BOUNDARY_THRESHOLD,
    "thumbnail_size": list(_THUMBNAIL_SIZE),
    "max_interval_samples": _MAX_INTERVAL_SAMPLES,
    "max_scene_samples": _MAX_SCENE_SAMPLES,
}
_SHEET_COLUMNS = 3
_CELL_PADDING = 10
_LABEL_HEIGHT = 28
SEMANTIC_FIELDS = (
    "动作时间轴",
    "身体朝向",
    "转身方向与角度",
    "左右手动作",
    "手提物状态与换手",
    "步伐",
    "头部与视线",
    "摆臂",
    "衣服整理",
    "插兜",
    "机位高度",
    "景别",
    "构图",
    "运镜",
    "镜头连续性与真实切镜",
    "未知与冲突",
)
_SEMANTIC_INSTRUCTION = (
    "仅根据本地生成的高密度联系表证据分析参考视频；默认逐个视频以不超过 0.25 秒的时间间隔"
    "填写完整动作时间轴，逐项记录身体朝向、转身方向与角度、左右手、手提物与换手、步伐、头部与视线、"
    "摆臂、衣服整理、插兜、机位高度、景别、构图、运镜以及真实切镜。看不清或无法判断的内容明确标为未知，"
    "不得补造画外信息；关键动作不得改写为应避免的动作。参考是连续镜头时必须保持连续镜头。"
    "多个视频出现冲突时分别记录，不要擅自拼接。"
)


class VideoAnalysisError(ValueError):
    """A safe, user-actionable reference-video analysis or output error."""

    def __init__(self, message: str, *, sha256: str | None = None) -> None:
        super().__init__(message)
        self.sha256 = sha256


@dataclass
class VideoMetadata:
    path: Path
    status: str
    sha256: str | None = None
    backend_name: str | None = None
    frame_count: int | None = None
    fps: float | None = None
    width: int | None = None
    height: int | None = None
    duration_seconds: float | None = None
    sample_timestamps: list[float] = field(default_factory=list)
    scene_boundaries: list[float] = field(default_factory=list)
    contact_sheet: Path | None = None
    error: str | None = None
    absolute_path: str | None = None
    file_size_bytes: int | None = None
    mtime_ns: int | None = None
    aspect_ratio: float | None = None
    audio_status: str | None = None
    audio_duration_seconds: float | None = None
    decode_status: str | None = None
    key_frames: list[dict[str, object]] = field(default_factory=list)
    action_timeline: list[dict[str, object]] = field(default_factory=list)
    shot_table: list[dict[str, object]] = field(default_factory=list)
    camera_angle: str | None = None
    person_orientation: str | None = None
    transitions: str | None = None
    final_conclusion: str | None = None
    semantic_analysis_status: str = "semantic_pending"
    semantic_review: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["path"] = str(self.path)
        result["contact_sheet"] = str(self.contact_sheet) if self.contact_sheet else None
        if self.status == "success":
            try:
                source = Path(self.path)
                metadata = source.stat()
                result["absolute_path"] = str(source.resolve(strict=True))
                result["file_size_bytes"] = int(metadata.st_size)
                result["mtime_ns"] = int(metadata.st_mtime_ns)
            except OSError:
                pass
            if result["aspect_ratio"] is None and self.width and self.height:
                result["aspect_ratio"] = self.width / self.height
            if result["decode_status"] is None and self.frame_count and self.fps:
                result["decode_status"] = "complete"
            if not result["key_frames"]:
                result["key_frames"] = [
                    {"timestamp_seconds": timestamp, "evidence": "contact_sheet_sample"}
                    for timestamp in self.sample_timestamps
                ]
            if not result["action_timeline"]:
                result["action_timeline"] = [
                    {"timestamp_seconds": timestamp, "action": "待人工语义分析"}
                    for timestamp in self.sample_timestamps
                ]
            if not result["shot_table"] and self.duration_seconds:
                result["shot_table"] = _shot_table(self.duration_seconds, self.scene_boundaries)
            for field_name in ("camera_angle", "person_orientation", "transitions", "final_conclusion"):
                if result[field_name] is None:
                    result[field_name] = "待人工语义分析"
        return result


@dataclass
class AnalysisManifest:
    videos: list[VideoMetadata]

    @property
    def contact_sheets(self) -> list[Path]:
        return [
            video.contact_sheet
            for video in self.videos
            if video.status == "success" and video.contact_sheet is not None
        ]

    @property
    def failures(self) -> list[VideoMetadata]:
        return [video for video in self.videos if video.status == "failed"]

    @property
    def status(self) -> str:
        if not self.videos or len(self.failures) == len(self.videos):
            return "failed"
        if self.failures:
            return "partial"
        return "ready"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "status": self.status,
            "semantic_analysis": {
                "instruction": _SEMANTIC_INSTRUCTION,
                "fields": list(SEMANTIC_FIELDS),
                "per_video_template": {field_name: "" for field_name in SEMANTIC_FIELDS},
            },
            "videos": [
                {
                    **video.to_dict(),
                    "semantic_analysis_status": video.semantic_analysis_status,
                    "semantic_review": dict(video.semantic_review),
                }
                for video in self.videos
            ],
            "failures": [
                {"path": str(video.path), "error": video.error}
                for video in self.failures
            ],
        }


def extract_analysis_manifest(
    video_paths: Iterable[Path],
    run_dir: Path,
    decoder_factory: Callable[[Path], object] | None = None,
    *,
    cache: object | None = None,
) -> AnalysisManifest:
    """Read local MP4/MOV files and create deterministic timestamped contact sheets.

    The decoder factory is injectable for fully offline tests. Each local source
    is read once into a private run-owned snapshot while its digest is computed;
    both the injected and OpenCV decoders receive that exact snapshot path.
    """
    sources = [Path(video_path) for video_path in video_paths]
    if not sources:
        raise VideoAnalysisError("未提供参考视频，无法生成动作分镜分析")
    output_dir = Path(run_dir)
    _require_local_output_path(output_dir)
    local_sources = [source for source in sources if not _is_network_path(source)]
    _validate_output_directory(output_dir, local_sources, check_source_identity=False)
    factory = decoder_factory or _opencv_decoder_factory

    metadata: list[VideoMetadata] = []
    cached_records: set[int] = set()
    rendered: list[tuple[Path, bytes, tuple[Path, ...]]] = []
    for index, source in enumerate(sources, start=1):
        if _is_network_path(source):
            metadata.append(
                VideoMetadata(
                    path=source,
                    status="failed",
                    error=f"参考视频必须是本地文件，拒绝访问网络路径：{source}",
                )
            )
            continue
        cached = _cache_lookup(cache, source)
        if cached is not None:
            try:
                record, sheet_bytes = _cached_metadata(cached, source, output_dir, index)
            except (OSError, TypeError, ValueError, KeyError):
                record = None
            if record is not None:
                metadata.append(record)
                cached_records.add(id(record))
                rendered.append((Path(record.contact_sheet.name), sheet_bytes, (source,)))
                continue
        try:
            _validate_output_directory(output_dir, [source])
            record, sheet_bytes = _analyze_video(source, index, factory, output_dir)
        except VideoAnalysisError as exc:
            metadata.append(
                VideoMetadata(
                    path=source,
                    status="failed",
                    sha256=exc.sha256,
                    error=str(exc),
                )
            )
            continue
        metadata.append(record)
        if sheet_bytes is not None and record.contact_sheet is not None:
            rendered.append((Path(record.contact_sheet.name), sheet_bytes, (source,)))

    if rendered:
        _publish_contact_sheets(output_dir, rendered)
    for record in metadata:
        if record.status == "success" and id(record) not in cached_records:
            _cache_store(cache, record)
    return AnalysisManifest(videos=metadata)


def _cache_lookup(cache: object | None, source: Path) -> dict[str, object] | None:
    loader = getattr(cache, "load", None)
    if not callable(loader):
        return None
    try:
        lookup = loader(source, ANALYSIS_VERSION, DECODE_CONFIG)
    except Exception:
        return None
    if getattr(lookup, "hit", False) is not True:
        return None
    record = getattr(lookup, "record", None)
    return dict(record) if isinstance(record, dict) else None


def _cached_metadata(
    cached: dict[str, object], source: Path, output_dir: Path, index: int
) -> tuple[VideoMetadata, bytes]:
    required = {
        "sha256", "backend_name", "frame_count", "fps", "width", "height",
        "duration_seconds", "sample_timestamps", "scene_boundaries", "contact_sheet",
        "absolute_path", "file_size_bytes", "mtime_ns", "aspect_ratio", "decode_status",
        "key_frames", "action_timeline", "shot_table", "camera_angle",
        "person_orientation", "transitions", "final_conclusion",
        "semantic_analysis_status", "semantic_review",
    }
    if not required <= set(cached) or cached.get("status") != "success":
        raise ValueError("incomplete video analysis cache record")
    cached_sheet = Path(str(cached["contact_sheet"]))
    digest = str(cached["sha256"])
    safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", source.stem).strip("_")[:48] or "video"
    sheet_path = output_dir / f"{index:02d}-{safe_stem}-{digest[:12]}.png"
    sheet_bytes = cached_sheet.read_bytes()
    cached_audio = cached.get("audio_status")
    cached_audio_duration = cached.get("audio_duration_seconds")
    if cached_audio not in {"present", "absent", "unknown"}:
        probed = probe_audio(source)
        cached_audio = probed.status
        cached_audio_duration = probed.duration_seconds
    record = VideoMetadata(
        path=source,
        status="success",
        sha256=digest,
        backend_name=str(cached["backend_name"]),
        frame_count=int(cached["frame_count"]),
        fps=float(cached["fps"]),
        width=int(cached["width"]),
        height=int(cached["height"]),
        duration_seconds=float(cached["duration_seconds"]),
        sample_timestamps=[float(item) for item in cached["sample_timestamps"]],
        scene_boundaries=[float(item) for item in cached["scene_boundaries"]],
        contact_sheet=sheet_path,
        absolute_path=str(source.resolve(strict=True)),
        file_size_bytes=int(source.stat().st_size),
        mtime_ns=int(source.stat().st_mtime_ns),
        aspect_ratio=float(cached["aspect_ratio"]),
        audio_status=str(cached_audio),
        audio_duration_seconds=(
            float(cached_audio_duration)
            if isinstance(cached_audio_duration, (int, float))
            and not isinstance(cached_audio_duration, bool)
            else None
        ),
        decode_status=str(cached["decode_status"]),
        key_frames=list(cached["key_frames"]),
        action_timeline=list(cached["action_timeline"]),
        shot_table=list(cached["shot_table"]),
        camera_angle=str(cached["camera_angle"]),
        person_orientation=str(cached["person_orientation"]),
        transitions=str(cached["transitions"]),
        final_conclusion=str(cached["final_conclusion"]),
        semantic_analysis_status=str(cached["semantic_analysis_status"]),
        semantic_review=dict(cached["semantic_review"]),
    )
    return record, sheet_bytes


def _cache_store(cache: object | None, record: VideoMetadata) -> None:
    store = getattr(cache, "store", None)
    if not callable(store):
        return
    try:
        store(record, ANALYSIS_VERSION, DECODE_CONFIG)
    except Exception:
        # A cache write must never invalidate a completed local analysis.
        return


def _looks_like_video_container(path: Path) -> bool:
    """Cheap signature guard for unknown extensions before decoder creation."""

    try:
        header = path.read_bytes()[:4096]
    except OSError:
        return False
    if len(header) < 4:
        return False
    if header.startswith((
        b"RIFF",                 # AVI / WebP containers
        b"\x1a\x45\xdf\xa3",    # Matroska / WebM (EBML)
        b"OggS",                 # Ogg / OGV
        b"FLV",                  # Flash Video
        b"fLaC",                 # FLAC-in-container edge case
        b"\x06\x0e\x2b\x34",    # MXF
        b"\x00\x00\x01\xba",    # MPEG program stream / VOB
    )):
        return True
    if b"ftyp" in header[:64] or header.startswith(b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"):
        return True
    if header[0] == 0x47 and len(header) >= 188 and header[187] == 0x47:
        return True
    return False


def _analyze_video(
    path: Path,
    index: int,
    decoder_factory: Callable[[Path], object],
    output_dir: Path,
) -> tuple[VideoMetadata, bytes | None]:
    (
        snapshot_path,
        source_hash,
        snapshot_directory,
        created_directories,
        snapshot_identity,
        snapshot_directory_identity,
    ) = (
        _create_video_snapshot(path, output_dir)
    )

    decoder = None
    try:
        if path.suffix.lower() not in _ALLOWED_SUFFIXES and not _looks_like_video_container(snapshot_path):
            raise VideoAnalysisError(
                f"参考视频内容无法识别或解码：{path}", sha256=source_hash
            )
        if snapshot_path.stat().st_size == 0:
            raise VideoAnalysisError(f"参考视频为空文件：{path}", sha256=source_hash)
        decoder = decoder_factory(snapshot_path)
        if not _decoder_is_open(decoder):
            raise VideoAnalysisError(f"参考视频无法解码或文件无效：{path}")
        backend_name = _backend_name(decoder)
        frame_count, fps, width, height = _read_properties(decoder, path)
        duration = frame_count / fps
        samples, boundaries = _decode_samples(
            decoder, frame_count, fps, width, height, path
        )
        if not samples:
            raise VideoAnalysisError(f"参考视频没有可读取的画面：{path}")

        sheet = _make_contact_sheet(samples)
        safe_stem = re.sub(r"[^A-Za-z0-9_-]+", "_", path.stem).strip("_")[:48] or "video"
        sheet_path = output_dir / f"{index:02d}-{safe_stem}-{source_hash[:12]}.png"
        audio = probe_audio(snapshot_path)
        record = VideoMetadata(
            path=path,
            status="success",
            sha256=source_hash,
            backend_name=backend_name,
            frame_count=frame_count,
            fps=fps,
            width=width,
            height=height,
            duration_seconds=duration,
            sample_timestamps=[round(item[0], 3) for item in samples],
            scene_boundaries=[round(timestamp, 3) for timestamp in boundaries],
            absolute_path=str(path.resolve(strict=True)),
            file_size_bytes=int(path.stat().st_size),
            mtime_ns=int(path.stat().st_mtime_ns),
            aspect_ratio=width / height,
            audio_status=audio.status,
            audio_duration_seconds=audio.duration_seconds,
            decode_status="complete",
            key_frames=[
                {"timestamp_seconds": round(item[0], 3), "evidence": "contact_sheet_sample"}
                for item in samples
            ],
            action_timeline=[
                {"timestamp_seconds": round(item[0], 3), "action": "待人工语义分析"}
                for item in samples
            ],
            shot_table=_shot_table(duration, boundaries),
            camera_angle="待人工语义分析",
            person_orientation="待人工语义分析",
            transitions="待人工语义分析",
            final_conclusion="待人工语义分析",
        )
        # The index avoids collisions when two input files share a stem.
        record.contact_sheet = sheet_path
        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG", optimize=False, compress_level=9)
        return record, buffer.getvalue()
    except VideoAnalysisError as exc:
        raise VideoAnalysisError(str(exc), sha256=source_hash) from exc
    except Exception as exc:
        raise VideoAnalysisError(f"参考视频解码失败：{path}") from exc
    finally:
        if decoder is not None:
            try:
                decoder.release()
            except Exception:
                pass
        _cleanup_video_snapshot(
            snapshot_path,
            snapshot_directory,
            created_directories,
            snapshot_identity=snapshot_identity,
            snapshot_directory_identity=snapshot_directory_identity,
        )


def _shot_table(duration: float, boundaries: list[float]) -> list[dict[str, object]]:
    points = [0.0, *(value for value in boundaries if 0 < value < duration), duration]
    points = sorted(set(round(value, 3) for value in points))
    return [
        {"index": index, "start_seconds": start, "end_seconds": end}
        for index, (start, end) in enumerate(zip(points, points[1:]), start=1)
    ]


def _create_video_snapshot(
    path: Path, output_dir: Path
) -> tuple[Path, str, Path, list[Path], tuple[int, int], tuple[int, int]]:
    """Copy and hash a source once into an exclusive task-owned staging directory."""
    created_directories: list[Path] = []
    snapshot_directory: Path | None = None
    snapshot_path: Path | None = None
    snapshot_owned = False
    snapshot_identity: tuple[int, int] | None = None
    snapshot_directory_identity: tuple[int, int] | None = None
    succeeded = False
    try:
        path_metadata = path.stat()
    except OSError as exc:
        raise VideoAnalysisError(f"无法检查参考视频文件：{path}") from exc
    if not stat.S_ISREG(path_metadata.st_mode):
        raise VideoAnalysisError(f"参考视频不存在或不是普通文件：{path}")
    try:
        with path.open("rb") as source_stream:
            try:
                metadata = os.fstat(source_stream.fileno())
            except OSError as exc:
                raise VideoAnalysisError(f"无法检查参考视频文件：{path}") from exc
            if not stat.S_ISREG(metadata.st_mode):
                raise VideoAnalysisError(f"参考视频不存在或不是普通文件：{path}")

            _ensure_directory(output_dir, created_directories)
            snapshot_directory = Path(
                tempfile.mkdtemp(prefix=".video-snapshot-", dir=output_dir)
            )
            _require_local_output_path(snapshot_directory)
            directory_metadata = snapshot_directory.lstat()
            if not stat.S_ISDIR(directory_metadata.st_mode):
                raise VideoAnalysisError(f"无法创建私有参考视频快照目录：{path}")
            snapshot_directory_identity = (
                directory_metadata.st_dev,
                directory_metadata.st_ino,
            )
            snapshot_path = snapshot_directory / f"snapshot{path.suffix.lower()}"
            descriptor = os.open(
                snapshot_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            snapshot_owned = True
            try:
                file_metadata = os.fstat(descriptor)
                if not stat.S_ISREG(file_metadata.st_mode):
                    raise VideoAnalysisError(f"参考视频安全快照不是普通文件：{path}")
                snapshot_identity = (file_metadata.st_dev, file_metadata.st_ino)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            digest = hashlib.sha256()
            try:
                snapshot_stream = os.fdopen(descriptor, "wb")
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            with snapshot_stream:
                while True:
                    chunk = source_stream.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    snapshot_stream.write(chunk)
                snapshot_stream.flush()
                os.fsync(snapshot_stream.fileno())
            result = (
                snapshot_path,
                digest.hexdigest(),
                snapshot_directory,
                created_directories,
                snapshot_identity,
                snapshot_directory_identity,
            )
            succeeded = True
            return result
    except VideoAnalysisError:
        raise
    except Exception as exc:
        raise VideoAnalysisError(f"无法读取或创建参考视频安全快照：{path}") from exc
    finally:
        if not succeeded:
            _cleanup_video_snapshot(
                snapshot_path,
                snapshot_directory,
                created_directories,
                snapshot_identity=snapshot_identity,
                snapshot_directory_identity=snapshot_directory_identity,
                remove_snapshot_file=snapshot_owned,
            )


def _cleanup_video_snapshot(
    snapshot_path: Path | None,
    snapshot_directory: Path | None,
    created_directories: list[Path],
    *,
    snapshot_identity: tuple[int, int] | None = None,
    snapshot_directory_identity: tuple[int, int] | None = None,
    remove_snapshot_file: bool = True,
) -> None:
    if remove_snapshot_file and snapshot_path is not None and snapshot_identity is not None:
        try:
            metadata = snapshot_path.lstat()
            if (
                stat.S_ISREG(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == snapshot_identity
            ):
                snapshot_path.unlink()
        except OSError:
            pass
    if snapshot_directory is not None and snapshot_directory_identity is not None:
        try:
            metadata = snapshot_directory.lstat()
            if (
                stat.S_ISDIR(metadata.st_mode)
                and (metadata.st_dev, metadata.st_ino) == snapshot_directory_identity
            ):
                snapshot_directory.rmdir()
        except OSError:
            pass
    for directory in reversed(created_directories):
        try:
            directory.rmdir()
        except OSError:
            pass


def _opencv_decoder_factory(path: Path):
    return cv2.VideoCapture(str(path))


def _decoder_is_open(decoder: object) -> bool:
    is_opened = getattr(decoder, "isOpened", None)
    return bool(is_opened()) if callable(is_opened) else True


def _backend_name(decoder: object) -> str:
    getter = getattr(decoder, "getBackendName", None)
    if callable(getter):
        try:
            name = getter()
            if name:
                return str(name)
        except Exception:
            pass
    return type(decoder).__name__


def _read_properties(decoder: object, path: Path) -> tuple[int, float, int, int]:
    getter = getattr(decoder, "get", None)
    if not callable(getter):
        raise VideoAnalysisError(f"参考视频解码器缺少元数据接口：{path}")
    try:
        frame_count_value = float(getter(cv2.CAP_PROP_FRAME_COUNT))
        fps = float(getter(cv2.CAP_PROP_FPS))
        width_value = float(getter(cv2.CAP_PROP_FRAME_WIDTH))
        height_value = float(getter(cv2.CAP_PROP_FRAME_HEIGHT))
    except Exception as exc:
        raise VideoAnalysisError(f"无法读取参考视频元数据：{path}") from exc
    if not all(math.isfinite(value) for value in (frame_count_value, fps, width_value, height_value)):
        raise VideoAnalysisError(f"参考视频元数据无效（帧数、帧率或画面尺寸）：{path}")
    frame_count, width, height = int(round(frame_count_value)), int(round(width_value)), int(round(height_value))
    if frame_count < 1 or fps <= 0 or width < 1 or height < 1:
        raise VideoAnalysisError(f"参考视频元数据无效（帧数、帧率或画面尺寸）：{path}")
    if frame_count > _MAX_ANALYSIS_FRAMES:
        raise VideoAnalysisError(
            f"参考视频帧数超过离线分析安全上限 {_MAX_ANALYSIS_FRAMES}：{path}"
        )
    if not math.isfinite(frame_count / fps):
        raise VideoAnalysisError(f"参考视频时长元数据超出安全范围：{path}")
    return frame_count, fps, width, height


def _decode_samples(
    decoder: object,
    frame_count: int,
    fps: float,
    width: int,
    height: int,
    path: Path,
) -> tuple[list[tuple[float, object]], list[float]]:
    """Stream frames, retaining at most 240 quarter-second anchors and 15 scene cuts.

    Quarter-second anchors take priority. For videos longer than the interval budget,
    anchors are spread deterministically across the clip. Scene
    candidates are grouped by the 250 ms de-duplication window while
    streaming, then fill up to 15 remaining slots by highest histogram
    difference, breaking ties in favor of earlier frames. Every retained image
    is reduced immediately to a 320x240-bounded thumbnail; metadata is capped
    at one million decoded frames before any metadata-sized iteration is
    formed.
    """
    reader = getattr(decoder, "read", None)
    if not callable(reader):
        raise VideoAnalysisError(f"参考视频解码器缺少逐帧读取接口：{path}")
    if frame_count > _MAX_ANALYSIS_FRAMES:
        raise VideoAnalysisError(
            f"参考视频帧数超过离线分析安全上限 {_MAX_ANALYSIS_FRAMES}：{path}"
        )
    interval_indices = _interval_sample_indices(frame_count, fps)
    interval_samples: dict[int, tuple[float, Image.Image]] = {}
    scene_samples: list[tuple[float, int, float, Image.Image]] = []
    pending_scene: tuple[float, int, float, Image.Image] | None = None
    pending_scene_start: float | None = None
    previous_histogram = None

    for frame_index in range(frame_count):
        try:
            ok, frame = reader()
        except Exception as exc:
            raise VideoAnalysisError(f"参考视频读取第 {frame_index} 帧失败：{path}") from exc
        if not ok or frame is None:
            raise VideoAnalysisError(f"参考视频在第 {frame_index} 帧截断或损坏：{path}")
        frame = _normalize_frame(frame, path)
        if frame.size == 0:
            raise VideoAnalysisError(f"参考视频含空画面（第 {frame_index} 帧）：{path}")
        frame_height, frame_width = frame.shape[:2]
        if frame_width != width or frame_height != height:
            raise VideoAnalysisError(f"参考视频画面尺寸与元数据不一致：{path}")

        timestamp = frame_index / fps
        histogram = _gray_histogram(frame)
        difference = 0.0
        if previous_histogram is not None:
            difference = cv2.compareHist(
                previous_histogram, histogram, cv2.HISTCMP_BHATTACHARYYA
            )
            if difference >= _SCENE_BOUNDARY_THRESHOLD:
                candidate = (float(difference), frame_index, timestamp)
                if pending_scene is None or pending_scene_start is None:
                    pending_scene_start = timestamp
                    pending_scene = (*candidate, _make_thumbnail(frame))
                elif timestamp - pending_scene_start < _MIN_SAMPLE_GAP_SECONDS:
                    if (candidate[0], -candidate[1]) > (
                        pending_scene[0], -pending_scene[1]
                    ):
                        pending_scene = (*candidate, _make_thumbnail(frame))
                else:
                    _retain_scene_candidate(scene_samples, pending_scene)
                    pending_scene_start = timestamp
                    pending_scene = (*candidate, _make_thumbnail(frame))
        previous_histogram = histogram

        if frame_index in interval_indices:
            interval_samples[frame_index] = (timestamp, _make_thumbnail(frame))

    if pending_scene is not None:
        _retain_scene_candidate(scene_samples, pending_scene)

    selected_by_frame = {
        frame_index: [timestamp, thumbnail, {"interval"}]
        for frame_index, (timestamp, thumbnail) in interval_samples.items()
    }
    interval_timestamps = [sample[0] for sample in interval_samples.values()]
    accepted_scene_timestamps: list[float] = []
    for _, frame_index, timestamp, thumbnail in sorted(scene_samples, key=lambda item: item[2]):
        if frame_index in selected_by_frame:
            selected_by_frame[frame_index][2].add("scene_boundary")
            continue
        if any(
            abs(timestamp - anchor) < _MIN_SAMPLE_GAP_SECONDS
            for anchor in interval_timestamps
        ) or any(
            abs(timestamp - prior) < _MIN_SAMPLE_GAP_SECONDS
            for prior in accepted_scene_timestamps
        ):
            # The nearby interval anchor remains the evidence for this moment;
            # scene detection must not evict or crowd out the anchor.
            continue
        selected_by_frame[frame_index] = [timestamp, thumbnail, {"scene_boundary"}]
        accepted_scene_timestamps.append(timestamp)

    ordered = sorted(selected_by_frame.values(), key=lambda item: item[0])
    # Interval anchors are the evidence contract, not duplicate scene detections.
    # Coalescing neighboring frames here can discard an anchor and double the next
    # gap (for example, alternating 7/8-frame steps at 30 fps). Duplicate frames
    # were already combined by selected_by_frame above, so retain every timestamp.
    samples = [(timestamp, thumbnail) for timestamp, thumbnail, _ in ordered]
    boundaries = sorted(
        timestamp for _, _, timestamp, _ in scene_samples
    )
    return samples, boundaries


def _interval_sample_indices(frame_count: int, fps: float) -> set[int]:
    duration = frame_count / fps
    interval_count = max(1, math.ceil(duration / _SAMPLE_INTERVAL_SECONDS))
    if interval_count <= _MAX_INTERVAL_SAMPLES:
        slots = range(interval_count)
    else:
        last_slot = _MAX_INTERVAL_SAMPLES - 1
        last_interval = interval_count - 1
        slots = (
            (slot * last_interval + last_slot // 2) // last_slot
            for slot in range(_MAX_INTERVAL_SAMPLES)
        )
    return {
        min(
            frame_count - 1,
            int(round(slot * _SAMPLE_INTERVAL_SECONDS * fps)),
        )
        for slot in slots
    }


def _make_thumbnail(rgb_frame: np.ndarray) -> Image.Image:
    image = Image.fromarray(rgb_frame)
    return ImageOps.contain(
        image, _THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS
    ).copy()


def _retain_scene_candidate(
    retained: list[tuple[float, int, float, Image.Image]],
    candidate: tuple[float, int, float, Image.Image],
) -> None:
    difference, frame_index, _, _ = candidate
    rank = (difference, -frame_index)
    if len(retained) >= _MAX_SCENE_SAMPLES and rank <= retained[0][:2]:
        return
    ranked_candidate = (difference, -frame_index, candidate[2], candidate[3])
    if len(retained) < _MAX_SCENE_SAMPLES:
        heapq.heappush(retained, ranked_candidate)
    else:
        heapq.heapreplace(retained, ranked_candidate)


def _normalize_frame(frame: object, path: Path) -> np.ndarray:
    if isinstance(frame, Image.Image):
        return np.asarray(frame.convert("RGB"))
    try:
        array = np.asarray(frame)
    except Exception as exc:
        raise VideoAnalysisError(f"参考视频画面格式无效：{path}") from exc
    if array.ndim == 2:
        return cv2.cvtColor(array, cv2.COLOR_GRAY2RGB)
    if array.ndim != 3 or array.shape[2] not in {3, 4}:
        raise VideoAnalysisError(f"参考视频画面格式无效：{path}")
    if array.shape[2] == 4:
        return cv2.cvtColor(array, cv2.COLOR_BGRA2RGB)
    return cv2.cvtColor(array, cv2.COLOR_BGR2RGB)


def _gray_histogram(rgb_frame: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2GRAY)
    histogram = cv2.calcHist([gray], [0], None, [32], [0, 256])
    cv2.normalize(histogram, histogram)
    return histogram


def _make_contact_sheet(samples: list[tuple[float, object]]) -> Image.Image:
    columns = min(_SHEET_COLUMNS, len(samples))
    rows = math.ceil(len(samples) / _SHEET_COLUMNS)
    cell_width = _THUMBNAIL_SIZE[0] + 2 * _CELL_PADDING
    cell_height = _THUMBNAIL_SIZE[1] + _LABEL_HEIGHT + 2 * _CELL_PADDING
    sheet = Image.new(
        "RGB", (columns * cell_width, rows * cell_height), (246, 246, 246)
    )
    draw = ImageDraw.Draw(sheet)
    font = ImageFont.load_default()

    for index, (timestamp, frame) in enumerate(samples):
        image = (
            frame.convert("RGB")
            if isinstance(frame, Image.Image)
            else Image.fromarray(np.asarray(frame)).convert("RGB")
        )
        thumbnail = ImageOps.contain(image, _THUMBNAIL_SIZE, method=Image.Resampling.LANCZOS)
        column, row = index % _SHEET_COLUMNS, index // _SHEET_COLUMNS
        left = column * cell_width + (cell_width - thumbnail.width) // 2
        top = row * cell_height + _CELL_PADDING
        sheet.paste(thumbnail, (left, top))
        label = f"{timestamp:07.3f} s"
        draw.text(
            (column * cell_width + _CELL_PADDING, top + _THUMBNAIL_SIZE[1] + 3),
            label,
            fill=(20, 20, 20),
            font=font,
        )
    return sheet


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_output_directory(
    output_dir: Path,
    sources: list[Path],
    *,
    check_source_identity: bool = True,
) -> None:
    _require_local_output_path(output_dir)
    if ".." in output_dir.parts:
        raise VideoAnalysisError(f"输出路径包含父目录跳转，拒绝写入：{output_dir}")
    _reject_reparse_components(output_dir)
    try:
        resolved_output = output_dir.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise VideoAnalysisError(f"无法安全解析输出路径：{output_dir}") from exc
    for source in sources:
        source_absolute = Path(os.path.abspath(source))
        output_absolute = Path(os.path.abspath(output_dir))
        if (
            _is_within(source_absolute, output_absolute)
            or _is_within(output_absolute, source_absolute.parent)
        ):
            raise VideoAnalysisError(
                f"输出路径与源视频目录重合，拒绝写入：{output_dir}；源视频：{source}"
            )
        if not check_source_identity:
            continue
        try:
            resolved_source = source.resolve(strict=False)
            source_parent = source.parent.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise VideoAnalysisError(f"无法核对参考视频和输出路径：{source}") from exc
        if (
            _is_within(resolved_source, resolved_output)
            or _is_within(resolved_output, source_parent)
            or _same_existing_file(resolved_source, resolved_output)
        ):
            raise VideoAnalysisError(f"输出路径与源视频目录重合，拒绝写入：{output_dir}；源视频：{source}")
    if output_dir.exists() and not output_dir.is_dir():
        raise VideoAnalysisError(f"输出路径不是文件夹，拒绝写入：{output_dir}")


def _is_network_path(path: Path) -> bool:
    normalized = str(path).replace("\\", "/")
    if normalized.startswith("//"):
        return True
    scheme = re.match(r"^([A-Za-z][A-Za-z0-9+.-]*):", normalized)
    if scheme and not (len(scheme.group(1)) == 1 and normalized[1:2] == ":"):
        return True
    return False


def _require_local_output_path(path: Path) -> None:
    if _is_network_path(path):
        raise VideoAnalysisError(f"输出目录必须是本地路径，拒绝访问网络路径：{path}")


def _reject_reparse_components(path: Path) -> None:
    _require_local_output_path(path)
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise VideoAnalysisError(f"无法检查输出路径组件：{component}") from exc
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise VideoAnalysisError(f"输出路径经过符号链接或 Windows 重解析点，拒绝写入：{component}")


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
        raise VideoAnalysisError(f"无法比较源文件与输出路径：{first}") from exc


def _publish_contact_sheets(
    output_dir: Path, outputs: list[tuple[Path, bytes, tuple[Path, ...]]]
) -> None:
    _require_local_output_path(output_dir)
    absolute_outputs = [
        (output_dir / name, payload, sources) for name, payload, sources in outputs
    ]
    expected_hashes = {
        target: hashlib.sha256(payload).hexdigest()
        for target, payload, _ in absolute_outputs
    }
    reusable: set[Path] = set()
    for target, _, sources in absolute_outputs:
        if _existing_output_matches(target, expected_hashes[target], sources):
            reusable.add(target)
    needed = [
        item for item in absolute_outputs if item[0] not in reusable
    ]
    if not needed:
        return

    created_dirs: list[Path] = []
    staged: list[tuple[Path, Path, tuple[int, int]]] = []
    published: list[tuple[Path, tuple[int, int]]] = []
    succeeded = False
    current_path = needed[0][0]
    try:
        _ensure_directory(output_dir, created_dirs)
        _validate_output_directory(
            output_dir, [source for _, _, sources in absolute_outputs for source in sources]
        )
        for target, payload, _ in needed:
            current_path = target
            _reject_reparse_components(target)
            temp_path, identity = _write_unique_temp(target, payload)
            staged.append((temp_path, target, identity))
        for temp_path, target, identity in staged:
            current_path = target
            _reject_reparse_components(target)
            published.append((target, identity))
            try:
                _publish_exclusive(temp_path, target)
            except FileExistsError:
                if _existing_output_matches(
                    target,
                    expected_hashes[target],
                    next(sources for path, _, sources in absolute_outputs if path == target),
                ):
                    _remove_temp(temp_path)
                    continue
                raise VideoAnalysisError(f"现有输出内容冲突，已保留原文件：{target}")
            _remove_temp(temp_path)
        succeeded = True
    except VideoAnalysisError:
        raise
    except OSError as exc:
        raise VideoAnalysisError(f"无法保存参考视频联系表：{current_path}") from exc
    finally:
        if not succeeded:
            for temp_path, _, _ in staged:
                _remove_temp(temp_path)
            for target, identity in reversed(published):
                _remove_owned_file(target, identity)
            for directory in reversed(created_dirs):
                try:
                    directory.rmdir()
                except OSError:
                    pass


def _existing_output_matches(
    path: Path, expected_hash: str, sources: tuple[Path, ...]
) -> bool:
    _require_local_output_path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise VideoAnalysisError(f"无法检查现有联系表：{path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise VideoAnalysisError(f"联系表不能是链接或重解析点：{path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise VideoAnalysisError(f"现有联系表不是普通文件，已保留原内容：{path}")
    if any(_same_existing_file(path, source) for source in sources):
        raise VideoAnalysisError(f"联系表与源视频文件别名相同，拒绝写入：{path}")
    if _sha256_file(path) != expected_hash:
        raise VideoAnalysisError(f"现有输出内容冲突，已保留原文件：{path}")
    return True


def _ensure_directory(path: Path, created_dirs: list[Path]) -> None:
    _require_local_output_path(path)
    missing: list[Path] = []
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                raise VideoAnalysisError(f"无法定位输出文件夹：{path}")
            current = current.parent
            continue
        except OSError as exc:
            raise VideoAnalysisError(f"无法检查输出文件夹：{current}") from exc
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise VideoAnalysisError(f"输出文件夹经过链接或重解析点：{current}")
        if not stat.S_ISDIR(metadata.st_mode):
            raise VideoAnalysisError(f"输出路径组件不是文件夹：{current}")
        break
    for directory in reversed(missing):
        try:
            directory.mkdir()
            created_dirs.append(directory)
        except FileExistsError:
            metadata = directory.lstat()
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or stat.S_ISLNK(metadata.st_mode)
                or getattr(metadata, "st_file_attributes", 0) & 0x400
            ):
                raise VideoAnalysisError(f"输出文件夹路径冲突，拒绝写入：{directory}")
        except OSError as exc:
            raise VideoAnalysisError(f"无法创建输出文件夹：{directory}") from exc


def _write_unique_temp(target: Path, payload: bytes) -> tuple[Path, tuple[int, int]]:
    _require_local_output_path(target)
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
                os.close(descriptor)
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        identity = _file_identity(temp_path)
        succeeded = True
        return temp_path, identity
    except OSError as exc:
        raise VideoAnalysisError(f"无法创建联系表临时文件：{target}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if not succeeded and temp_path is not None:
            _remove_temp(temp_path)


def _publish_exclusive(temp_path: Path, target: Path) -> None:
    _require_local_output_path(temp_path)
    _require_local_output_path(target)
    if os.name == "nt":
        os.rename(temp_path, target)
    else:
        os.link(temp_path, target)


def _file_identity(path: Path) -> tuple[int, int]:
    _require_local_output_path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise VideoAnalysisError(f"无法检查联系表临时文件：{path}") from exc
    return metadata.st_dev, metadata.st_ino


def _remove_temp(path: Path) -> None:
    _require_local_output_path(path)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _remove_owned_file(path: Path, identity: tuple[int, int]) -> None:
    _require_local_output_path(path)
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity and stat.S_ISREG(metadata.st_mode):
            path.unlink()
    except OSError:
        pass
