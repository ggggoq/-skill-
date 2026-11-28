#!/usr/bin/env python3
"""一键接新款：拷素材、探参数、扫活动度、建交付目录、生成款配置模板。

跑完会得到 `_check/<款号>/sku.config.json`，里面把机器能算的都填好了
（窗口起点、参考切片长度、反面裁切框、每组的动作节拍留 TODO 给人写）。

同时会做三项体检，命中就打印警示：
  · 同款不同组素材 MD5 重复（供应商把同一张图复制到多个文件夹）
  · 「场景参考_反面」四组完全相同（通常是另一件衣服或另一个场地，不能当反面参考）
  · 视频流真实时长 < 容器 Duration（窗口上限必须用真值）

用法：
  python scripts/new_sku.py --sku KF96467710
  python scripts/new_sku.py --sku KF96467710 --source-root "//192.168.2.19/.../素材"
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_SOURCE = r"\\192.168.2.19\团队文件-商品-2026\00-AI买家秀\2-种草视频\素材"
REF_LEN = 4.9
SCENE_PATTERNS = ("*场景参考*01*", "*场景*")
BACK_PATTERNS = ("*场景参考*反面*", "*反面*")
FLAT_PATTERNS = ("*服装细节*", "*细节*")
VIDEO_PATTERNS = ("*动作参考*.mp4", "*.mp4")


def first(d: Path, patterns) -> Path | None:
    for pat in patterns:
        hits = sorted(d.glob(pat))
        if hits:
            return hits[0]
    return None


def grayscale(video: Path, width: int, height: int, root: Path, sr: int = 10) -> np.ndarray:
    """按目标宽高抽灰度帧。高度必须按素材真实比例算，不能写死 16:9。"""
    cmd = [common.ffmpeg(root), "-v", "error", "-i", str(video),
           "-vf", f"fps={sr},scale={width}:{height},format=gray", "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (width * height)
    return np.frombuffer(raw[: n * width * height], dtype=np.uint8).reshape(n, height, width).astype(np.float32)


def activity(video: Path, meta: dict, root: Path) -> dict:
    w = 96
    h = max(2, round(w * (meta["height"] or 1280) / (meta["width"] or 720)))
    frames = grayscale(video, w, h, root)
    diff = np.abs(np.diff(frames, axis=0)).mean(axis=(1, 2))
    bins = [float(diff[i * 5:(i + 1) * 5].mean()) for i in range(len(diff) // 5)]
    best = None
    for start in np.arange(0, max(0.0, len(bins) / 2 - 10) + 0.01, 0.5):
        i0 = int(round(start * 2))
        win = bins[i0:i0 + 20]
        if len(win) < 20:
            continue
        worst = min(min(win[j:j + 4]) for j in range(0, 17))
        if best is None or worst > best[1]:
            best = (float(start), float(worst), float(np.mean(win)))
    return {
        "seconds": round(len(frames) / 10, 2),
        "bins_0.5s": [round(b, 2) for b in bins],
        "recommended_start": best[0] if best else 0.0,
        "worst_2s_mean": best[1] if best else 0.0,
        "window_mean": best[2] if best else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="接新款：拷素材 + 体检 + 生成配置模板")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project", help="项目根（默认自动定位）")
    ap.add_argument("--source-root", default=DEFAULT_SOURCE, help="素材盘根目录")
    ap.add_argument("--batch", default=datetime.now().strftime("%Y%m%d"))
    ap.add_argument("--skip-copy", action="store_true", help="素材已在本地，不重复拷")
    ap.add_argument("--force", action="store_true", help="覆盖已有 sku.config.json")
    args = ap.parse_args()

    root = common.find_project(args.project)
    common.ensure_engine(root)
    paths = common.project_paths(root)
    sku = args.sku.strip()

    cfg_path = common.config_path(root, sku)
    if cfg_path.exists() and not args.force:
        print(f"已有配置：{cfg_path}\n要重建加 --force（会覆盖已填的提示词内容）。")
        return 1

    src_root = Path(args.source_root) / sku
    if not src_root.is_dir():
        raise SystemExit(f"素材盘没有这个款号：{src_root}\n先确认款号，别猜。")
    groups = sorted([d for d in src_root.iterdir() if d.is_dir()], key=lambda p: p.name)
    if not groups:
        raise SystemExit(f"{src_root} 下没有分组目录")

    local_root = paths["materials"] / sku
    if not args.skip_copy:
        for g in groups:
            dst = local_root / g.name
            dst.mkdir(parents=True, exist_ok=True)
            for f in g.iterdir():
                if f.is_file():
                    shutil.copy2(f, dst / f.name)
        print(f"素材已拷到 {local_root}（{len(groups)} 组）")

    # ------------------------------------------------------------- 体检
    print("\n=== 素材体检 ===")
    rows, acts = [], {}
    for g in groups:
        d = local_root / g.name
        video = first(d, VIDEO_PATTERNS)
        scene = first(d, SCENE_PATTERNS)
        back = first(d, BACK_PATTERNS)
        flat = first(d, FLAT_PATTERNS)
        if not video:
            print(f"! G{g.name} 没有动作参考视频，跳过")
            continue
        meta = common.probe_meta(video, root)
        act = activity(video, meta, root)
        acts[g.name] = act
        cap = round(meta["stream_duration"] - 10.0, 3)
        rows.append((g.name, video, scene, back, flat, meta, act, cap))
        print(f"G{g.name}: {meta['width']}x{meta['height']} {meta['fps']}fps "
              f"容器/流 {meta['stream_duration']:.3f}s | 窗口起点上限 {cap:.3f}s | "
              f"推荐 {act['recommended_start']:.1f}s | 音轨 {'有' if meta['has_audio'] else '无'}")
        print("    活动度 " + " ".join(f"{i*0.5:.1f}:{b:.1f}" for i, b in enumerate(act["bins_0.5s"])))

    backs = {r[0]: common.sha256(r[3]) for r in rows if r[3]}
    if len(backs) > 1 and len(set(backs.values())) == 1:
        print("! 四组「场景参考_反面」MD5 完全相同：多半是同一张图复制到各组，或根本不是这一组的背面。"
              "别直接当反面参考喂，先目视核一遍是不是同一件衣服。")
    flats = {r[0]: common.sha256(r[4]) for r in rows if r[4]}
    if len(flats) > 1 and len(set(flats.values())) == 1:
        print("· 各组「服装细节」也是同一张（正常，平铺图通常共用）。")

    # ------------------------------------------------------------- 配置模板
    cfg_groups = []
    for i, (name, video, scene, back, flat, meta, act, cap) in enumerate(rows, start=1):
        start = min(act["recommended_start"], max(0.0, cap - 0.05))
        start = round(start, 2)
        cfg_groups.append({
            "group": i,
            "source_group": name,
            "version": f"v{i:03d}",
            "window_start": start,
            "window_start_cap": cap,
            "ref_len": REF_LEN,
            "girl": "TODO 人物：年龄、肤色、发型/刘海、配饰（包、眼镜、帽子）、表情",
            "outfit": "TODO 服装：版型/颜色/材质/图案/细节（罗纹、刺绣位置、印花），逐件写",
            "setting": "TODO 场景：墙面、地面、背后与两侧物体、光线；只写 Picture 1 里真实存在的",
            "setting_short": "TODO 场景简称，用于成片尾部的一致性声明",
            "neg": "TODO 排除项：只否画面里没有的东西，按组点名（别写 no doorway 这类会把主体否掉的词）",
            "sound": "TODO 环境声：两三句具体声音，不要写音乐",
            "beats": [["00:00.000", "00:01.500", "TODO 动作节拍：朝向、手的位置、视线"]],
            "back_crop": [0.24, 0.626, 0.92, 0.912],
            "storyboard": {"mode": "detected", "anchors": [4.9, 5.4, 5.9, 6.4, 6.9, 7.4, 8.2, 9.0]},
            "assets": {
                "video": video.name if video else None,
                "scene": scene.name if scene else None,
                "back": back.name if back else None,
                "flat": flat.name if flat else None,
            },
            "meta": {k: meta[k] for k in ("width", "height", "fps", "has_audio", "stream_duration")},
        })

    cfg = {
        "schema": "minimax-sku/1",
        "sku": sku,
        "batch": args.batch,
        "model": "minimax-h3-pro-768p",
        "duration": 10,
        "ratio": "9:16",
        "price_per_call_usd": 0.373,
        "common_outfit": "TODO 四组共用的服装描述（各组可用 outfit 覆盖）",
        "groups": cfg_groups,
    }
    common.save_config(root, sku, cfg)

    # 交付目录
    day = paths["delivery"] / sku / args.batch
    for sub in ("00_直接看成片", "_记录/供应商原片", "_记录/证据图", "_记录/不合格-待重出", "_记录/候选"):
        (day / sub).mkdir(parents=True, exist_ok=True)
    (paths["logs"] / f"{args.batch}_{sku}{len(cfg_groups)}组").mkdir(parents=True, exist_ok=True)
    for g in cfg_groups:
        (paths["packs"] / sku / f"minimax-G{g['group']}-{g['version']}-{args.batch}").mkdir(parents=True, exist_ok=True)

    out = root / "_check" / sku / "activity.json"
    out.write_text(json.dumps(acts, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    print(f"\n配置模板：{cfg_path}")
    print(f"交付目录：{day}")
    print(f"活动度明细：{out}")
    print("\n下一步：")
    print("  1) 逐组看素材：`场景参考_01` 定场景/服装，`动作参考` 用 1fps + 0.5fps 抽帧读动作")
    print("  2) 填 sku.config.json 的 girl / outfit / setting / neg / sound / beats")
    print("     · 窗口起点不超过 window_start_cap，否则提交会被时长校验拒掉")
    print("     · 节拍每段 ≤1.5s，必须首尾相接铺满 00:00.000–00:10.000")
    print("  3) 写反面裁切框 back_crop（默认是「腰部以下、不含鞋」的裤子区）")
    print("  4) python scripts/skeleton.py --sku " + sku)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
