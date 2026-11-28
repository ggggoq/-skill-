#!/usr/bin/env python3
"""成片质检：三道机器闸门 + 抽帧证据图 + 交付清单。

闸门（全部由项目 tools/ 下的脚本执行，口径改脚本不改这里）：
  check_camera.py     机位：p90 抖动、净位移、有无单向运镜、同批是否雷同
  check_stillness.py  动作：帧差中位、最长静止段、边缘抖动
  check_audio.py      音轨：时长是否满、逐秒电平是否平稳、有没有静音段

机器闸门过了不等于能交，还必须人眼看 20 格抽帧：背景是否跳变、服装是否漂色、
有没有第二个人、具象道具（玩偶/花篮）有没有变形。看缩略图时注意：
170 px 的格子会把具象道具看成「一条红条」，判断形态要放大到 500 px 以上再看。

用法：
  python scripts/qc.py --sku KF96467710
  python scripts/qc.py --sku KF96467710 --versions v001 v002
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def run(cmd: list[str], env: dict | None = None) -> str:
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    return (r.stdout or "") + (r.stderr or "")


def verdict(text: str, name: str) -> str:
    """从检查器输出里抽这一行的结论。

    三个检查器的输出格式不统一：camera/audio 带 `[PASS]` 标记，stillness 只在行尾写 `| ok`。
    """
    for line in text.splitlines():
        if name in line or Path(name).stem in line:
            m = re.search(r"\[(PASS\*?|WARN|FAIL)\s*\]", line)
            if m:
                return m.group(1)
            tail = line.rsplit("|", 1)[-1].strip().lower()
            if tail.startswith("ok"):
                return "PASS"
            if "fail" in line.lower() or "不合格" in line:
                return "FAIL"
            if "warn" in line.lower() or "偏高" in line:
                return "WARN"
    return "?"


def main() -> int:
    ap = argparse.ArgumentParser(description="成片三道质检 + 证据图 + 交付清单")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--versions", nargs="*")
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    paths = common.project_paths(root)
    ff = common.ffmpeg(root)
    day = paths["delivery"] / args.sku / cfg["batch"]
    out_dir = day / "00_直接看成片"
    ev = day / "_记录" / "证据图"
    ev.mkdir(parents=True, exist_ok=True)

    versions = args.versions or sorted(p.stem for p in out_dir.glob("v*.mp4"))
    if not versions:
        raise SystemExit(f"{out_dir} 里没有成片，先跑 finalize.py")

    files = [str(out_dir / f"{v}.mp4") for v in versions]
    # check_*.py 各自解析 ffmpeg：统一用环境变量喂进去，避免「找不到 ffmpeg」
    env = dict(os.environ, KLING_FFMPEG=ff)
    tools = {k: root / "tools" / f"{k}.py" for k in ("check_camera", "check_stillness", "check_audio")}
    outputs = {k: run([sys.executable, str(p), *files], env) for k, p in tools.items() if p.is_file()}

    rows = []
    for v, f in zip(versions, files):
        fp = Path(f)
        meta = common.probe_meta(fp, root)
        tile = ev / f"{v}_抽帧20格.jpg"
        subprocess.run([ff, "-v", "error", "-i", f, "-vf", "fps=2,scale=170:-1,tile=6x4",
                        "-frames:v", "1", "-q:v", "3", "-y", str(tile)], check=False)
        rows.append({
            "version": v,
            "bytes": fp.stat().st_size,
            "md5": __import__("hashlib").md5(fp.read_bytes()).hexdigest(),
            "duration": round(meta["stream_duration"], 3),
            "size": f"{meta['width']}x{meta['height']}",
            "camera": verdict(outputs.get("check_camera", ""), fp.name),
            "stillness": verdict(outputs.get("check_stillness", ""), fp.name),
            "audio": verdict(outputs.get("check_audio", ""), fp.name),
            "evidence": str(tile.relative_to(root)),
        })

    lines = ["# " + args.sku + " 质检清单", "",
             "| 版本 | 大小 | 时长 | 画幅 | 机位 | 动作 | 音轨 | MD5 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(f"| {r['version']} | {r['bytes']:,} B | {r['duration']} s | {r['size']} | "
                     f"{r['camera']} | {r['stillness']} | {r['audio']} | `{r['md5']}` |")
    report = day / "_记录" / "质检清单.md"
    report.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("\n".join(lines))
    for k, text in outputs.items():
        print(f"\n--- {k} ---")
        print("\n".join(text.strip().splitlines()[-6:]))
    print(f"\n清单：{report}")
    print("证据图：" + ", ".join(r["evidence"] for r in rows))
    print("\n机器过了还要人眼：逐组看 20 格抽帧（背景跳变 / 服装漂色 / 第二个人 / 具象道具变形）。")

    (day / "_记录" / "qc.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1) + "\n",
                                           encoding="utf-8")
    return 0 if all(r["camera"] != "FAIL" and r["stillness"] != "FAIL" and r["audio"] != "FAIL"
                    for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
