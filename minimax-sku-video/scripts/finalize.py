#!/usr/bin/env python3
"""收口：把供应商原片裁到 240 帧 / 10.000 s，并把音轨换成「参考动作片同一 10 秒窗口的原声」。

两个约定：
  · 供应商回的是 10.13 s / 243 帧，内部校验写死 ==10.000 s，所以必须本地裁；
    这不是失败，也没重复扣费。
  · BGM 不用平台随机配的那条，也不用别款号的音乐，取该组参考动作视频的同一个 10 秒窗口
    原声，起点 +23.2 ms 抵消 mp4 muxer 的 aac priming；末尾用 apad 补满，
    否则音轨会比视频短 20–200 ms（音画不同步的报警源）。

用法：
  python scripts/finalize.py --sku KF96467710 --group 1 --version v001
  python scripts/finalize.py --sku KF96467710 --group 2 --version v005 --start 2.0
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

AUD_OFFSET = 0.0232
AUD_BITRATE = "320k"
FRAMES = 240


def probe(ff: str, p: Path) -> str:
    out = subprocess.run([ff, "-hide_banner", "-i", str(p)], capture_output=True, text=True).stderr
    keep = [l.strip() for l in out.splitlines() if "Duration" in l or "Stream #0:0" in l or "Stream #0:1" in l]
    return " | ".join(keep)


def main() -> int:
    ap = argparse.ArgumentParser(description="裁 240 帧 + 换参考原声")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--group", type=int, required=True)
    ap.add_argument("--version", required=True, help="交付版本号，例如 v001；重出时必须改")
    ap.add_argument("--start", type=float, help="动作窗口起点（默认取配置里的 window_start）")
    ap.add_argument("--raw", help="原片文件名（默认 <版本>_raw.mp4）")
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    paths = common.project_paths(root)
    ff = common.ffmpeg(root)
    grp = common.get_group(cfg, args.group)
    start = float(args.start if args.start is not None else grp["window_start"])

    day = paths["delivery"] / args.sku / cfg["batch"]
    raw = day / "_记录" / "供应商原片" / (args.raw or f"{args.version}_raw.mp4")
    if not raw.is_file():
        raise SystemExit(f"缺少供应商原片：{raw}（先跑 poll.py --download）")
    src = paths["materials"] / args.sku / grp["source_group"] / grp["assets"]["video"]
    if not src.is_file():
        raise SystemExit(f"缺少参考动作片：{src}")

    out_dir = day / "00_直接看成片"
    out_dir.mkdir(parents=True, exist_ok=True)
    dst = out_dir / f"{args.version}.mp4"
    dst.unlink(missing_ok=True)
    astart = start + AUD_OFFSET
    subprocess.run(
        [ff, "-v", "error", "-y",
         "-i", str(raw),
         "-ss", f"{astart:.4f}", "-t", "10.2", "-i", str(src),
         "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-pix_fmt", "yuv420p",
         "-vf", "fps=24,setpts=PTS-STARTPTS",
         "-af", "asetpts=PTS-STARTPTS,apad",
         "-c:a", "aac", "-b:a", AUD_BITRATE, "-ar", "44100",
         "-t", "10.000", "-avoid_negative_ts", "make_zero",
         "-movflags", "+faststart", str(dst)], check=True)
    print(f"{args.version}: {dst}")
    print(f"  BGM 窗口 {astart:.4f}–{astart + 10:.4f}s（视频窗口 {start:.2f} 起，"
          f"+{AUD_OFFSET*1000:.1f} ms priming 补偿）")
    print("  " + probe(ff, dst))
    frames = subprocess.run([ff, "-hide_banner", "-i", str(dst), "-map", "0:v", "-c", "copy",
                             "-f", "null", "-"], capture_output=True, text=True).stderr
    if f"frame=  {FRAMES}" not in frames and "frame=" in frames:
        tail = frames.strip().splitlines()[-1][-80:]
        print(f"  ! 帧数核对：{tail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
