"""机位位移闸门：手持看得到、但看不出规律（§6.9「手持感的下限」）。

量什么
------
抽 2 fps 帧 -> 灰度 -> 缩到 192 宽 -> 取画面上部 40%（树干/树冠/门框这类背景，
人物不占）-> 相邻帧在 ±12 px 内做整像素 SAD 搜索 -> 得到每步 (dx, dy)。

判什么
------
* **锁定**：位移 90 分位 < 1.0 px —— 看不出是手持，等同三脚架，判不合格。
* **单向运镜**：同号帧 >= 85% 且净位移 >= 8 px —— 把"抖动"写成了运镜，判不合格。
* **幅值过大**：位移 90 分位 > 12 px —— 不是微抖，是摇镜头，判不合格。
* **同批雷同**：主方向相同、且 p90 落在同一 1.6 倍带宽内的组 **>= 3 组** —— 统一偏移，
  判不合格；只有 2 组命中时输出提示，需人工并排复核。
  若命中簇的 p90 全部 <= 3 px，该区间落在 192 宽整像素量化的离散下限（只能取 1/2/3），
  同值可能只是量化撞值，只提示不判不合格，用 `--width 384` 复测后再定。

为什么不用相关系数：相邻帧位移接近白噪声，累计路径更是随机游走，两组无关的随机游走
很容易算出 |r| > 0.8。旧批（YF95402255 音轨截断版五组）实测：逐帧相关 0 对命中，
累计路径出现 v001–v005 r=0.86 的伪相关。所以判据用"幅值落在同一带"这种稳定量。

用法
----
    python tools/check_camera.py YF95402255
    python tools/check_camera.py 02_待审核/MiniMax/YF95402255/20261109
    python tools/check_camera.py a.mp4 b.mp4 --verbose

退出码：全部通过 0；任一不合格 1；无法测量 2。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# —— 阈值（与 docs/所有模型标准必看.md §6.9「手持感的下限」一致）——
P90_MIN = 1.0          # 低于此值判"看不出手持"
P90_TARGET_MAX = 8.0   # 目标区间上界
P90_HARD_MAX = 12.0    # 超过此值判"摇镜头"
SIGN_RATIO_MAX = 0.85  # 同号帧比例上限
NET_DRIFT_MAX = 8.0    # 净位移上限（px）
CLUSTER_P90_RATIO = 1.6  # 同批 >=3 组主方向相同且 p90 落在 1.6 倍带宽内 -> 统一偏移
PAIR_P90_RATIO = 1.3     # 2 组同向且 p90 相差 <=30% -> 提示人工复核
SAMPLE_FPS = 2
FRAME_WIDTH = 192
SEARCH_RADIUS = 12
TOP_PORTION = 0.40
# 192 宽整像素搜索的量化下限：位移只有 1–3 px 时 p90 只能取 1/2/3 这几个离散值，
# 多组必然撞值。撞出来的"同簇"不能当统一偏移判不合格，只能提示提高精度复测。
LOW_RES_FLOOR = 3.0


@dataclass
class Run:
    name: str
    steps: list[tuple[int, int]] = field(default_factory=list)
    sad: list[float] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    verdict: str = ""
    reasons: list[str] = field(default_factory=list)


def ffmpeg_path() -> str:
    env = os.environ.get("KLING_FFMPEG")
    if env and Path(env).is_file():
        return env
    for candidate in (
        PROJECT_ROOT / "tools" / "ffmpeg.exe",
        Path("D:/ShadowBot/shadowbot-6.3.39/ffmpeg.exe"),
    ):
        if candidate.is_file():
            return str(candidate)
    return "ffmpeg"


def _frames(video: Path, width: int = FRAME_WIDTH) -> list[Path]:
    tmp = Path(tempfile.mkdtemp(prefix="camgate_"))
    command = [
        ffmpeg_path(), "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video),
        "-vf", f"fps={SAMPLE_FPS},scale={width}:-2",
        "-q:v", "2", str(tmp / "%04d.jpg"),
    ]
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败: {completed.stderr.strip()[:200]}")
    return sorted(tmp.glob("*.jpg"))


def _gray(path: Path) -> np.ndarray:
    image = Image.open(path).convert("L")
    width, height = image.size
    top = int(height * TOP_PORTION)
    return np.asarray(image.crop((0, 0, width, top)), dtype=np.float32)


def _best_shift(a: np.ndarray, b: np.ndarray, radius: int = SEARCH_RADIUS) -> tuple[int, int, float]:
    height, width = a.shape
    if height <= 2 * radius or width <= 2 * radius:
        return 0, 0, float("inf")
    core = a[radius:height - radius, radius:width - radius]
    best = (0, 0, float("inf"))
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            moved = b[radius + dy:height - radius + dy, radius + dx:width - radius + dx]
            score = float(np.abs(core - moved).mean())
            if score < best[2]:
                best = (dx, dy, score)
    return best


def measure(video: Path, radius: int = SEARCH_RADIUS, width: int = FRAME_WIDTH) -> Run:
    run = Run(name=video.name)
    files = _frames(video, width)
    if len(files) < 5:
        run.verdict = "INSUFFICIENT"
        run.reasons.append(f"只抽到 {len(files)} 帧，样本不足")
        return run
    previous = None
    scale = width / FRAME_WIDTH  # 位移归一到 192 基准，阈值口径不变
    for path in files:
        current = _gray(path)
        if previous is not None:
            dx, dy, sad = _best_shift(previous, current, radius)
            run.steps.append((dx / scale, dy / scale))
            run.sad.append(round(sad, 2))
        previous = current
    classify(run)
    return run


def classify(run: Run) -> Run:
    steps = run.steps
    if not steps:
        run.verdict = "INSUFFICIENT"
        run.reasons = ["没有可比帧"]
        return run
    xs = np.array([s[0] for s in steps], dtype=float)
    ys = np.array([s[1] for s in steps], dtype=float)
    p90_x = float(np.percentile(np.abs(xs), 90))
    p90_y = float(np.percentile(np.abs(ys), 90))
    med_x = float(np.median(np.abs(xs)))
    med_y = float(np.median(np.abs(ys)))
    p90 = max(p90_x, p90_y)
    median = max(med_x, med_y)

    def sign_ratio(values: np.ndarray) -> float:
        positive = int((values > 0).sum())
        negative = int((values < 0).sum())
        return max(positive, negative) / len(values)

    ratio_x, ratio_y = sign_ratio(xs), sign_ratio(ys)
    net_x, net_y = float(xs.sum()), float(ys.sum())
    # 主方向按幅值判，不按标准差：某一轴恒定时标准差为 0，会误判成另一轴。
    main_axis = "x" if float(np.abs(xs).max()) >= float(np.abs(ys).max()) else "y"
    sign_ratio = ratio_x if main_axis == "x" else ratio_y
    net = abs(net_x) if main_axis == "x" else abs(net_y)

    run.metrics = {
        "frames": len(steps) + 1,
        "p90": round(p90, 2),
        "p90_x": round(p90_x, 2),
        "p90_y": round(p90_y, 2),
        "median": round(median, 2),
        "sign_ratio": round(sign_ratio, 3),
        "net_x": round(net_x, 1),
        "net_y": round(net_y, 1),
        "net": round(net, 1),
        "main_axis": main_axis,
        "mean_sad": round(float(np.mean(run.sad)), 2) if run.sad else None,
    }

    reasons: list[str] = []
    if p90 < P90_MIN:
        reasons.append(f"位移 90 分位仅 {p90:.2f} px（< {P90_MIN}），看不出手持")
    if p90 > P90_HARD_MAX:
        reasons.append(f"位移 90 分位 {p90:.2f} px（> {P90_HARD_MAX}），幅值过大像摇镜头")
    if sign_ratio >= SIGN_RATIO_MAX and net >= NET_DRIFT_MAX:
        reasons.append(
            f"{main_axis} 方向同号帧 {sign_ratio:.0%}（≥ {SIGN_RATIO_MAX:.0%}）、"
            f"净位移 {net:.1f} px（≥ {NET_DRIFT_MAX}），是单向运镜不是抖动"
        )
    run.reasons = reasons
    run.verdict = "FAIL" if reasons else ("PASS" if p90 <= P90_TARGET_MAX else "PASS*")
    return run


def _largest_cluster(items: list[tuple[float, str]], factor: float) -> list[tuple[float, str]]:
    """在 p90 序列里找落在 factor 倍带宽内的最大一簇（不要求全部组都在带内）。"""
    ordered = sorted(items)
    best: list[tuple[float, str]] = []
    for start in range(len(ordered)):
        end = start
        while end + 1 < len(ordered) and ordered[end + 1][0] <= ordered[start][0] * factor:
            end += 1
        candidate = ordered[start:end + 1]
        if len(candidate) > len(best):
            best = candidate
    return best


def compare(runs: list[Run]) -> tuple[list[str], list[str]]:
    """返回 (不合格, 提示)。判据见模块 docstring「同批雷同」。"""
    fails: list[str] = []
    warns: list[str] = []
    usable = [r for r in runs if r.steps and r.metrics]
    by_axis: dict[str, list[Run]] = {}
    for run in usable:
        by_axis.setdefault(run.metrics["main_axis"], []).append(run)

    for axis, group in by_axis.items():
        if len(group) < 2:
            continue
        items = [(run.metrics["p90"], run.name) for run in group]
        if min(value for value, _ in items) <= 0:
            continue
        cluster = _largest_cluster(items, CLUSTER_P90_RATIO)
        names = "、".join(name for _, name in cluster)
        values = [value for value, _ in cluster]
        if len(cluster) >= 3 and max(values) <= LOW_RES_FLOOR:
            warns.append(
                f"{len(cluster)} 组主方向同为 {axis}、幅值挤在 {min(values):.1f}–{max(values):.1f} px"
                f"（{names}）—— 该区间落在整像素量化的离散下限，同值可能只是量化撞值，"
                "不等于同一条曲线；请加 --width 384 复测后再判"
            )
            continue
        if len(cluster) >= 3:
            fails.append(
                f"{len(cluster)} 组主方向同为 {axis}，p90 落在 {min(values):.1f}–{max(values):.1f} px"
                f"（同一 {CLUSTER_P90_RATIO} 倍带宽，{names}）—— 同批统一偏移"
            )
        elif len(cluster) == 2:
            smaller, larger = min(values), max(values)
            if larger / smaller <= PAIR_P90_RATIO:
                warns.append(
                    f"{names} 同为 {axis} 向且 p90 相差 ≤{(PAIR_P90_RATIO - 1) * 100:.0f}%"
                    f"（{smaller:.1f} / {larger:.1f} px）—— 疑似同款，请并排复核"
                )
    return fails, warns


def resolve_targets(raw: list[str]) -> list[Path]:
    targets: list[Path] = []
    for item in raw:
        path = Path(item)
        if path.is_file() and path.suffix.lower() == ".mp4":
            targets.append(path)
            continue
        if not path.is_absolute() and (PROJECT_ROOT / path).exists():
            path = PROJECT_ROOT / path
        if path.is_dir():
            files = sorted(path.glob("*.mp4"))
            if files:
                targets.extend(files)
                continue
        # 当成款号：取最新日期子目录
        sku_dir = PROJECT_ROOT / "02_待审核" / "MiniMax" / item
        if sku_dir.is_dir():
            dated = sorted([d for d in sku_dir.iterdir() if d.is_dir() and d.name.isdigit()])
            if dated:
                latest = dated[-1]
                # 2026-11-14 起成片收在 00_直接看成片/；旧批次仍在日期目录根下，两者都认
                straight = latest / "00_直接看成片"
                pool = sorted(straight.glob("*.mp4")) if straight.is_dir() else sorted(latest.glob("*.mp4"))
                if not pool:
                    pool = sorted(latest.glob("*.mp4"))
                targets.extend(pool)
                continue
        print(f"[跳过] 找不到可测对象：{item}", file=sys.stderr)
    return targets


def main() -> int:
    parser = argparse.ArgumentParser(description="机位位移闸门（手持感上限与下限）")
    parser.add_argument("targets", nargs="+", help="款号 / 目录 / mp4 文件")
    parser.add_argument("--verbose", action="store_true", help="打印逐段位移表")
    parser.add_argument("--no-batch", action="store_true", help="跳过同批相似度比对")
    parser.add_argument("--width", type=int, default=FRAME_WIDTH,
                        help=f"分析宽度（默认 {FRAME_WIDTH}；低幅值撞值时用 384 复测，结果已归一）")
    args = parser.parse_args()

    videos = resolve_targets(args.targets)
    if not videos:
        print("没有可测的视频", file=sys.stderr)
        return 2

    radius = max(4, int(round(SEARCH_RADIUS * args.width / FRAME_WIDTH)))
    runs: list[Run] = []
    for video in videos:
        try:
            run = measure(video, radius=radius, width=args.width)
        except Exception as error:  # noqa: BLE001
            print(f"[ERROR] {video.name}: {error}", file=sys.stderr)
            return 2
        runs.append(run)
        mark = {"PASS": "PASS  ", "PASS*": "PASS* ", "FAIL": "FAIL  "}.get(run.verdict, run.verdict)
        metrics = run.metrics
        if metrics:
            print(
                f"[{mark}] {run.name}  p90={metrics['p90']:.2f}px "
                f"(x {metrics['p90_x']:.2f} / y {metrics['p90_y']:.2f})  "
                f"中位={metrics['median']:.2f}  同号={metrics['sign_ratio']:.0%}  "
                f"净位移={metrics['net']:.1f}px  主方向={metrics['main_axis']}"
            )
        else:
            print(f"[{mark}] {run.name}")
        for reason in run.reasons:
            print(f"         - {reason}")
        if args.verbose:
            print("         t(s) " + " ".join(f"{i / SAMPLE_FPS:>4.1f}" for i in range(len(run.steps))))
            print("         dx   " + " ".join(f"{s[0]:>6.1f}" for s in run.steps))
            print("         dy   " + " ".join(f"{s[1]:>6.1f}" for s in run.steps))

    batch_fail, batch_warn = ([], []) if args.no_batch else compare(runs)
    for problem in batch_fail:
        print(f"[FAIL] {problem}")
    for hint in batch_warn:
        print(f"[WARN] {hint}")

    failed = [r for r in runs if r.verdict == "FAIL"]
    warn = [r for r in runs if r.verdict == "PASS*"]
    print(
        f"\n合计 {len(runs)} 条：合格 {len(runs) - len(failed) - len(warn)}，"
        f"偏高 {len(warn)}，不合格 {len(failed)}，同批雷同 {len(batch_fail)} 组，提示 {len(batch_warn)} 条"
    )
    if failed or batch_fail:
        print("结论：不合格，禁止交付（先改提示词重出，不要只改措辞）")
        return 1
    print("结论：通过（手持可见、无单向运镜、同批不雷同）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
