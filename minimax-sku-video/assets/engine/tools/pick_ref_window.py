"""按姿态速度挑选参考视频切片窗口（§6.7 硬规则的可执行实现）。

用法（需要带 mediapipe 的解释器，本项目为 F:/jp_tmp_frames/venv_pose/Scripts/python.exe）：

    venv_pose/Scripts/python.exe tools/pick_ref_window.py --video "路径/动作参考_01.mp4"
    ... --window 4.9 --top 3 --json out.json

输出：排名前 N 的 4.9 秒窗口（起止秒、姿态速度总和），以及 0.5 秒粒度的速度曲线，
用于判断"某窗口是否含切镜/特写"时的人工复核位置。
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

KEY = [11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]
L_HIP, R_HIP, L_SHO, R_SHO = 23, 24, 11, 12


def which_ffmpeg() -> str:
    for cand in (shutil.which("ffmpeg"), r"D:/ShadowBot/shadowbot-6.3.39/ffmpeg.exe"):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit("找不到 ffmpeg：请把 ffmpeg 加入 PATH 或用 --ffmpeg 指定")


def extract(video: Path, work: Path, fps: int) -> None:
    subprocess.run([which_ffmpeg(), "-v", "error", "-y", "-i", str(video),
                    "-vf", f"fps={fps},scale=-2:720", "-q:v", "2", str(work / "%04d.jpg")],
                   check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True)
    ap.add_argument("--window", type=float, default=4.9)
    ap.add_argument("--fps", type=int, default=10)
    ap.add_argument("--top", type=int, default=3)
    ap.add_argument("--json")
    args = ap.parse_args()

    try:
        import mediapipe as mp
        import numpy as np
        from mediapipe.tasks import python as mpp
        from mediapipe.tasks.python import vision
        from PIL import Image
    except ModuleNotFoundError as exc:
        print(f"缺少依赖 {exc.name}：请用带 mediapipe 的解释器运行，例如\n"
              r"  F:/jp_tmp_frames/venv_pose/Scripts/python.exe tools/pick_ref_window.py --video <路径>",
              file=sys.stderr)
        return 2

    video = Path(args.video)
    if not video.exists():
        print(f"视频不存在：{video}", file=sys.stderr)
        return 2

    model = Path(r"F:/jp_tmp_frames/models/pose_landmarker_lite.task")
    if not model.exists():
        print("缺少姿态模型 pose_landmarker_lite.task", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        work = Path(tmp)
        extract(video, work, args.fps)
        frames = sorted(work.glob("*.jpg"))
        if not frames:
            print("抽帧为空", file=sys.stderr)
            return 1
        with Image.open(frames[0]) as probe:
            aspect = probe.width / probe.height

        opts = vision.PoseLandmarkerOptions(
            base_options=mpp.BaseOptions(model_asset_path=str(model)),
            running_mode=vision.RunningMode.VIDEO, num_poses=1,
            min_pose_detection_confidence=0.25, min_pose_presence_confidence=0.25,
            min_tracking_confidence=0.25)
        poses = []
        with vision.PoseLandmarker.create_from_options(opts) as lm:
            for i, f in enumerate(frames):
                with Image.open(f) as im:
                    arr = np.asarray(im.convert("RGB"))
                res = lm.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=arr), i * 1000 // args.fps)
                if not res.pose_landmarks:
                    poses.append(None)
                    continue
                pts = np.array([[p.x, p.y] for p in res.pose_landmarks[0]], dtype=float)
                pts[:, 0] *= aspect  # x 按宽、y 按高分别归一化，先还原等比
                poses.append(pts)

    def unit(p):
        o = (p[L_HIP] + p[R_HIP]) / 2
        s = float(np.linalg.norm((p[L_SHO] + p[R_SHO]) / 2 - o))
        return None if s < 1e-6 else (p - o) / s

    norm = [unit(p) if p is not None else None for p in poses]
    speed = [0.0]
    for a, b in zip(norm, norm[1:]):
        if a is None or b is None:
            speed.append(0.0)
        else:
            speed.append(float(np.linalg.norm(a[KEY] - b[KEY], axis=1).mean()))

    n = max(1, int(round(args.window * args.fps)))
    if len(speed) < n:
        print(f"视频只有 {len(speed) / args.fps:.2f} 秒，不足一个 {args.window} 秒窗口", file=sys.stderr)
        return 1
    totals = [(sum(speed[i:i + n]), i) for i in range(0, len(speed) - n + 1)]
    totals.sort(key=lambda x: (-x[0], x[1]))  # 总和降序，并列取更靠前
    valid_rate = sum(1 for x in norm if x is not None) / max(1, len(norm))

    tops = []
    seen: list[int] = []
    for total, i in totals:
        if any(abs(i - j) < n // 2 for j in seen):  # 跳过与已选窗口重叠过半的
            continue
        seen.append(i)
        tops.append({"start_s": round(i / args.fps, 2),
                     "end_s": round((i + n - 1) / args.fps, 2),
                     "speed_sum": round(total, 3),
                     "speed_mean": round(total / n, 4)})
        if len(tops) >= args.top:
            break

    curve = [{"t_s": round(i / args.fps, 1), "v": round(sum(speed[i:i + args.fps]) / args.fps, 4)}
             for i in range(0, len(speed) - args.fps + 1, args.fps)]

    result = {"video": str(video), "fps": args.fps, "window_s": args.window,
              "duration_s": round(len(speed) / args.fps, 2),
              "detect_rate": round(valid_rate, 3), "top_windows": tops, "speed_curve": curve}
    print(json.dumps(result, ensure_ascii=False, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    if valid_rate < 0.8:
        print(f"警告：关键点检出率只有 {valid_rate:.0%}，该窗口选择不可靠，需人工复核", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
