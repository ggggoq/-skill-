"""交付前声音闸门：逐条复核成片的音轨是否真正覆盖整段视频。

用法：
    python tools/check_audio.py 02_待审核/MiniMax/YF95402255/20261109
    python tools/check_audio.py YF95402255            # 按款号扫全部已交付目录
    python tools/check_audio.py 某条.mp4 --json

退出码 1 表示至少有一条不合格，禁止交付。

为什么需要它：``media_audio.validate_audio`` 原本已能比对音轨与视频时长，
但整体 mean/max 电平对"前 5 秒有声、后 5 秒全静"完全不敏感（旧片整体仍有
-24 dB）。本工具把逐秒电平表一并打出来，让"哪一秒开始没声音"一眼可见。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from kling_workflow.media_audio import (  # noqa: E402
    _SEGMENT_WINDOW_SECONDS,
    resolve_ffmpeg,
    validate_audio,
)

DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def video_duration(path: Path, binary: str | None) -> float | None:
    """Return the container duration in seconds using ffmpeg's banner."""
    if binary is None:
        return None
    try:
        completed = subprocess.run(
            [binary, "-hide_banner", "-i", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = DURATION_RE.search(completed.stderr or "")
    if match is None:
        return None
    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def collect(targets: list[str]) -> list[Path]:
    """Expand款号 / 目录 / 文件参数 into concrete mp4 paths."""
    found: list[Path] = []
    for raw in targets:
        candidate = Path(raw)
        if candidate.is_file():
            found.append(candidate)
            continue
        for base in (candidate, ROOT / raw, ROOT / "02_待审核" / "MiniMax" / raw):
            if base.is_dir():
                found.extend(sorted(p for p in base.rglob("*.mp4") if "_记录" not in p.parts))
                break
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in found:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def render_levels(levels: list[float | None] | None) -> str:
    if not levels:
        return "（无逐秒数据）"
    cells = []
    for index, level in enumerate(levels):
        mark = f"{index * _SEGMENT_WINDOW_SECONDS:.0f}s"
        cells.append(f"{mark}:静音" if level is None else f"{mark}:{level:.0f}")
    return " ".join(cells)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成片音轨逐秒复核")
    parser.add_argument("targets", nargs="+", help="款号、目录或 mp4 文件")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--expected", type=float, default=None, help="强制指定期望时长")
    args = parser.parse_args(argv)

    binary = resolve_ffmpeg()
    if binary is None:
        print("FAIL 未找到 ffmpeg：请在 04系统配置/ffmpeg路径.txt 写入绝对路径", file=sys.stderr)
        return 2
    print(f"ffmpeg: {binary}")

    files = collect(args.targets)
    if not files:
        print("FAIL 未找到任何 mp4", file=sys.stderr)
        return 2

    results = []
    failures = 0
    for path in files:
        duration = args.expected if args.expected is not None else video_duration(path, binary)
        report = validate_audio(path, expected_duration=duration)
        ok = bool(report.get("audio_ok"))
        failures += 0 if ok else 1
        results.append({"file": str(path), "ok": ok, "report": report})
        try:
            shown = path.relative_to(ROOT)
        except ValueError:
            shown = path
        audio_duration = report.get("audio_duration_seconds")
        print(
            f"[{'PASS' if ok else 'FAIL'}] {shown} | "
            f"音轨 {audio_duration:.2f}s / 视频 {duration if duration is not None else -1:.2f}s | "
            f"mean {report.get('audio_mean_db')} dB"
        )
        print(f"        {render_levels(report.get('audio_segment_db'))}")
        for error in report.get("audio_errors") or []:
            print(f"        - {error}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n合计 {len(files)} 条，合格 {len(files) - failures} 条，不合格 {failures} 条")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
