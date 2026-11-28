#!/usr/bin/env python3
"""姿态序列 DTW 比对：时间规整后再算关节距离，消除动作节奏差异。

为什么需要本工具：逐帧对齐比对（帧序相同才算距离）会被生成模型的动作重排污染——
同一套动作只要前后挪了 0.3 秒，距离就虚高。DTW 允许时间轴弹性伸缩，
只比较「动作序列本身」是否一致。

用法（必须用带 mediapipe 的姿态环境）：
    /f/jp_tmp_frames/venv_pose/Scripts/python.exe tools/pose_dtw_compare.py \
        --a 成片.mp4 --b 源片.mp4 --b-ss 2.6 --b-t 4.9

输出：归一化 DTW 距离（体长单位，越小越像）、规整路径长度、逐帧匹配明细。
判读参考（同一批素材内相对比较，不要当绝对阈值用）：
    < 0.15  几乎同一套动作
    0.15-0.30  同一动作族，细节有差
    > 0.30  动作不一致
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

FF = r"D:/ShadowBot/shadowbot-6.3.39/ffmpeg.exe"
MODEL = r"F:/jp_tmp_frames/models/pose_landmarker_lite.task"
FPS = 10
MIN_VIS = 0.5
MIN_JOINTS = 6


def extract_frames(video: Path, work: Path, ss: float | None, t: float | None) -> list[Path]:
    cmd = [FF, "-v", "error", "-y"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.3f}"]
    if t is not None:
        cmd += ["-t", f"{t:.3f}"]
    cmd += ["-i", str(video), "-vf", f"fps={FPS},scale=720:-1", str(work / "f_%05d.jpg")]
    subprocess.run(cmd, check=True)
    files = sorted(work.glob("f_*.jpg"))
    if not files:
        raise SystemExit(f"{video}: 抽帧为空")
    return files


def load_poses(files: list[Path]) -> list:
    import cv2
    import mediapipe as mp
    import numpy as np
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision

    opts = vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=MODEL),
        running_mode=vision.RunningMode.IMAGE, num_poses=1,
    )
    out = []
    with vision.PoseLandmarker.create_from_options(opts) as lm:
        for f in files:
            img = cv2.imread(str(f))
            if img is None:
                out.append(None)
                continue
            res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                     data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
            if not res.pose_landmarks:
                out.append(None)
                continue
            p = np.array([[l.x, l.y, l.visibility] for l in res.pose_landmarks[0]], dtype=np.float32)
            hip = (p[23, :2] + p[24, :2]) / 2
            sh = (p[11, :2] + p[12, :2]) / 2
            scale = float(np.linalg.norm(sh - hip))
            if scale < 1e-6:
                scale = float(np.ptp(p[:, 1])) or 1.0
            q = (p[:, :2] - hip) / scale
            out.append(np.column_stack([q, p[:, 2]]))
    return out


def frame_cost(a, b) -> float | None:
    import numpy as np
    if a is None or b is None:
        return None
    vis = (a[:, 2] > MIN_VIS) & (b[:, 2] > MIN_VIS)
    if vis.sum() < MIN_JOINTS:
        return None
    return float(np.linalg.norm(a[vis, :2] - b[vis, :2], axis=1).mean())


def dtw(seq_a: list, seq_b: list) -> dict:
    """返回归一化 DTW 距离与规整路径。缺检测帧用相邻继承值填补，避免整段比对失败。"""
    import numpy as np
    a = [x for x in seq_a if x is not None]
    b = [x for x in seq_b if x is not None]
    if len(a) < 5 or len(b) < 5:
        raise SystemExit(f"有效姿态帧太少：A={len(a)} B={len(b)}，无法比对")
    n, m = len(a), len(b)
    INF = float("inf")
    D = np.full((n + 1, m + 1), INF, dtype=np.float64)
    D[0, 0] = 0.0
    cost = np.zeros((n, m), dtype=np.float64)
    for i in range(n):
        for j in range(m):
            c = frame_cost(a[i], b[j])
            if c is None:
                c = 0.5  # 检测失败给中性代价，不偏袒任何一侧
            cost[i, j] = c
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            D[i, j] = cost[i - 1, j - 1] + min(D[i - 1, j - 1], D[i - 1, j], D[i, j - 1])
    i, j = n, m
    path = []
    while i > 0 and j > 0:
        path.append((i - 1, j - 1))
        step = min((D[i - 1, j - 1], (i - 1, j - 1)),
                   (D[i - 1, j], (i - 1, j)),
                   (D[i, j - 1], (i, j - 1)))[1]
        i, j = step
    path.reverse()
    total = float(D[n, m])
    path_costs = [cost[p] for p in path]
    return {
        "frames_a": n, "frames_b": m, "path_length": len(path),
        "dtw_normalized": round(total / len(path), 4),
        "path_cost_mean": round(float(np.mean(path_costs)), 4),
        "path_cost_median": round(float(np.median(path_costs)), 4),
        "path_cost_max": round(float(np.max(path_costs)), 4),
        "path": path,
    }


def verdict(v: float) -> str:
    if v < 0.15:
        return "几乎同一套动作"
    if v < 0.30:
        return "同一动作族，细节有差"
    return "动作不一致"


def main() -> int:
    ap = argparse.ArgumentParser(description="姿态序列 DTW 比对（时间规整）")
    ap.add_argument("--a", required=True, help="视频 A")
    ap.add_argument("--b", required=True, help="视频 B")
    ap.add_argument("--a-ss", type=float, default=None, help="A 起点秒")
    ap.add_argument("--a-t", type=float, default=None, help="A 时长秒")
    ap.add_argument("--b-ss", type=float, default=None, help="B 起点秒")
    ap.add_argument("--b-t", type=float, default=None, help="B 时长秒")
    ap.add_argument("--json-out", default=None, help="结果写盘路径")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as d1, tempfile.TemporaryDirectory() as d2:
        fa = extract_frames(Path(args.a), Path(d1), args.a_ss, args.a_t)
        fb = extract_frames(Path(args.b), Path(d2), args.b_ss, args.b_t)
        pa = load_poses(fa)
        pb = load_poses(fb)
    r = dtw(pa, pb)
    r["a"], r["b"] = args.a, args.b
    r["verdict"] = verdict(r["dtw_normalized"])
    print(f"A = {args.a}" + (f" [{args.a_ss}s +{args.a_t}s]" if args.a_ss is not None else ""))
    print(f"B = {args.b}" + (f" [{args.b_ss}s +{args.b_t}s]" if args.b_ss is not None else ""))
    print(f"有效帧 A={r['frames_a']} B={r['frames_b']} | DTW 路径长 {r['path_length']}")
    print(f"归一化 DTW 距离 = {r['dtw_normalized']:.4f} 体长单位")
    print(f"路径代价：均 {r['path_cost_mean']:.4f} / 中位 {r['path_cost_median']:.4f} / 最大 {r['path_cost_max']:.4f}")
    print(f"判读：{r['verdict']}")
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        slim = {k: v for k, v in r.items() if k != "path"}
        out.write_text(json.dumps(slim, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"-> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
