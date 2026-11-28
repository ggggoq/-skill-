#!/usr/bin/env python3
"""打包素材：四张参考图 + 4.9 秒参考切片 + manifest（提交闸门只认这个 manifest）。

四图角色固定顺序（少一个都提不上去）：
  model_image       场景/服装权威图，直接裁自「场景参考_01」
  reverse_worn      反面参考。★ 一定要按 back_crop 裁：
                    供应商常把「另一件衣服」或「另一组的背面」复制到各组，整张喂进去
                    模型会把背面的条纹/别的场地画出来（KF96467710、KF92463330 都踩过）。
  front_flat        平铺细节图
  action_storyboard 8 格骨架分镜（skeleton.py 产出）

参考切片固定取「窗口起点起 4.9 秒」，压到 1 MB 内：
上传网关吞吐 20–25 KB/s，超过 1.8 MB 的视频根本传不完。

用法：
  python scripts/build_pack.py --sku KF96467710
  python scripts/build_pack.py --sku KF96467710 --group 3
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def photo(src: Path, dst: Path, width: int = 1024, box=None, quality: int = 92) -> None:
    with Image.open(src) as im:
        im = im.convert("RGB")
        if box:
            w, h = im.size
            x0, y0, x1, y1 = box
            im = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        if im.width > width:
            im = im.resize((width, round(im.height * width / im.width)), Image.LANCZOS)
        im.save(dst, "JPEG", quality=quality, optimize=True)


def cut(ff: str, src: Path, dst: Path, start: float, length: float) -> None:
    subprocess.run([ff, "-v", "error", "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
                    "-c:v", "libx264", "-preset", "medium", "-crf", "26",
                    "-maxrate", "1200k", "-bufsize", "2400k", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "64k", "-ac", "1",
                    "-movflags", "+faststart", "-y", str(dst)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="打包四图 + 参考切片 + manifest")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--group", type=int, action="append")
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    paths = common.project_paths(root)
    ff = common.ffmpeg(root)
    mat = paths["materials"] / args.sku

    for grp in cfg["groups"]:
        g = int(grp["group"])
        if args.group and g not in args.group:
            continue
        ver, src_group = grp["version"], grp["source_group"]
        src = mat / src_group
        pack = paths["packs"] / args.sku / f"minimax-G{g}-{ver}-{cfg['batch']}"
        pack.mkdir(parents=True, exist_ok=True)

        a = grp["assets"]
        if not a.get("scene") or not (src / a["scene"]).is_file():
            raise SystemExit(f"G{g} 缺场景图：{src / str(a.get('scene'))}")
        photo(src / a["scene"], pack / "scene_front.jpg")
        if a.get("back") and (src / a["back"]).is_file():
            photo(src / a["back"], pack / "reverse_worn.jpg", box=grp.get("back_crop"))
        else:
            raise SystemExit(f"G{g} 缺反面图")
        if a.get("flat") and (src / a["flat"]).is_file():
            photo(src / a["flat"], pack / "front_flat.jpg")
        else:
            raise SystemExit(f"G{g} 缺平铺细节图")
        if not (pack / "storyboard.jpg").is_file():
            raise SystemExit(f"G{g} 缺骨架分镜，先跑 scripts/skeleton.py --sku {args.sku}")

        start = float(grp["window_start"])
        ref_len = float(grp.get("ref_len", cfg.get("ref_len", 4.9)))
        video = src / a["video"]
        ref_name = f"reference_video_{start:.2f}-{start + ref_len:.2f}s.mp4"
        cut(ff, video, pack / ref_name, start, ref_len)
        ref_bytes = (pack / ref_name).stat().st_size
        if ref_bytes > 1_600_000:
            print(f"  ! G{g} 切片 {ref_bytes/1e6:.1f} MB 偏大，上传可能被掐")

        base = f"04_历史版本/{args.sku}/minimax-G{g}-{ver}-{cfg['batch']}"
        manifest = {
            "schema_version": 1,
            "sku": args.sku, "version": ver, "group_number": g,
            "model": cfg["model"], "duration": int(cfg["duration"]), "ratio": cfg["ratio"],
            "prompt_file": f".minimax_bridge/prompts/{args.sku}/{ver}.txt",
            "images": {
                "model_image": f"{base}/scene_front.jpg",
                "reverse_worn": f"{base}/reverse_worn.jpg",
                "front_flat": f"{base}/front_flat.jpg",
                "action_storyboard": f"{base}/storyboard.jpg",
            },
            "action_source": {
                "path": str(video.relative_to(root)).replace("\\", "/"),
                "sha256": common.sha256(video),
                "source_start_seconds": start,
                "source_end_seconds": round(start + float(cfg["duration"]), 3),
            },
            "reference_video": {"path": f"{base}/{ref_name}", "sha256": common.sha256(pack / ref_name)},
        }
        mdir = paths["bridge"] / "manifests" / args.sku
        mdir.mkdir(parents=True, exist_ok=True)
        (mdir / f"{ver}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n",
                                          encoding="utf-8")
        (pack / "pack_info.json").write_text(json.dumps({
            "sku": args.sku, "group": g, "version": ver, "window_start": start, "ref_len": ref_len,
            "back_crop": grp.get("back_crop"),
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[G{g}] {pack.name} 窗口 {start:.2f}–{start + float(cfg['duration']):.2f} "
              f"切片 {ref_bytes/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
