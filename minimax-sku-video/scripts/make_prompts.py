#!/usr/bin/env python3
"""按模板渲染提示词，并跑规则闸门（段长 ≤1.5s、连续覆盖 10.000s、≤7000 字符）。

提示词是这套流程的命门，五条硬规则：
  1. 总长 ≤ 7000 字符（上游硬上限，超了直接拒，不截断提交）
  2. 节拍每段 ≤ 1.5 秒，且必须首尾相接铺满 00:00.000–00:10.000
  3. 不许出现含糊停顿（for a moment / for a beat），必须给秒
  4. 0–4.9 秒是「照着参考片复刻」，之后的节拍要与参考片后续画面一致，否则模型自己在两套指令间摇摆
  5. 场景只写 Picture 1 里真实存在的东西；排除项按组点名，不许写会把主体否掉的粗粒度否定

渲染完会调用项目里的 tools/check_prompt_rules.py 做机器复核。

用法：
  python scripts/make_prompts.py --sku KF96467710
  python scripts/make_prompts.py --sku KF96467710 --group 2
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "templates" / "prompt.txt"

DEFAULT_GARMENT_HOLD = (
    "the garments keep exactly the cut, colour and details written in <Subject 2>, front and back "
    "the same: the top stays one even colour, plain and smooth, with no stripes, no ribbing across "
    "the body, no print and no shine; the trousers stay one solid black from waist to hem, with no "
    "fade, no grey patches and no sheen."
)


def render(cfg: dict, grp: dict) -> str:
    beats = "".join(f"{a}-{b} {c}\n" for a, b, c in grp["beats"])
    outfit = grp.get("outfit") or cfg.get("common_outfit") or ""
    return TEMPLATE.read_text(encoding="utf-8").format(
        girl=grp["girl"],
        outfit=outfit,
        setting=grp["setting"],
        beats=beats,
        garment_hold=grp.get("garment_hold") or DEFAULT_GARMENT_HOLD,
        setting_short=grp.get("setting_short", "the setting written above"),
        neg=grp.get("neg", "no second wall"),
        sound=grp.get("sound", "quiet room tone"),
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染提示词并做规则检查")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--group", type=int, action="append")
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    out_dir = root / ".minimax_bridge" / "prompts" / args.sku
    out_dir.mkdir(parents=True, exist_ok=True)

    written = []
    for grp in cfg["groups"]:
        g = int(grp["group"])
        if args.group and g not in args.group:
            continue
        text = render(cfg, grp)
        p = out_dir / f"{grp['version']}.txt"
        p.write_text(text, encoding="utf-8", newline="\n")
        written.append(p)
        print(f"{grp['version']}: {len(text)} 字符 -> {p.relative_to(root)}")

    checker = root / "tools" / "check_prompt_rules.py"
    if checker.is_file():
        print("\n=== 规则检查 ===")
        r = subprocess.run([sys.executable, str(checker), *map(str, written)],
                           capture_output=True, text=True)
        print((r.stdout or r.stderr).strip())
        if r.returncode:
            print("\n不通过就回去改 beats / 压缩长句，别硬提。")
            return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
