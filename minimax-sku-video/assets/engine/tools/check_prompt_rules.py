#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
check_prompt_rules.py — 提示词规则自检（§6.13 / §6.14）

用法：
    ./.venv/Scripts/python.exe tools/check_prompt_rules.py <款号 或 文件...>
    ./.venv/Scripts/python.exe tools/check_prompt_rules.py YF96430410
    ./.venv/Scripts/python.exe tools/check_prompt_rules.py path/to/v001.txt

检查项（§6.14 三节清单的可执行版）：
  1. 无量化停顿：for a beat / a moment / briefly / hold it there / pause for
  2. 速度形容词：unhurried / slow / slowly / gently / softly / carefully /
     deliberate / leisurely —— 含 camera|phone 的镜头句豁免（§6.14 第 2 条例外）
  3. 强制节奏句：same tempo as <Video 1>, no slow motion, no easing
  4. 整段占位：From 00:04.9x to the last frame she only stands...
  5. 时间码连续性：从 00:00.000 起首尾相接、无空隙无重叠、覆盖到 00:10.000
  6. 单段时长 ≤1.5 秒（§6.13 单次停留上限）
  7. 无「站定到底」类收尾：only stands / stays still / remains still

镜头层（§6.9 条 1bis「镜头三句必写」，2026-11-12 主管定：镜头问题必须在
写提示词时解决，不得等出片后审出来再花钱重出）：
  8. 缺手持语境句（hand-held / handheld）
  9. 缺「画面不静止」正向句 —— 只写 never locked off / not a tripod 这类
     纯否定句仍然落不到画面上（TF95390180 v002 实测 p90 = 0.00 px）
 10. 缺「不跟随 + 不推拉」句 —— 人物有走位而镜头没写死"不跟人走"，
     模型就会做出单向跟随平移（TF95390180 v004 实测同号 85% / 净 59 px）
 11. 有走位句且缺不跟随句：单独点名高风险组合

退出码：全部通过 0，任一不通过 1。
"""

import re
import sys
from pathlib import Path

BAN_STOP = re.compile(r"for a beat|a moment|briefly|hold it there|pause for", re.I)
BAN_SPEED = re.compile(r"unhurried|slowly|\bslow\b|gently|softly|carefully|deliberate|leisurely", re.I)
TEMPO = re.compile(r"same tempo as\s*<Video 1>.*no slow motion.*no easing", re.I | re.S)
PLACEHOLDER = re.compile(r"From 00:04\.9\d* to the last frame", re.I)
STANDING = re.compile(r"only stands|stays? still|remains? still|stands? still to the last frame", re.I)
BEAT = re.compile(r"(\d{2}):(\d{2})\.(\d{3})-(\d{2}):(\d{2})\.(\d{3})")
MAX_BEAT = 1.5
TOL = 0.001

# —— 镜头层（§6.9 条 1bis）——
CAM_HANDHELD = re.compile(r"hand-?held", re.I)
# 正向"画面不静止"：必须是画面状态，不是"不锁定"这种否定式
CAM_LIVE = re.compile(
    r"never (?:at rest|still|resting|glued|sit[s]? fixed|locks?|locked)"
    r"|not still|unsteadiness|unsteady|drifts? by a hair|live (?:unsteadiness|movement)"
    r"|keeps? the small .{0,30}(?:movement|shake|drift)",
    re.I,
)
# 纯否定式（只写这个不算过关）
CAM_NEG_ONLY = re.compile(r"never locked off|not locked off|no tripod|not a tripod|not on a tripod", re.I)
# 不跟随：拍摄者站着不动、镜头不追人
CAM_NOFOLLOW = re.compile(
    r"(?:feet planted|on one spot|stays? where (?:he|she) (?:is|stands))"
    r".{0,160}?(?:does not|doesn't|never)\s+(?:walk|follow|step|travel|chase|move with)"
    r"|(?:does not|doesn't|never)\s+(?:walk|follow|travel|chase)[^.]{0,40}with (?:her|him)"
    r"|the frame never chases",
    re.I | re.S,
)
CAM_NOPUSH = re.compile(r"no push-?in|no zoom|no pull-?back|no re-?framing|no dolly|no pan", re.I)
# 人物走位（容易诱发单向跟随运镜）；只认"走了几步 + 方向"，不认重心后移的半 step back
CAM_WALK = re.compile(
    r"\bwalks?\s+(?:\w+\s+){0,4}?(?:toward|towards|away|forward)\b"
    r"|\bwalks?\s+(?:\w+\s+){0,4}?with her back"
    r"|\btakes?\s+(?:\w+\s+){0,2}?steps\b[^.]{0,30}(?:toward|towards|away|to the lens)",
    re.I,
)


def sec(mm, ss, ms):
    """时间码格式为 MM:SS.mmm（分:秒.毫秒），不是时:分:秒。"""
    return int(mm) * 60 + int(ss) + int(ms) / 1000.0


def check(path):
    t = path.read_text(encoding="utf-8")
    problems = []
    notes = []

    for m in BAN_STOP.finditer(t):
        problems.append(f"无量化停顿：'{m.group(0)}'")

    for m in BAN_SPEED.finditer(t):
        ctx = t[max(0, m.start() - 70): m.end() + 70]
        if re.search(r"camera|phone", ctx, re.I):
            continue
        # 强制节奏句里的 `no slow motion` 是硬要求，不是速度形容词
        if re.search(r"\bno\s+$", t[max(0, m.start() - 6): m.start()], re.I):
            continue
        problems.append(f"速度形容词：'{m.group(0)}'")

    if not TEMPO.search(t):
        problems.append("缺强制节奏句（same tempo as <Video 1>, no slow motion, no easing in or out）")

    for m in PLACEHOLDER.finditer(t):
        problems.append(f"整段占位：'{m.group(0)}'")

    for m in STANDING.finditer(t):
        problems.append(f"站定收尾：'{m.group(0)}'")

    beats = [(sec(*m.group(1, 2, 3)), sec(*m.group(4, 5, 6))) for m in BEAT.finditer(t)]
    if not beats:
        problems.append("没有任何 XX:XX.XXX-XX:XX.XXX 时间码")
    else:
        if abs(beats[0][0]) > TOL:
            problems.append(f"首段未从 00:00.000 开始（{beats[0][0]:.3f}）")
        for i in range(1, len(beats)):
            gap = beats[i][0] - beats[i - 1][1]
            if abs(gap) > TOL:
                problems.append(
                    f"时间码不连续：{beats[i-1][1]:.3f} → {beats[i][0]:.3f} 差 {gap:+.3f}s")
        if abs(beats[-1][1] - 10.0) > TOL:
            problems.append(f"尾部未到 00:10.000（{beats[-1][1]:.3f}）")
        for a, b in beats:
            if b - a > MAX_BEAT + TOL:
                problems.append(f"单段 {b-a:.2f}s 超过 {MAX_BEAT}s（{a:.2f}-{b:.2f}）")

    # —— 镜头层（§6.9 条 1bis）：写提示词时就要过，不许留到出片后再审 ——
    has_live = CAM_LIVE.search(t)
    has_follow = bool(CAM_NOFOLLOW.search(t))
    has_walk = bool(CAM_WALK.search(t))
    if not CAM_HANDHELD.search(t):
        problems.append("镜头：缺手持语境句（hand-held / handheld）")
    if not has_live:
        hint = "（现在只有纯否定式 never locked off / not a tripod，落不到画面上）" if CAM_NEG_ONLY.search(t) else ""
        problems.append(f"镜头：缺「画面不静止」正向句，必写成状态而非否定{hint}")
    if has_walk and not has_follow:
        # 有走位又没写死"不跟人走"，就是 TF95390180 v004 单向运镜（同号 85%/净 59 px）的成因
        problems.append("镜头：有人物走位却没有「拍摄者双脚不动、不跟着她走」句，单向跟随运镜高风险")
    elif not has_follow:
        notes.append("镜头：未写「拍摄者双脚不动、不跟着她走」句（本组无走位，暂不判不合格，加一句更稳）")
    if not CAM_NOPUSH.search(t):
        problems.append("镜头：缺「不推近/不拉远/不变焦/不重构图」句")

    return problems, notes, len(beats), sum(b - a for a, b in beats) if beats else 0.0


def resolve(args, sku):
    files = []
    root = Path(".minimax_bridge/prompts")
    for a in args:
        p = Path(a)
        if p.is_file():
            files.append(p)
            continue
        d = root / a
        if d.is_dir():
            files.extend(sorted(d.glob("*.txt")))
            continue
        if re.fullmatch(r"v\d{3}", a):
            cand = root / sku / f"{a}.txt"
            if cand.is_file():
                files.append(cand)
                continue
        print(f"[跳过] {a}")
    return files


def main():
    argv = sys.argv[1:]
    sku = "YF96430410"
    if "--sku" in argv:
        i = argv.index("--sku")
        sku = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    files = resolve(argv, sku)
    if not files:
        print("没有可检查的提示词")
        return 2
    bad = 0
    for f in files:
        problems, notes, n, total = check(f)
        if problems:
            bad += 1
            print(f"\n{f.name}  不合格（{n} 段 / {total:.2f}s）")
            for p in problems[:12]:
                print(f"    - {p}")
            if len(problems) > 12:
                print(f"    ... 另有 {len(problems) - 12} 项")
        else:
            print(f"{f.name}  通过（{n} 段 / {total:.2f}s，连续覆盖 00:00.000-00:10.000）")
        for note in notes:
            print(f"    [提示] {note}")
    print()
    print(f"结论：{len(files) - bad}/{len(files)} 通过")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
