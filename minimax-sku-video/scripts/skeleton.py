#!/usr/bin/env python3
"""骨架分镜：把参考动作片里的姿态画成 8 格灰底亮绿骨架，供模型读「人体尺度 + 站姿」。

两种模式：
  detected（默认）  按 config 里 storyboard.anchors 逐个锚点检测，±0.48s 抖动搜帧，
                    过几何闸门（可见度 / 包围盒 / 手臂折叠 / 肘角 / 躯干歪）。姿态有差异。
  fixed            只取一帧人工确认干净的姿态，画满 8 格。检测版出现下肢折叠、
                    双腿交叉 X、头身分离时用这个，是唯一 100% 不会畸形的来源。

为什么要两种：mediapipe 在「白衣配白墙 / 手在脸侧 / 转身背对 / 手举过头」这些姿态上
会判错，而闸门只能拦住一部分。detected 出来的图必须人眼过一遍，见 references/pitfalls.md。

用法：
  python scripts/skeleton.py --sku KF96467710
  python scripts/skeleton.py --sku KF96467710 --group 3 --fixed 3:0.50
  python scripts/skeleton.py --sku KF96467710 --probe 1:0.5,2.0,3.0     # 只看候选骨架
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

try:
    import mediapipe as mp
    from mediapipe.tasks import python as mpp
    from mediapipe.tasks.python import vision
except ImportError:
    raise SystemExit(
        "当前 python 没有 mediapipe。骨架分镜要用带 mediapipe 的解释器跑：\n"
        "  python scripts/init_project.py --root <项目> --venv --install\n"
        "  然后 <项目>/.venv/Scripts/python.exe scripts/skeleton.py ...\n"
        "（mediapipe 目前支持 Python 3.10–3.12）"
    )

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

CANVAS_W, CANVAS_H = 480, 854
BG = (243, 241, 236)
GREEN = (0, 160, 60)
RED = (200, 30, 30)
FILL = (212, 212, 204)
CONN = [
    (0, 1), (1, 2), (2, 3), (3, 7), (0, 4), (4, 5), (5, 6), (6, 8),
    (9, 10), (11, 12), (11, 13), (13, 15), (15, 17), (15, 19), (15, 21),
    (17, 19), (12, 14), (14, 16), (16, 18), (16, 20), (16, 22), (18, 20),
    (11, 23), (12, 24), (23, 24), (23, 25), (24, 26), (25, 27), (26, 28),
    (27, 29), (28, 30), (29, 31), (30, 32), (27, 31), (28, 32),
]
TRUNK = [(11, 12), (11, 23), (12, 24), (23, 24), (11, 13), (13, 15),
         (12, 14), (14, 16), (23, 25), (25, 27), (24, 26), (26, 28)]
KEY = [11, 12, 13, 14, 15, 16, 23, 24, 25, 26, 27, 28]
JITTER = [round(i * 0.04, 2) for i in range(0, 13)] + [round(-i * 0.04, 2) for i in range(1, 13)]
FONT_PATH = r"C:/Windows/Fonts/msyh.ttc"


def font(size: int = 17):
    for p in (FONT_PATH, "/System/Library/Fonts/PingFang.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"):
        if Path(p).is_file():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def quality(lm) -> tuple[float, str]:
    """可见度 + 几何硬闸门。返回 (最低可见度, 问题描述)。"""
    vis = min(lm[j].visibility for j in KEY)
    sh_y = (lm[11].y + lm[12].y) / 2
    hip_y = (lm[23].y + lm[24].y) / 2
    note = ""
    if hip_y - sh_y < 0.05:
        note = "躯干过短"
    elif max(lm[27].y, lm[28].y) < hip_y + 0.10:
        note = "踝不在髋下"
    elif abs((lm[11].x + lm[12].x) / 2 - (lm[23].x + lm[24].x) / 2) / max(hip_y - sh_y, 1e-3) > 0.22:
        note = "躯干歪"
    elif abs(lm[25].y - lm[23].y) < 0.02 or abs(lm[26].y - lm[24].y) < 0.02:
        note = "膝平髋"
    else:
        torso = max(hip_y - sh_y, 1e-3)
        for sh, el, wr in ((11, 13, 15), (12, 14, 16)):
            d_sw = math.hypot(lm[wr].x - lm[sh].x, lm[wr].y - lm[sh].y)
            d_ew = math.hypot(lm[wr].x - lm[el].x, lm[wr].y - lm[el].y)
            ax, ay = lm[sh].x - lm[el].x, lm[sh].y - lm[el].y
            bx, by = lm[wr].x - lm[el].x, lm[wr].y - lm[el].y
            den = max(math.hypot(ax, ay) * math.hypot(bx, by), 1e-9)
            if d_sw < 0.55 * torso or d_ew < 0.30 * torso or (ax * bx + ay * by) / den > 0.64:
                note = "手臂折叠"
                break
    return vis, note


def bbox_h(lm) -> float:
    ys = [lm[j].y for j in KEY]
    return max(ys) - min(ys)


def grab(ff: str, video: Path, t: float, dst: Path) -> None:
    subprocess.run([ff, "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "2", "-y", str(dst)], check=True)


def draw_cells(pts_list, labels, out: Path) -> None:
    pts_all = [p for pts in pts_list if pts for p in pts]
    xs = [p.x for p in pts_all]
    ys = [p.y for p in pts_all]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = max(bx1 - bx0, 1e-3), max(by1 - by0, 1e-3)
    scale = min(CANVAS_W * 0.88 / bw, CANVAS_H * 0.94 / bh)
    offx = (CANVAS_W - bw * scale) / 2 - bx0 * scale
    offy = (CANVAS_H - bh * scale) / 2 - by0 * scale

    cw = CANVAS_W // 2
    ch = int(cw * CANVAS_H / CANVAS_W)
    pad = 26
    sheet = Image.new("RGB", (4 * cw, 2 * (ch + pad)), "white")
    sd = ImageDraw.Draw(sheet)
    f = font(17)
    for i, (pts, label) in enumerate(zip(pts_list, labels)):
        canvas = Image.new("RGB", (CANVAS_W, CANVAS_H), BG)
        d = ImageDraw.Draw(canvas)
        if pts:
            xy = [(p.x * scale + offx, p.y * scale + offy) for p in pts]
            ok = [p.visibility >= 0.35 for p in pts]
            for a_, b_ in TRUNK:
                if ok[a_] and ok[b_]:
                    d.line([xy[a_], xy[b_]], fill=FILL, width=26)
            for a_, b_ in CONN:
                if ok[a_] and ok[b_]:
                    d.line([xy[a_], xy[b_]], fill=GREEN, width=5)
            for (x, y), k in zip(xy, ok):
                if k:
                    d.ellipse([x - 4, y - 4, x + 4, y + 4], fill=RED)
        x0, y0 = (i % 4) * cw, (i // 4) * (ch + pad)
        sheet.paste(canvas.resize((cw - 2, ch), Image.LANCZOS), (x0, y0))
        sd.text((x0 + 4, y0 + ch + 3), label, fill="black", font=f)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=92, optimize=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 8 格骨架分镜")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--group", type=int, action="append", help="只做这一组，可重复")
    ap.add_argument("--fixed", action="append", default=[],
                    help="固定姿态模式，格式 组:秒，例如 --fixed 3:0.5；可重复")
    ap.add_argument("--probe", help="只输出候选骨架条，格式 组:秒1,秒2,...")
    ap.add_argument("--model", help="pose_landmarker_lite.task 路径（默认 <项目>/models/）")
    ap.add_argument("--min-vis", type=float, default=0.78)
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    paths = common.project_paths(root)
    tmp = root / "_check" / args.sku / "pose_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    model = Path(args.model) if args.model else root / "models" / "pose_landmarker_lite.task"
    if not model.is_file():
        raise SystemExit(f"缺姿态模型：{model}\n跑 init_project.py 会自动下载，或 --model 指定。")

    det = vision.PoseLandmarker.create_from_options(vision.PoseLandmarkerOptions(
        base_options=mpp.BaseOptions(model_asset_path=str(model)),
        running_mode=vision.RunningMode.IMAGE, num_poses=1, min_pose_detection_confidence=0.3))
    ff = common.ffmpeg(root)

    fixed = {}
    for spec in args.fixed:
        g, t = spec.split(":")
        fixed[int(g)] = float(t)

    # ---------------------------------------------------------------- 候选条
    if args.probe:
        g_s, times = args.probe.split(":")
        g = int(g_s)
        grp = common.get_group(cfg, g)
        video = paths["materials"] / args.sku / grp["source_group"] / grp["assets"]["video"]
        pts_list, labels = [], []
        for t in [float(x) for x in times.split(",")]:
            f = tmp / f"probe_{g}_{t:.2f}.jpg"
            try:
                grab(ff, video, t, f)
                r = det.detect(mp.Image.create_from_file(str(f)))
            except Exception:
                f.unlink(missing_ok=True)
                continue
            f.unlink(missing_ok=True)
            if r.pose_landmarks:
                pts_list.append(r.pose_landmarks[0])
                labels.append(f"{t:.2f}s")
        if not pts_list:
            print("一张都没检到")
            return 1
        while len(pts_list) % 8:
            pts_list.append(pts_list[-1])
            labels.append(labels[-1])
        out = root / "_check" / args.sku / f"pose_probe_g{g}.jpg"
        draw_cells(pts_list[:8], labels[:8], out)
        print(out)
        return 0

    # ---------------------------------------------------------------- 正式出图
    report = {}
    for grp in cfg["groups"]:
        g = int(grp["group"])
        if args.group and g not in args.group:
            continue
        video = paths["materials"] / args.sku / grp["source_group"] / grp["assets"]["video"]
        start = float(grp["window_start"])
        pack = paths["packs"] / args.sku / f"minimax-G{g}-{grp['version']}-{cfg['batch']}"
        out = pack / "storyboard.jpg"

        if g in fixed or grp.get("storyboard", {}).get("mode") == "fixed":
            t = fixed.get(g) or float(grp["storyboard"].get("frame", 0.0))
            f = tmp / f"fix_{g}_{t:.2f}.jpg"
            grab(ff, video, t, f)
            r = det.detect(mp.Image.create_from_file(str(f)))
            f.unlink(missing_ok=True)
            if not r.pose_landmarks:
                print(f"[G{g}] {t:.2f}s 没检到姿态，换一帧")
                continue
            draw_cells([r.pose_landmarks[0]] * 8, [f"P{i+1}" for i in range(8)], out)
            print(f"[G{g}] 固定姿态 x8（{t:.2f}s）-> {out.relative_to(root)}")
            report[f"G{g}"] = {"mode": "fixed", "frame": t}
            continue

        anchors = grp["storyboard"]["anchors"]
        cand: dict[float, list] = {}
        for a in anchors:
            rows = []
            for off in JITTER:
                t = start + a + off
                if t < 0:
                    continue
                f = tmp / f"j_{g}_{t:.3f}.jpg"
                try:
                    grab(ff, video, t, f)
                    r = det.detect(mp.Image.create_from_file(str(f)))
                except Exception:
                    f.unlink(missing_ok=True)
                    continue
                f.unlink(missing_ok=True)
                if not r.pose_landmarks:
                    continue
                lm = r.pose_landmarks[0]
                vis, note = quality(lm)
                rows.append((t, lm, vis, note, bbox_h(lm)))
            cand[a] = rows

        heights = sorted(r[4] for rows in cand.values() for r in rows if r[2] >= 0.68 and not r[3])
        ref_h = heights[len(heights) // 2] if heights else 0.5
        pts_list, labels, marks = [], [], []
        for a in anchors:
            rows = [r for r in cand[a] if r[4] >= 0.65 * ref_h]
            good = [r for r in rows if r[2] >= args.min_vis and not r[3]]
            pick = min(good, key=lambda r: abs(r[0] - (start + a))) if good else (
                max(rows, key=lambda r: r[2]) if rows else None)
            pts_list.append(pick[1] if pick else None)
            labels.append(f"F{a:.1f} = {(pick[0] - start) if pick else -1:.2f}s"
                          + ("" if pick is None or not pick[3] else f" {pick[3]}"))
            marks.append({"anchor": a, "used": None if pick is None else round(pick[0], 3),
                          "vis": None if pick is None else round(pick[2], 2),
                          "flag": None if pick is None else pick[3]})
        if not any(pts_list):
            print(f"[G{g}] 全部锚点无姿态，改用 --fixed {g}:<秒>")
            continue
        draw_cells(pts_list, labels, out)
        flagged = sum(1 for m in marks if m["flag"] or m["used"] is None)
        print(f"[G{g}] 检测版 -> {out.relative_to(root)}"
              + (f"  ⚠ {flagged}/8 格带问题标记，必须人眼过一遍" if flagged else ""))
        report[f"G{g}"] = {"mode": "detected", "marks": marks}

    (root / "_check" / args.sku / "storyboard_times.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("\n图纸出来后逐组目视：有没有手臂折三角、手臂被拉成直线、腿交叉成 X、单腿悬空、头身分离。")
    print("有畸形就换 --fixed 组:秒（先用 --probe 挑一帧干净的）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
