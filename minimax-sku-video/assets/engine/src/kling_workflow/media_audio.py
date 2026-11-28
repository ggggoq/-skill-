"""Small, dependency-free audio-track probes and digital-silence gates for local MP4 files.

Two layers are provided on purpose:

* ``probe_audio`` answers "is there an audio track at all" by reading the MP4
  box tree.  A digitally silent track still has a ``soun`` handler, so this
  layer alone cannot reject silent output.
* ``measure_audio`` answers "does that track actually contain sound".  When a
  local ``ffmpeg`` binary is configured it measures real loudness; otherwise it
  falls back to container-level sample statistics, which is what caught the
  silent ``YF95402010/v002`` candidate.
"""

from __future__ import annotations

import mmap
import os
import re
import shutil
import struct
import subprocess
from dataclasses import dataclass
from pathlib import Path


# A decoded track quieter than this is treated as digital silence.  Reference
# material and delivered clips sit above -22 dB, so -60 dB leaves a wide margin.
SILENCE_DB_THRESHOLD = -60.0
# Container fallback: encoded silence compresses to a near-constant frame size.
# Measured samples: silent file = 2 distinct sizes / 473 frames (0.004),
# audible files = 127-239 distinct sizes / 649 frames (0.196+).
_SILENCE_SIZE_DIVERSITY = 0.02
_SILENCE_MIN_SAMPLES = 100
_FFMPEG_TIMEOUT_SECONDS = 180

# Delivered clips must carry an audible, continuously running music bed.
# 0 dB is the digital full-scale ceiling, so a literal "must not be negative"
# rule would demand clipping; -20 dB peak / -45 dB mean sits far above digital
# silence (-91 dB) while staying below any real music master.
AUDIO_MIN_PEAK_DB = -20.0
AUDIO_MIN_MEAN_DB = -45.0
# A music bed that drops out for longer than this inside the clip is rejected.
_SILENCE_GAP_NOISE_DB = -50.0
_SILENCE_GAP_MIN_SECONDS = 0.8
# Whole-clip mean level hides a truncated track: a clip whose audio stops at
# 4.9 s still averages -24 dB over the audible half.  Every 1 s segment must
# therefore clear this floor on its own.  Measured deliveries sit near -25 dB,
# so -50 dB flags a dead segment without punishing a quiet room tone.
AUDIO_MIN_SEGMENT_DB = -50.0
_SEGMENT_WINDOW_SECONDS = 1.0
_SEGMENT_SAMPLE_RATE = 8000
_PCM_FORMATS = frozenset({b"sowt", b"twos", b"lpcm", b"in24", b"in32", b"fl32", b"fl64", b"raw "})
_MEAN_DB = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")
_MAX_DB = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB")


@dataclass(frozen=True)
class AudioProbe:
    status: str
    duration_seconds: float | None = None


@dataclass(frozen=True)
class AudioMeasurement:
    """Decoded-or-inferred loudness evidence for one audio track."""

    status: str
    duration_seconds: float | None = None
    method: str = "none"
    mean_db: float | None = None
    max_db: float | None = None
    sample_count: int | None = None
    distinct_sample_sizes: int | None = None
    median_sample_size: int | None = None
    silent: bool | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "audio_status": self.status,
            "audio_duration_seconds": self.duration_seconds,
            "audio_silence_method": self.method,
            "audio_mean_db": self.mean_db,
            "audio_max_db": self.max_db,
            "audio_sample_count": self.sample_count,
            "audio_distinct_sample_sizes": self.distinct_sample_sizes,
            "audio_median_sample_size": self.median_sample_size,
            "audio_silent": self.silent,
        }


def mcp_audio_arguments(policy: str) -> dict[str, object]:
    """Return the connected Kling MCP audio flags without exposing assets."""
    if policy == "preserve_original":
        return {"enable_audio": False, "keepOriginalSound": True}
    if policy == "generate_music":
        return {"enable_audio": True, "keepOriginalSound": False}
    raise ValueError("音频策略无效")


def probe_audio(path: str | Path) -> AudioProbe:
    """Return ``present``, ``absent`` or ``unknown`` without decoding video frames."""
    try:
        source = Path(path)
        with source.open("rb") as stream:
            if stream.seek(0, 2) <= 0:
                return AudioProbe("unknown")
            with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
                top_level = tuple(_boxes(data, 0, len(data)))
                moov = next((box for box in top_level if box[0] == b"moov"), None)
                if moov is None:
                    return AudioProbe("unknown")
                durations: list[float] = []
                for trak in _children(data, moov[1], moov[2], b"trak"):
                    mdia = next(_children(data, trak[1], trak[2], b"mdia"), None)
                    if mdia is None:
                        continue
                    handler = next(_children(data, mdia[1], mdia[2], b"hdlr"), None)
                    if handler is None or _handler_type(data, handler[1], handler[2]) != b"soun":
                        continue
                    mdhd = next(_children(data, mdia[1], mdia[2], b"mdhd"), None)
                    duration = _media_duration(data, mdhd) if mdhd else None
                    if duration is not None:
                        durations.append(duration)
                    else:
                        durations.append(0.0)
                if durations:
                    positive = [value for value in durations if value > 0]
                    return AudioProbe("present", max(positive) if positive else None)
                return AudioProbe("absent")
    except (OSError, ValueError, OverflowError, struct.error):
        return AudioProbe("unknown")


def measure_audio(path: str | Path, *, ffmpeg: str | None = None) -> AudioMeasurement:
    """Measure whether a local MP4 carries audible sound, not just a ``soun`` box."""
    probe = probe_audio(path)
    if probe.status == "absent":
        return AudioMeasurement("absent", probe.duration_seconds, "none", silent=True)
    if probe.status != "present":
        return AudioMeasurement("unknown", None, "none", silent=None)
    try:
        source = Path(path)
        binary = resolve_ffmpeg(ffmpeg)
        if binary is not None:
            decoded = _measure_with_ffmpeg(source, binary)
            if decoded is not None:
                mean_db, max_db = decoded
                return AudioMeasurement(
                    "present",
                    probe.duration_seconds,
                    "ffmpeg",
                    mean_db=mean_db,
                    max_db=max_db,
                    silent=max_db <= SILENCE_DB_THRESHOLD,
                )
        track = _read_audio_track(source)
    except (OSError, ValueError, OverflowError, struct.error):
        return AudioMeasurement("present", probe.duration_seconds, "none", silent=None)
    if track is None:
        return AudioMeasurement("present", probe.duration_seconds, "none", silent=None)
    sizes, payloads, audio_format = track
    if not sizes:
        return AudioMeasurement("present", probe.duration_seconds, "none", silent=None)
    median = sorted(sizes)[len(sizes) // 2]
    distinct = len(set(sizes))
    if audio_format in _PCM_FORMATS:
        silent: bool | None = None
        if payloads:
            flat = b"".join(payloads)
            silent = len(set(flat)) == 1
        return AudioMeasurement(
            "present",
            probe.duration_seconds,
            "container-pcm",
            sample_count=len(sizes),
            distinct_sample_sizes=distinct,
            median_sample_size=median,
            silent=silent,
        )
    if audio_format is not None and not _is_compressed_audio(audio_format):
        return AudioMeasurement(
            "present",
            probe.duration_seconds,
            "none",
            sample_count=len(sizes),
            distinct_sample_sizes=distinct,
            median_sample_size=median,
            silent=None,
        )
    silent = None
    if len(sizes) >= _SILENCE_MIN_SAMPLES:
        silent = distinct / len(sizes) <= _SILENCE_SIZE_DIVERSITY
    return AudioMeasurement(
        "present",
        probe.duration_seconds,
        "container-sizes",
        sample_count=len(sizes),
        distinct_sample_sizes=distinct,
        median_sample_size=median,
        silent=silent,
    )


def resolve_ffmpeg(explicit: str | None = None) -> str | None:
    """Return an ffmpeg binary path from an argument, env var, project config or PATH."""
    candidates: list[str] = []
    if isinstance(explicit, str) and explicit.strip():
        candidates.append(explicit.strip())
    env_value = os.environ.get("KLING_FFMPEG")
    if env_value:
        candidates.append(env_value.strip())
    config = Path(__file__).resolve().parents[2] / "04系统配置" / "ffmpeg路径.txt"
    try:
        if config.is_file():
            first = config.read_text(encoding="utf-8").splitlines()
            if first and first[0].strip():
                candidates.append(first[0].strip())
    except (OSError, UnicodeError):
        pass
    found = shutil.which("ffmpeg")
    if found:
        candidates.append(found)
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    return None


def _silence_gaps(path: str | Path, ffmpeg: str | None = None) -> list[tuple[float, float]] | None:
    """Return decoded silence intervals, or None when ffmpeg is unavailable."""
    binary = resolve_ffmpeg(ffmpeg)
    if binary is None:
        return None
    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-af",
        f"silencedetect=noise={_SILENCE_GAP_NOISE_DB}dB:d={_SILENCE_GAP_MIN_SECONDS}",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stderr or ""
    starts = [float(value) for value in re.findall(r"silence_start:\s*(-?\d+(?:\.\d+)?)", text)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*(-?\d+(?:\.\d+)?)", text)]
    gaps: list[tuple[float, float | None]] = []
    for index, start in enumerate(starts):
        # A silence that never ends before the track runs out emits no
        # ``silence_end`` line.  Dropping those entries is exactly how a track
        # truncated at 4.9 s used to pass: the whole tail was an open-ended gap.
        end = ends[index] if index < len(ends) else None
        gaps.append((round(max(start, 0.0), 3), round(end, 3) if end is not None else None))
    return gaps


def _segment_levels(
    path: str | Path, ffmpeg: str | None = None, window: float = _SEGMENT_WINDOW_SECONDS
) -> list[float | None] | None:
    """Return per-window RMS levels in dB, or None when ffmpeg is unavailable.

    Whole-file loudness cannot prove the sound lasts to the last frame; this
    returns one number per second so a silent tail is visible as trailing
    ``None``/very low entries.
    """
    binary = resolve_ffmpeg(ffmpeg)
    if binary is None:
        return None
    rate = _SEGMENT_SAMPLE_RATE
    samples = max(1, int(round(rate * window)))
    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        (
            f"aresample={rate},asetnsamples=n={samples},"
            "astats=metadata=1:reset=1,"
            "ametadata=print:key=lavfi.astats.Overall.RMS_level"
        ),
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=_FFMPEG_TIMEOUT_SECONDS, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stderr or ""
    levels: list[float | None] = []
    for raw in re.findall(r"Overall\.RMS_level=(-?inf|-?\d+(?:\.\d+)?)", text):
        if raw.endswith("inf"):
            levels.append(None if raw.startswith("-") else 0.0)
        else:
            levels.append(float(raw))
    return levels or None


def validate_audio(
    path: str | Path,
    expected_duration: float | None = None,
    *,
    ffmpeg: str | None = None,
) -> dict[str, object]:
    """Return a safe audio validation report for a downloaded candidate."""
    measurement = measure_audio(path, ffmpeg=ffmpeg)
    errors: list[str] = []
    if measurement.status == "absent":
        errors.append("成片必须包含音轨；未检测到可用音轨")
    if measurement.status == "present" and measurement.silent is True:
        detail = (
            f"解码实测 max_volume={measurement.max_db:.1f} dB"
            if measurement.max_db is not None
            else f"容器采样统计 {measurement.sample_count} 帧仅 {measurement.distinct_sample_sizes} 种帧长"
        )
        errors.append(
            f"成片音频为数字静音（{detail}）；参考视频无有效音轨时必须改用纯音乐策略重新生成"
        )
    gaps: list[tuple[float, float | None]] | None = None
    segments: list[float | None] | None = None
    if measurement.status == "present" and measurement.silent is not True:
        if measurement.max_db is not None and measurement.max_db < AUDIO_MIN_PEAK_DB:
            errors.append(
                f"成片音频峰值仅 {measurement.max_db:.1f} dB，低于 {AUDIO_MIN_PEAK_DB:.0f} dB 合格线；"
                "必须重新生成贯穿全片的纯音乐"
            )
        if measurement.mean_db is not None and measurement.mean_db < AUDIO_MIN_MEAN_DB:
            errors.append(
                f"成片音频平均电平仅 {measurement.mean_db:.1f} dB，低于 {AUDIO_MIN_MEAN_DB:.0f} dB 合格线；"
                "音频过弱，视为近静音"
            )
        gaps = _silence_gaps(path, ffmpeg)
        if gaps:
            duration = measurement.duration_seconds or 0.0
            # A gap that runs to the last frame is an open-ended `None` end.
            # Both cases mean the sound stopped early, so neither is exempt.
            interior = [
                gap
                for gap in gaps
                if gap[0] > 0.3
                and (gap[1] is None or duration <= 0 or gap[1] >= duration - 0.2)
            ]
            if interior:
                first = interior[0]
                end_text = f"{first[1]:.1f} 秒" if first[1] is not None else "片尾"
                errors.append(
                    f"成片音频从 {first[0]:.1f} 秒起静音直到{end_text}；音轨必须覆盖整段视频"
                )
        segments = _segment_levels(path, ffmpeg)
        if segments:
            span = max(
                measurement.duration_seconds or 0.0,
                float(expected_duration or 0.0),
            )
            dead: list[float] = []
            for index, level in enumerate(segments):
                start = index * _SEGMENT_WINDOW_SECONDS
                # Ignore the final partial window and the first 0.2 s, where an
                # AAC encoder delay can dip before the first real sample.
                if span > 0 and start + _SEGMENT_WINDOW_SECONDS > span - 0.3:
                    continue
                if start < 0.2:
                    continue
                if level is None or level <= AUDIO_MIN_SEGMENT_DB:
                    dead.append(start)
            if dead:
                shown = "、".join(f"{value:.0f}" for value in dead[:8])
                errors.append(
                    f"成片第 {shown} 秒存在整秒无声段（逐秒电平低于 {AUDIO_MIN_SEGMENT_DB:.0f} dB）；"
                    "声音必须在任意一秒都不断"
                )
    if (
        measurement.status == "present"
        and expected_duration is not None
        and measurement.duration_seconds is not None
        and abs(measurement.duration_seconds - expected_duration) > 0.25
    ):
        errors.append(
            "成片音频时长必须与视频时长一致"
            f"（音轨 {measurement.duration_seconds:.2f} 秒 / 视频 {expected_duration:.2f} 秒）；"
            "参考切片只有 2–5 秒，不能直接 -c:a copy 铺 10 秒，"
            "必须从动作源片同起点连续取满成片时长"
        )
    report = measurement.to_dict()
    report.update(
        {
            "audio_errors": errors,
            "audio_ok": not errors,
            "audio_silence_gaps": gaps,
            "audio_segment_db": segments,
            "audio_min_peak_db": AUDIO_MIN_PEAK_DB,
            "audio_min_mean_db": AUDIO_MIN_MEAN_DB,
            "audio_min_segment_db": AUDIO_MIN_SEGMENT_DB,
        }
    )
    return report


def _is_compressed_audio(audio_format: bytes) -> bool:
    return audio_format in {b"mp4a", b"ac-3", b"ec-3", b"Opus", b"alac", b"enca"}


def _measure_with_ffmpeg(source: Path, binary: str) -> tuple[float, float] | None:
    command = [
        binary,
        "-hide_banner",
        "-nostats",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            timeout=_FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = completed.stderr.decode("utf-8", errors="replace")
    mean_match = _MEAN_DB.search(text)
    max_match = _MAX_DB.search(text)
    if not mean_match or not max_match:
        return None
    try:
        return float(mean_match.group(1)), float(max_match.group(1))
    except ValueError:
        return None


def _read_audio_track(
    source: Path,
) -> tuple[list[int], list[bytes], bytes | None] | None:
    with source.open("rb") as stream:
        if stream.seek(0, 2) <= 0:
            return None
        with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            top_level = tuple(_boxes(data, 0, len(data)))
            moov = next((box for box in top_level if box[0] == b"moov"), None)
            if moov is None:
                return None
            for trak in _children(data, moov[1], moov[2], b"trak"):
                mdia = next(_children(data, trak[1], trak[2], b"mdia"), None)
                if mdia is None:
                    continue
                handler = next(_children(data, mdia[1], mdia[2], b"hdlr"), None)
                if handler is None or _handler_type(data, handler[1], handler[2]) != b"soun":
                    continue
                minf = next(_children(data, mdia[1], mdia[2], b"minf"), None)
                if minf is None:
                    continue
                stbl = next(_children(data, minf[1], minf[2], b"stbl"), None)
                if stbl is None:
                    continue
                sizes = _sample_sizes(data, stbl)
                if sizes is None:
                    return None
                audio_format = _sample_format(data, stbl)
                payloads = _first_sample_payloads(data, stbl, sizes, limit=48)
                return sizes, payloads, audio_format
            return None


def _sample_sizes(data: mmap.mmap, stbl: tuple[bytes, int, int]) -> list[int] | None:
    stsz = next(_children(data, stbl[1], stbl[2], b"stsz"), None)
    if stsz is None:
        return None
    _, start, end = stsz
    if end - start < 12:
        return None
    fixed = struct.unpack_from(">I", data, start + 4)[0]
    count = struct.unpack_from(">I", data, start + 8)[0]
    if count <= 0:
        return None
    if fixed:
        return [fixed] * count
    if start + 12 + count * 4 > end:
        return None
    return list(struct.unpack_from(f">{count}I", data, start + 12))


def _sample_format(data: mmap.mmap, stbl: tuple[bytes, int, int]) -> bytes | None:
    stsd = next(_children(data, stbl[1], stbl[2], b"stsd"), None)
    if stsd is None:
        return None
    _, start, end = stsd
    # version/flags (4) + entry_count (4) + entry size (4) + entry format (4)
    if end - start < 20:
        return None
    return bytes(data[start + 12 : start + 16])


def _first_sample_payloads(
    data: mmap.mmap,
    stbl: tuple[bytes, int, int],
    sizes: list[int],
    *,
    limit: int,
) -> list[bytes]:
    chunk_offsets = _chunk_offsets(data, stbl)
    if not chunk_offsets:
        return []
    position = chunk_offsets[0]
    payloads: list[bytes] = []
    for size in sizes[:limit]:
        if size <= 0 or position + size > len(data):
            break
        payloads.append(bytes(data[position : position + size]))
        position += size
    return payloads


def _chunk_offsets(data: mmap.mmap, stbl: tuple[bytes, int, int]) -> list[int]:
    stco = next(_children(data, stbl[1], stbl[2], b"stco"), None)
    if stco is not None:
        _, start, end = stco
        count = struct.unpack_from(">I", data, start + 4)[0] if end - start >= 8 else 0
        if count <= 0 or start + 8 + count * 4 > end:
            return []
        return list(struct.unpack_from(f">{count}I", data, start + 8))
    co64 = next(_children(data, stbl[1], stbl[2], b"co64"), None)
    if co64 is not None:
        _, start, end = co64
        count = struct.unpack_from(">I", data, start + 4)[0] if end - start >= 8 else 0
        if count <= 0 or start + 8 + count * 8 > end:
            return []
        return list(struct.unpack_from(f">{count}Q", data, start + 8))
    return []


def _boxes(data: mmap.mmap, start: int, end: int):
    position = start
    while position + 8 <= end:
        size = struct.unpack_from(">I", data, position)[0]
        kind = bytes(data[position + 4 : position + 8])
        header = 8
        if size == 1:
            if position + 16 > end:
                return
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header = 16
        elif size == 0:
            size = end - position
        if size < header or position + size > end:
            return
        yield kind, position + header, position + size
        position += size


def _children(data: mmap.mmap, start: int, end: int, kind: bytes):
    return (box for box in _boxes(data, start, end) if box[0] == kind)


def _handler_type(data: mmap.mmap, start: int, end: int) -> bytes | None:
    if end - start < 12:
        return None
    return bytes(data[start + 8 : start + 12])


def _media_duration(data: mmap.mmap, box: tuple[bytes, int, int]) -> float | None:
    _, start, end = box
    if end - start < 20:
        return None
    version = data[start]
    if version == 0:
        timescale = struct.unpack_from(">I", data, start + 12)[0]
        duration = struct.unpack_from(">I", data, start + 16)[0]
    elif version == 1 and end - start >= 32:
        timescale = struct.unpack_from(">I", data, start + 20)[0]
        duration = struct.unpack_from(">Q", data, start + 24)[0]
    else:
        return None
    if timescale <= 0 or duration <= 0:
        return None
    return duration / timescale
