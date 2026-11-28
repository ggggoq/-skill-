#!/usr/bin/env python3
"""提交前的固定模型闸门（主管 2026-11-09 硬规则）。

MiniMax 生成一律使用 minimax-h3-pro-768p，不允许选择其他模型。
本脚本逐份扫描 .minimax_bridge 的 manifests / approvals / previews，
只要出现任何非 minimax-h3-pro-768p 的 model 值就返回非零退出码，
用于提交前强制拦截（三处 model 必须逐字一致）。

用法：
    python tools/check_fixed_model.py YF95402255          # 提交前必跑
    python tools/check_fixed_model.py YF95402255 NF96306130
    python tools/check_fixed_model.py --all               # 全库体检（含历史款号）

说明：2026-11-05 的 JF96463785 是规则生效前用 minimax-h3-2k 出片的既成事实，
不参与日常拦截，只在 --all 体检时列出，便于区分“历史遗留”与“新提交”。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FIXED_MODEL = "minimax-h3-pro-768p"
LAYERS = ("manifests", "approvals", "previews")
BRIDGE = Path(__file__).resolve().parents[1] / ".minimax_bridge"


def iter_records(skus: list[str] | None):
    for layer in LAYERS:
        base = BRIDGE / layer
        if not base.is_dir():
            continue
        roots = [base / s for s in skus] if skus else [p for p in base.iterdir() if p.is_dir()]
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.glob("*.json")):
                yield layer, root.name, path


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    scan_all = "--all" in argv[1:]
    args = [a for a in args if a not in {FIXED_MODEL, "minimax-h3-768p", "minimax-h3-2k", "minimax-h3-pro-2k"}]
    if not scan_all and not args:
        print(__doc__.strip())
        return 2
    skus = None if scan_all else args

    rows: list[tuple[str, str, str, str]] = []
    bad: list[tuple[str, str, str, str]] = []
    missing = 0
    for layer, sku, path in iter_records(skus):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            model = f"<无法解析: {exc}>"
        else:
            model = data.get("model")
        if model is None:
            model = "<缺 model 字段>"
            missing += 1
        model = str(model)
        rows.append((layer, sku, path.name, model))
        if model != FIXED_MODEL:
            bad.append((layer, sku, path.name, model))

    scope = "全库" if scan_all else " / ".join(args)
    print(f"固定模型闸门：要求 model == {FIXED_MODEL}")
    print(f"范围：{scope}；扫描层：{' / '.join(LAYERS)}")
    print("-" * 84)
    for layer, sku, name, model in rows:
        if model == FIXED_MODEL:
            if not bad:
                print(f"OK   {layer:<10} {sku:<14} {name:<28} {model}")
        else:
            print(f"FAIL {layer:<10} {sku:<14} {name:<28} {model}")
    if bad and any(r[3] == FIXED_MODEL for r in rows):
        print(f"（其余 {len(rows) - len(bad)} 份均为 {FIXED_MODEL}，已省略）")
    print("-" * 84)
    print(f"共 {len(rows)} 份；合规 {len(rows) - len(bad)}；不合规 {len(bad)}；缺 model {missing}")
    if not rows:
        print("结果：范围内没有任何清单，禁止提交。")
        return 1
    if bad:
        live = [b for b in bad if b[1] != "JF96463785"]
        if live:
            print("结果：禁止提交。先把下列文件改回 minimax-h3-pro-768p 再走审批：")
            for layer, sku, name, model in live:
                print(f"  - .minimax_bridge/{layer}/{sku}/{name} -> {model}")
            return 1
        print(f"结果：仅历史款号 JF96463785（{len(bad)} 份，minimax-h3-2k 既成事实，规则生效前）不合规。")
        print("      本次提交范围不含历史款号，可继续；新提交一律不得再出现非 pro-768p。")
        return 0
    print("结果：通过，三处 model 逐字一致，可提交。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
