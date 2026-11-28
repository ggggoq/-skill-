#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_stillness.py — 成片静止段闸门（§6.13）

主管 2026-11-12 硬规则：
「生成的成片不允许有超过两秒保持同一个动作不动，
  比如背对两秒无动作或者正面站立无动作」

用法（只用 ffmpeg + numpy，项目 .venv 即可，不需要 mediapipe）：

    ./.venv/Scripts/python.exe tools/check_stillness.py YF96430410
    ./.venv/Scripts/python.exe tools/check_stillness.py a.mp4 b.mp4
    ... --max-still 2.0 --thr 1.5 --fps 10
    ... --no-edge        # 跳过边缘运动报警，只跑静止闸门

判定口径：
  1. 10 fps 抽帧，缩到 96×96 灰度。
  2. 相邻帧平均绝对差（0–255 灰阶）= 该帧的"动作量"。
  3. 2 秒滑动窗口（20 帧）、0.1 秒步进，窗口内动作量均值 < thr（默认 1.5）
     判该窗口"无动作"。
  4. 连续 k 个窗口无动作 → 无动作段时长 = 2.0 + (k-1)×0.1 秒。
  5. 最长无动作段 > max-still（默认 2.0 s）判不合格。
  6. 退出码：全部通过 0，任一不合格 1，无法分析 2。

为什么用帧差而不是姿态关键点（2026-11-12 实测结论）：
  MediaPipe 手腕/踝部的跟踪抖动与真人微动同量级（0.1–0.2 体长），
  逐点阈值分不清「举着手不动」和「轻微抖动」，会漏判；
  而帧差直接对应"画面上有没有动作"，正好就是本条规则要判的东西。

阈值依据（2026-11-12 双批校准）：
  - 参考片四组（连续动作素材）：最长无动作段 0.0 s，2 秒窗口帧差最小 2.85；
  - 本批成片：v002 3.1 s / v003 2.7 s / v004 2.9 s → 判不合格，
    与主管目视「004 六秒后站定」「002 举着手不走」完全一致；
  - 同款组 1（动作最满）：0.0 s；另一批 YF95460685 已验收五条均为 0.0 s，不误报。

边缘持续运动报警（2026-11-12 主管要求追加）：
  目的：抓「画面边缘有第二人／路人的手／身体入镜」这类违规
        （提示词恒要求 Only she in frame, no second person）。
  做法：**原分辨率裁条带**（不要先缩整帧——96×96 全帧切片会把边缘细节
        平均掉，实测右缘明显运动只测出 1.9，裁条带能测出 8.9）。
        裁左条 0–14%、右条 86–100%、中心带 30–70%，各缩到 24×96 灰度，
        取逐帧帧差：
          ratio = 条带帧差中位 / 中心帧差中位
          持续占比 = (条带帧差 > max(1.5, 中心×1.2)) 的帧占比
        仅当 ratio > 1.2 且 持续占比 > 0.5 时报警——即"边缘比主体更活跃"。
        这样人像满画面（脚/腿进入下缘）不会误报。
  报警只作提示，不改变通过/不合格判定，退出码不受影响。

  实测校准（2026-11-12，同批数据）：
    · 合成入侵片（右缘 115px 黑块 300px/s 上下移动）：右比 1.29、持续 0.58 → 触发；
    · 合成弱运动片（右缘 testsrc2 细密图案）：右比 0.30、持续 0.00 → 不触发（检出力有限）；
    · 干净成片 v003 / v004 / 1(组1)：右比 0.62 / 0.16 / 0.64，持续 ≤0.12 → 不触发；
    · 参考片组2 / 组4：不触发。误报 0。

  已知局限（务必读）：
    本报警只能抓「边缘有东西在大幅动」。2026-11-12 YF96430410 v002 那次
    第二只手是**静止托在画面右缘**，实测右条帧差中位 1.41，
    反而低于干净片 v003 的 4.29 —— 报警不会触发。
    同批试过并失败的替代判据：
      · 边缘/中心帧差比值（v002 0.18 vs v004 0.16，不可分）
      · 边缘某块持续运动占比（人像构图下恒 >0.85，误报）
      · RGB 肤色占比（暖色木地板/墙面全被判成肤色，v004 背景 0.77）
      · MediaPipe num_poses=3 多人检测（只有一只手，不算一个人）
      · 整帧缩到 96 再切条（细节被平均，入侵片只测出 1.9）
    结论：**静止或小幅入镜的局部物体，像素统计方法无法可靠检出**，
    需手部目标检测器或人工。故本工具不替代逐格印相图目视复核。

限制：
  工具只做筛查。§12 判决权在主管目视；工具 ok 也不能替代目视，
  背景本身在剧烈变化（风、人流）时可能低估静止，需人工复核。
"""

import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np

FF = r"D:/ShadowBot/shadowbot-6.3.39/ffmpeg.exe"
SIZE = 96
STEP = 0.1


def _frame_maps(path, fps, dur):
    """返回逐帧逐像素灰度差图 (n-1, SIZE, SIZE)；帧数不足返回 None。"""
    b = subprocess.run(
        [FF, "-v", "error", "-y", "-i", str(path), "-t", f"{dur:.3f}",
         "-vf", f"fps={fps},scale={SIZE}:{SIZE}", "-pix_fmt", "gray",
         "-f", "rawvideo", "-"],
        capture_output=True).stdout
    n = len(b) // (SIZE * SIZE)
    if n < 2:
        return None
    fr = np.frombuffer(b[:n * SIZE * SIZE], dtype=np.uint8).reshape(n, SIZE, SIZE).astype(np.float32)
    return np.abs(np.diff(fr, axis=0))


def _frame_diffs(path, fps, dur):
    dmap = _frame_maps(path, fps, dur)
    if dmap is None:
        return None
    return dmap.mean(axis=(1, 2))


def _strip_diffs(path, fps, dur, x0, x1, tw=24):
    """按画面横向比例裁一条竖带，缩到 tw×SIZE 灰度，返回逐帧帧差。"""
    vf = (f"fps={fps},crop=iw*{x1 - x0:.3f}:ih:iw*{x0:.3f}:0,"
          f"scale={tw}:{SIZE}")
    b = subprocess.run(
        [FF, "-v", "error", "-y", "-i", str(path), "-t", f"{dur:.3f}",
         "-vf", vf, "-pix_fmt", "gray", "-f", "rawvideo", "-"],
        capture_output=True).stdout
    n = len(b) // (tw * SIZE)
    if n < 2:
        return None
    fr = np.frombuffer(b[:n * tw * SIZE], dtype=np.uint8).reshape(n, SIZE, tw).astype(np.float32)
    return np.abs(np.diff(fr, axis=0)).mean(axis=(1, 2))


def _edge_alert(path, fps, dur, ratio_thr=1.2, persist_thr=0.5, band=0.14):
    """
    边缘持续运动报警：比较左右竖条与中心带的帧差。
    必须用原分辨率裁条带——先把整帧缩到 96×96 再切片会把边缘细节平均掉，
    实测右缘明显运动只测出 1.9（全帧切片）而裁条带能测出 8.9。
    """
    left = _strip_diffs(path, fps, dur, 0.00, band)
    right = _strip_diffs(path, fps, dur, 1.0 - band, 1.00)
    core = _strip_diffs(path, fps, dur, 0.30, 0.70)
    if left is None or right is None or core is None:
        return None

    core_med = float(np.median(core))
    out = dict(alert=False, side="", ratio=0.0, persist=0.0,
               band_med=0.0, core_med=round(core_med, 2))
    if core_med <= 1e-6:
        core_med = 1e-6

    worst = None
    for name, seq in (("左", left), ("右", right)):
        med = float(np.median(seq))
        ratio = med / core_med
        persist = float((seq > np.maximum(1.5, core * 1.2)).mean())
        if ratio > ratio_thr and persist > persist_thr:
            if worst is None or ratio > worst[2]:
                worst = (name, med, ratio, persist)

    if worst is not None:
        name, med, ratio, persist = worst
        out.update(alert=True, side=name, ratio=round(ratio, 2),
                   persist=round(persist, 2), band_med=round(med, 2))
    return out


def analyze(path, fps=10, thr=1.5, max_still=2.0, dur=10.0, edge_check=True):
    dmap = _frame_maps(path, fps, dur)
    if dmap is None:
        return dict(status="unable", reason="抽帧失败或帧数不足")
    d = dmap.mean(axis=(1, 2))
    W = int(2.0 * fps)
    if len(d) < W:
        return dict(status="unable", reason=f"有效帧 {len(d)} 不足一个 2 秒窗口")
    w = np.array([d[i:i + W].mean() for i in range(0, len(d) - W)])

    still = w < thr
    best_k, cur, best_end = 0, 0, 0
    for i, v in enumerate(still):
        if v:
            cur += 1
            if cur > best_k:
                best_k, best_end = cur, i
        else:
            cur = 0

    if best_k == 0:
        longest, span = 0.0, (0.0, 0.0)
    else:
        longest = 2.0 + (best_k - 1) * STEP
        end_t = (best_end + W) / fps
        span = (round(max(0.0, end_t - longest), 2), round(end_t, 2))

    edge = _edge_alert(path, fps, dur) if edge_check else None

    return dict(status="fail" if longest > max_still else "pass",
                longest=round(longest, 2), span=span,
                median=round(float(np.median(w)), 2),
                minv=round(float(w.min()), 2),
                still_ratio=round(float(still.mean()), 2),
                edge=edge)


def resolve_targets(args):
    files = []
    for a in args:
        p = Path(a)
        if p.is_file():
            files.append(p)
            continue
        base = Path("02_待审核/MiniMax") / a
        if base.is_dir():
            files.extend(sorted(f for f in base.rglob("*.mp4") if "_记录" not in str(f)))
        else:
            print(f"[跳过] {a} 既不是文件也不是款号目录")
    return files


def main():
    argv = sys.argv[1:]
    max_still, thr, fps = 2.0, 1.5, 10
    edge_check = True
    rest, i = [], 0
    while i < len(argv):
        if argv[i] == "--max-still":
            max_still = float(argv[i + 1]); i += 2
        elif argv[i] == "--thr":
            thr = float(argv[i + 1]); i += 2
        elif argv[i] == "--fps":
            fps = int(argv[i + 1]); i += 2
        elif argv[i] == "--no-edge":
            edge_check = False; i += 1
        else:
            rest.append(argv[i]); i += 1

    targets = resolve_targets(rest)
    if not targets:
        print("没有可分析的目标")
        return 2

    print(f"静止闸门 §6.13：2 秒窗口帧差均值 < {thr} 判无动作，上限 {max_still:.1f} s，{fps} fps")
    if edge_check:
        print("边缘报警：左右竖条(各14%)帧差 > 中心×1.2 且持续占比 >0.5 时提示")
    bad = unable = warned = 0
    for f in targets:
        try:
            r = analyze(f, fps, thr, max_still, edge_check=edge_check)
        except Exception as e:
            print(f"  {f.name:30s} 无法分析：{type(e).__name__}: {e}")
            unable += 1
            continue
        if r["status"] == "unable":
            print(f"  {f.name:30s} {r['reason']}")
            unable += 1
            continue
        tag = "不合格" if r["status"] == "fail" else "ok"
        span = f"@{r['span'][0]:.1f}-{r['span'][1]:.1f}s" if r["longest"] else ""
        print(f"  {f.name:30s} 帧差中位 {r['median']:.2f} 最低 {r['minv']:.2f} | "
              f"最长无动作 {r['longest']:.1f}s {span} | {tag}")
        if r["status"] == "fail":
            bad += 1
        e = r.get("edge")
        if e and e["alert"]:
            warned += 1
            print(f"  {'':30s} ⚠ 边缘报警：{e['side']}侧持续运动 "
                  f"条带中位 {e['band_med']:.2f} / 中心 {e['core_med']:.2f} = {e['ratio']:.2f}×，"
                  f"持续占比 {e['persist']:.2f} → 疑似第二人/手入镜，须逐格目视")

    print()
    if warned:
        print(f"提示：{warned} 条触发边缘运动报警，须人工确认画面边缘是否有第二人/手（不改变合格判定）")
    if bad:
        print(f"结论：{bad} 条不合格（存在超过 {max_still:.1f} 秒的无动作段），禁止交付")
        return 1
    print(f"结论：{len(targets) - unable} 条通过；仍需主管目视确认动作本身")
    return 0 if not unable else 2


if __name__ == "__main__":
    sys.exit(main())
