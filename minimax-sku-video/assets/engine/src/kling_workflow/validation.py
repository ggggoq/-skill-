from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .media_audio import probe_audio, validate_audio


_REPORT_NAME = "视频校验.json"
_FRAME_POSITION_TOLERANCE = 0.25
_BACKEND_NAME = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

# 2026-11-05 主管口径：成片只卡宽度下限 720 px，高度不限，取消精确 9:16 门禁。
# 提交载荷仍可请求 9:16（让平台尽量出竖版），但验收不再按比例拦截。
MIN_OUTPUT_WIDTH = 720


class VideoValidationError(RuntimeError):
    """A safe report-persistence error without local paths or backend details."""


@dataclass(frozen=True)
class VideoValidation:
    ok: bool
    errors: list[str]
    width: int | None
    height: int | None
    duration: float | None
    decoder_backend: str | None
    file_size: int | None
    sha256: str | None
    audio_status: str | None = None
    audio_duration_seconds: float | None = None
    audio_ok: bool | None = None
    audio_silent: bool | None = None
    audio_max_db: float | None = None
    audio_silence_method: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        for key in (
            "audio_status",
            "audio_duration_seconds",
            "audio_ok",
            "audio_silent",
            "audio_max_db",
            "audio_silence_method",
        ):
            if result.get(key) is None:
                result.pop(key, None)
        return result


def validate_video(
    path: str | Path,
    *,
    decoder: Callable[[Path], object] | None = None,
    write_report: bool = True,
    expected_aspect_ratio: float = 9 / 16,
    require_audio: bool = False,
    decode_all_frames: bool = False,
    expected_duration_seconds: float | None = None,
) -> VideoValidation:
    """Decode sampled or all frames and bind the report to one unchanged local MP4."""
    errors: list[str] = []
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    backend: str | None = None
    file_size: int | None = None
    digest: str | None = None
    audio_status: str | None = None
    audio_duration_seconds: float | None = None
    audio_ok: bool | None = None
    audio_silent: bool | None = None
    audio_max_db: float | None = None
    audio_silence_method: str | None = None
    report_allowed = False
    capture: object | None = None
    identity_before: tuple[int, int, int, int] | None = None
    if (
        not isinstance(expected_aspect_ratio, (int, float))
        or isinstance(expected_aspect_ratio, bool)
        or not math.isfinite(float(expected_aspect_ratio))
        or expected_aspect_ratio <= 0
    ):
        expected_aspect_ratio = 9 / 16

    try:
        video_path = Path(path)
        if video_path.suffix.lower() != ".mp4":
            errors.append("视频文件必须使用 .mp4 扩展名")
        elif _is_network_path(video_path):
            errors.append("视频必须是本地普通文件，不能使用网络路径")
        else:
            try:
                identity_before, metadata = _regular_file_identity(video_path)
                file_size = int(metadata.st_size)
                report_allowed = True
            except (OSError, TypeError, ValueError):
                errors.append("视频必须是本地普通文件，不能是链接或重解析点")

        if report_allowed and file_size == 0:
            errors.append("视频文件为空，无法验证")

        initial_digest: str | None = None
        if report_allowed and file_size:
            try:
                initial_digest = _sha256_file(video_path, identity_before)
                digest = initial_digest
            except (OSError, TypeError, ValueError):
                errors.append("无法安全读取视频文件")

        initial_metadata_valid = report_allowed and file_size is not None and file_size > 0 and initial_digest is not None
        if initial_metadata_valid:
            try:
                identity_before_decode, _ = _regular_file_identity(video_path)
                if identity_before_decode != identity_before:
                    errors.append("视频文件身份在校验过程中发生变化，请保留候选文件并重试")
                else:
                    capture = (decoder or _opencv_decoder)(video_path)
                    is_open = getattr(capture, "isOpened", None)
                    if not callable(is_open) or is_open() is not True:
                        errors.append("视频无法通过 OpenCV 解码，请检查文件是否完整")
                    else:
                        try:
                            backend_name = capture.getBackendName()
                            if not isinstance(backend_name, str) or not _BACKEND_NAME.fullmatch(backend_name):
                                raise ValueError
                            backend = backend_name
                        except Exception:
                            errors.append("无法安全读取视频解码器信息")

                        frame_count = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_COUNT))
                        fps = _positive_float(capture.get(cv2.CAP_PROP_FPS))
                        width = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
                        height = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
                        if frame_count is None or fps is None or width is None or height is None:
                            errors.append("视频帧数、帧率、时长和宽高必须大于 0")
                        else:
                            duration = frame_count / fps
                            if not math.isfinite(duration) or duration <= 0:
                                errors.append("视频帧数、帧率、时长和宽高必须大于 0")
                            elif (
                                expected_duration_seconds is not None
                                and round(duration, 3) != round(expected_duration_seconds, 3)
                            ):
                                errors.append("视频时长与预期时长不一致")
                            actual_ratio = width / height
                            if width >= height:
                                errors.append("视频必须为竖版（高度大于宽度）；不固定具体比例，禁止横版视频")

                        if (
                            frame_count is not None
                            and fps is not None
                            and width is not None
                            and height is not None
                            and duration is not None
                            and math.isfinite(duration)
                            and duration > 0
                            and width < height
                            and backend is not None
                            and (
                                expected_duration_seconds is None
                                or round(duration, 3) == round(expected_duration_seconds, 3)
                            )
                        ):
                            if decode_all_frames:
                                _decode_all_frames(capture, frame_count, width, height, errors)
                            else:
                                for label, index in _target_frames(frame_count):
                                    try:
                                        seeked = capture.set(cv2.CAP_PROP_POS_FRAMES, index)
                                        if seeked is not True:
                                            errors.append(f"视频{label}无法定位解码")
                                            continue
                                        if not _frame_position_matches(capture, index):
                                            errors.append(f"视频{label}实际定位位置与目标帧不一致")
                                            continue
                                        success, frame = capture.read()
                                        if success is not True or not _valid_frame(frame, width, height):
                                            errors.append(f"视频{label}无法解码或帧尺寸无效")
                                        if not _frame_position_matches(capture, index + 1):
                                            errors.append(f"视频{label}解码后的帧位置与目标不一致")
                                    except Exception:
                                        errors.append(f"视频{label}无法安全解码")
            except Exception:
                errors.append("视频无法通过 OpenCV 解码，请检查文件是否完整")
        elif not errors:
            errors.append("无法安全读取视频文件")
        if require_audio and report_allowed and file_size:
            audio_report = validate_audio(video_path, expected_duration=duration)
            audio_status = audio_report.get("audio_status")
            raw_audio_duration = audio_report.get("audio_duration_seconds")
            audio_duration_seconds = (
                float(raw_audio_duration)
                if isinstance(raw_audio_duration, (int, float))
                and not isinstance(raw_audio_duration, bool)
                else None
            )
            audio_ok = audio_report.get("audio_ok") is True
            raw_silent = audio_report.get("audio_silent")
            audio_silent = raw_silent if isinstance(raw_silent, bool) else None
            raw_max_db = audio_report.get("audio_max_db")
            audio_max_db = (
                float(raw_max_db)
                if isinstance(raw_max_db, (int, float)) and not isinstance(raw_max_db, bool)
                else None
            )
            raw_method = audio_report.get("audio_silence_method")
            audio_silence_method = raw_method if isinstance(raw_method, str) else None
            errors.extend(
                item for item in audio_report.get("audio_errors", []) if isinstance(item, str)
            )
    except Exception:
        errors.append("视频路径无效，无法安全校验")
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                errors.append("无法安全关闭视频解码器")

    if report_allowed and identity_before is not None:
        try:
            identity_after, _ = _regular_file_identity(video_path)
            final_digest = _sha256_file(video_path, identity_before)
            if identity_after != identity_before or (digest is not None and final_digest != digest):
                errors.append("视频文件身份在校验过程中发生变化，请保留候选文件并重试")
        except (OSError, TypeError, ValueError):
            errors.append("视频文件身份在校验过程中发生变化，请保留候选文件并重试")

    result = VideoValidation(
        ok=not errors,
        errors=errors,
        width=width,
        height=height,
        duration=duration,
        decoder_backend=backend,
        file_size=file_size,
        sha256=digest,
        audio_status=audio_status,
        audio_duration_seconds=audio_duration_seconds,
        audio_ok=audio_ok,
        audio_silent=audio_silent,
        audio_max_db=audio_max_db,
        audio_silence_method=audio_silence_method,
    )
    if write_report and report_allowed:
        _write_report(video_path.parent / _REPORT_NAME, result.to_dict())
    return result


def _opencv_decoder(path: Path) -> object:
    return cv2.VideoCapture(os.fspath(path))


def _positive_integer(value: object) -> int | None:
    if not isinstance(value, (int, float, np.integer, np.floating)) or isinstance(value, (bool, np.bool_)):
        return None
    number = float(value)
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        return None
    return int(number)


def _positive_float(value: object) -> float | None:
    if not isinstance(value, (int, float, np.integer, np.floating)) or isinstance(value, (bool, np.bool_)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _target_frames(frame_count: int) -> tuple[tuple[str, int], ...]:
    return (
        ("首帧", 0),
        ("中间帧", (frame_count - 1) // 2),
        ("末帧", frame_count - 1),
    )


def _decode_all_frames(capture: object, frame_count: int, width: int, height: int, errors: list[str]) -> None:
    try:
        if capture.set(cv2.CAP_PROP_POS_FRAMES, 0) is not True or not _frame_position_matches(capture, 0):
            errors.append("视频无法从头开始完整解码")
            return
        for frame_index in range(frame_count):
            success, frame = capture.read()
            if success is not True or not _valid_frame(frame, width, height):
                errors.append("视频存在无法完整解码的帧或帧尺寸无效")
                return
            if not _frame_position_matches(capture, frame_index + 1):
                errors.append("视频完整解码时帧位置不连续")
                return
        success, _ = capture.read()
        if success is True:
            errors.append("视频实际帧数超过声明帧数，无法完整解码")
    except Exception:
        errors.append("视频无法完整解码")


def _valid_frame(frame: object, width: int, height: int) -> bool:
    if not isinstance(frame, np.ndarray) or frame.ndim < 2 or frame.ndim > 3:
        return False
    if frame.shape[0] <= 0 or frame.shape[1] <= 0:
        return False
    if frame.ndim == 3 and frame.shape[2] <= 0:
        return False
    return int(frame.shape[1]) == width and int(frame.shape[0]) == height


def _frame_position_matches(capture: object, expected: int) -> bool:
    try:
        value = capture.get(cv2.CAP_PROP_POS_FRAMES)
        if not isinstance(value, (int, float, np.integer, np.floating)) or isinstance(
            value, (bool, np.bool_)
        ):
            return False
        position = float(value)
        return math.isfinite(position) and abs(position - expected) <= _FRAME_POSITION_TOLERANCE
    except Exception:
        return False


def _identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
    )


def _regular_file_identity(path: Path) -> tuple[tuple[int, int, int, int], os.stat_result]:
    _reject_reparse_components(path)
    metadata = path.lstat()
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise OSError("not a local ordinary file")
    return _identity(metadata), metadata


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_ATTRIBUTE)


def _reject_reparse_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(metadata):
            raise OSError("reparse component")


def _is_network_path(path: Path) -> bool:
    value = os.path.abspath(os.fspath(path))
    return value.startswith("\\\\") or value.startswith("//")


def _sha256_file(path: Path, expected_identity: tuple[int, int, int, int] | None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        before = os.fstat(stream.fileno())
        if expected_identity is not None and _identity(before) != expected_identity:
            raise OSError("file identity changed")
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
        if _identity(after) != _identity(before):
            raise OSError("file identity changed")
    return digest.hexdigest()


def _write_report(path: Path, report: dict[str, object]) -> None:
    try:
        _reject_reparse_components(path)
        payload = (json.dumps(report, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
        if path.exists():
            if _read_existing_report(path, len(payload)) != payload:
                raise VideoValidationError("现有校验报告内容不同，已保留原报告")
            return

        descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_temp)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            _reject_reparse_components(path)
            try:
                if os.name == "nt":
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
                    temporary.unlink()
            except FileExistsError:
                if _read_existing_report(path, len(payload)) != payload:
                    raise VideoValidationError("现有校验报告内容不同，已保留原报告") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
    except VideoValidationError:
        raise
    except Exception:
        raise VideoValidationError("无法安全写入视频校验报告") from None


def _read_existing_report(path: Path, expected_size: int) -> bytes:
    try:
        expected_identity, _ = _regular_file_identity(path)
        with path.open("rb") as stream:
            opened_identity = _identity(os.fstat(stream.fileno()))
            if opened_identity != expected_identity:
                raise OSError("report identity changed")
            payload = stream.read(expected_size + 1)
            final_stream_identity = _identity(os.fstat(stream.fileno()))
        final_path_identity, _ = _regular_file_identity(path)
        if final_stream_identity != expected_identity or final_path_identity != expected_identity:
            raise OSError("report identity changed")
        return payload
    except Exception:
        raise VideoValidationError("现有校验报告不是稳定的普通文件，已保留") from None
