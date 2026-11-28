#!/usr/bin/env python3
"""款号参考体检（一条命令）：四条参考是不是同一条、动作类型够不够、图片有没有复制。

解决什么问题
------------
「换了参考，成片动作还是那几套」——先别怪模型。多数情况是这一款拿到的参考
本身就有问题：动作参考是同一个文件复制四份、或者四条内容各不相同但动作类型
只有「站定摆拍」和「走动」两种。这两种情况都会让成片收敛到同一批网红动作
（抬手、比耶、转圈）。本脚本把这件事变成机器可查的结论，不靠肉眼。

一条命令
--------
    python scripts/ref_audit.py --sku KF96103773
    python scripts/ref_audit.py --sku KF96103773 --pose-python "F:\\jp_tmp_frames\\venv_pose\\Scripts\\python.exe"

默认去素材盘 `<source-root>/<款号>/<组号>/` 取料；加 `--local` 改读项目 `01_素材库`。

查什么
------
1. 动作参考：SHA256 / 大小 / 分辨率 / 时长 / 音频采样率
2. 动作参考两两内容比对：2 fps 64×64 灰度、±4 s 滑窗对齐的最小 MAE
   < 8 判「同一段」（改封装/裁剪也算重复），> 10 判「不同内容」
3. 动作类型画像（可选，需 mediapipe）：抬手帧占比、净位移（肩宽）、肩宽摆动
   → 归成 `走动` / `站定摆拍` / `站定+抬手` 三类，看四组到底有几种动作
4. 图片体检：场景参考_01 / 场景参考_反面 / 服装细节 是否跨组复制
   （反面图四组同 MD5 是硬伤：G2–G4 的背面一致性必崩）

退出码
------
0 可以继续；1 有 BLOCK 级问题（别提交，先换参考）；`--strict` 时 WARN 也返回 1。
"""
from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DEFAULT_SOURCE = r"\\192.168.2.19\团队文件-商品-2026\00-AI买家秀\2-种草视频\素材"


def resolve_pose(root: Path, pose_py: str | None, pose_model: str | None):
    """定位「带 mediapipe 的 python」与姿态模型。

    新同事机器上没有 F:\\jp_tmp_frames\\venv_pose，所以默认走项目自己的 .venv 与
    models/（init_project.py --venv --install 会把 mediapipe 和 .task 装好）。
    探测不到就降级：只做参考比对，不做动作画像，并明确告诉人怎么补。
    """
    py = pose_py
    if not py:
        for c in (root / ".venv" / "Scripts" / "python.exe",
                  root / ".venv" / "bin" / "python",
                  Path(sys.executable)):
            if c.is_file():
                py = str(c)
                break
    if py and not Path(py).is_file():
        print(f"[WARN] pose python not found: {py} -> skip motion profile")
        return None, None
    if py:
        try:
            ok = subprocess.run([py, "-c", "import mediapipe"], capture_output=True,
                                timeout=120).returncode == 0
        except Exception:
            ok = False
        if not ok:
            print("[WARN] this python has no mediapipe -> skip motion profile")
            print("       fix: init_project.py --venv --install, or pass --pose-python")
            return None, None

    mdl = pose_model
    if not mdl:
        for c in (root / "models" / "pose_landmarker_lite.task",
                  Path(r"F:/jp_tmp_frames/models/pose_landmarker_lite.task")):
            if c.is_file():
                mdl = str(c)
                break
    if not mdl or not Path(mdl).is_file():
        print("[WARN] pose model missing -> skip motion profile")
        print(f"       expected: {root / 'models' / 'pose_landmarker_lite.task'}")
        return None, None
    return py, mdl
VIDEO_PATTERNS = ("*动作参考*.mp4", "*动作参考*", "*.mp4", "*.mov")
SCENE_PATTERNS = ("*场景参考*01*", "*场景参考*", "*场景*")
BACK_PATTERNS = ("*反面*",)
FLAT_PATTERNS = ("*服装细节*", "*细节*")

FPS = 2
SIZE = 64
MAX_SHIFT = 8          # ±4 s
MAE_SAME = 8.0         # < 8 判同一段内容
MAE_DIFF = 10.0        # > 10 判不同内容


# ---------------------------------------------------------------- 基础
def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def md5_cached(path: Path, cache: dict) -> str:
    """跨款扫描用：按 (大小, mtime) 缓存，避免每次重读网络盘。"""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return ""
    ent = cache.get(key)
    if isinstance(ent, dict) and ent.get("size") == st.st_size and abs(ent.get("mtime", 0) - st.st_mtime) < 1:
        return str(ent.get("md5", ""))
    try:
        v = hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return ""
    cache[key] = {"size": st.st_size, "mtime": st.st_mtime, "md5": v}
    return v


def pick(group_dir: Path, patterns) -> Path | None:
    for pat in patterns:
        hits = sorted(p for p in group_dir.glob(pat) if p.is_file())
        if hits:
            return hits[0]
    return None


def probe(path: Path, ff: str) -> dict:
    out = subprocess.run([ff, "-hide_banner", "-i", str(path)],
                         capture_output=True, text=True, encoding="utf-8",
                         errors="replace").stderr
    dur = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    res = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    fps = re.search(r"(\d+(?:\.\d+)?)\s+fps", out)
    ar = re.search(r"Audio:.*?(\d{4,6})\s*Hz", out)
    seconds = None
    if dur:
        seconds = int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3))
    return {
        "width": int(res.group(1)) if res else None,
        "height": int(res.group(2)) if res else None,
        "fps": float(fps.group(1)) if fps else None,
        "audio_hz": int(ar.group(1)) if ar else None,
        "has_audio": "Audio:" in out,
        "duration": round(seconds, 2) if seconds else None,
        "ratio": (round(int(res.group(1)) / int(res.group(2)), 4) if res else None),
    }


def gray_frames(path: Path, ff: str, fps: int = FPS, size: int = SIZE) -> np.ndarray:
    cmd = [ff, "-v", "error", "-i", str(path),
           "-vf", f"fps={fps},scale={size}:{size}:flags=area,format=gray",
           "-f", "rawvideo", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    n = len(raw) // (size * size)
    if n == 0:
        raise SystemExit(f"抽帧为空：{path}")
    return np.frombuffer(raw[: n * size * size], dtype=np.uint8).reshape(n, size, size).astype(np.float32)


def align_mae(a: np.ndarray, b: np.ndarray, max_shift: int = MAX_SHIFT) -> tuple[float, int]:
    best, best_shift = 1e9, 0
    for shift in range(-max_shift, max_shift + 1):
        if shift >= 0:
            x, y = a[shift:], b[: len(b) - shift]
        else:
            x, y = a[: len(a) + shift], b[-shift:]
        m = min(len(x), len(y))
        if m < 6:
            continue
        d = float(np.abs(x[:m] - y[:m]).mean())
        if d < best:
            best, best_shift = d, shift
    return best, best_shift


# ---------------------------------------------------------------- 姿态画像（子进程）
POSE_WORKER_CODE = r'''
import json, sys, tempfile, subprocess
from pathlib import Path
import cv2, numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mpp
from mediapipe.tasks.python import vision

FF, MODEL, FPS = sys.argv[1], sys.argv[2], int(sys.argv[3])
video = Path(sys.argv[4])
work = Path(tempfile.mkdtemp())
subprocess.run([FF, "-v", "error", "-y", "-i", str(video), "-vf", "fps=%d,scale=720:-1" % FPS,
                str(work / "f_%05d.jpg")], check=True)
files = sorted(work.glob("f_*.jpg"))
opts = vision.PoseLandmarkerOptions(
    base_options=mpp.BaseOptions(model_asset_path=MODEL),
    running_mode=vision.RunningMode.IMAGE, num_poses=1)
rows = []
with vision.PoseLandmarker.create_from_options(opts) as lm:
    for f in files:
        img = cv2.imread(str(f))
        if img is None:
            continue
        res = lm.detect(mp.Image(image_format=mp.ImageFormat.SRGB,
                                 data=cv2.cvtColor(img, cv2.COLOR_BGR2RGB)))
        if not res.pose_landmarks:
            continue
        p = res.pose_landmarks[0]
        nose, lw, rw, ls, rs, lh, rh = p[0], p[15], p[16], p[11], p[12], p[23], p[24]
        le, re = p[13], p[14]
        sh_y = (ls.y + rs.y) / 2
        body = max(abs(((lh.y + rh.y) / 2) - sh_y), 1e-3)   # 归一化单位下的体长
        # 阈值一律换成"体长倍数"，远近不同的画幅才不会误判
        elev = (sh_y - min(lw.y, rw.y, le.y, re.y)) / body
        arm_up = bool(elev > 0.10)
        near_face = bool(min(np.hypot(lw.x - nose.x, lw.y - nose.y),
                             np.hypot(rw.x - nose.x, rw.y - nose.y)) / body < 0.55)
        rows.append({
            "raise": arm_up,
            "elev": float(elev),
            "hand_face": bool(arm_up and near_face),
            "hip_x": float((lh.x + rh.x) / 2),
            "sh_w": float(max(abs(ls.x - rs.x), 1e-3)),
        })
if not rows:
    print(json.dumps({"error": "no pose"}))
    raise SystemExit(0)
n = len(rows)
xs = np.array([r["hip_x"] for r in rows])
sw = float(np.median([r["sh_w"] for r in rows]))
elevs = np.array([r["elev"] for r in rows])
print(json.dumps({
    "frames": n,
    "seconds": round(n / FPS, 2),
    "raise_ratio": round(sum(r["raise"] for r in rows) / n, 3),
    "arm_up_max": round(float(elevs.max()), 2),
    "arm_up_p90": round(float(np.percentile(elevs, 90)), 2),
    "hand_face_ratio": round(sum(r["hand_face"] for r in rows) / n, 3),
    "net_travel": round(float(abs(xs[-1] - xs[0])) / sw, 3),
    "moving_frames": int((np.abs(np.diff(xs)) / sw > 0.02).sum()),
    "yaw_swing": round(float(np.abs(np.diff(np.array([r["sh_w"] for r in rows]))).sum() / sw), 2),
}, ensure_ascii=False))
'''


def pose_profile(video: Path, pose_python: str, ff: str, model: str) -> dict:
    code = POSE_WORKER_CODE
    proc = subprocess.run([pose_python, "-c", code, ff, model, "5", str(video)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = ""
    for ln in reversed(proc.stdout.strip().splitlines()):
        if ln.strip().startswith("{"):
            line = ln.strip()
            break
    if not line:
        return {"error": (proc.stderr or "").strip()[-200:] or "pose worker 无输出"}
    try:
        return json.loads(line)
    except ValueError:
        return {"error": "pose worker 输出不是 JSON"}


# ---------------------------------------------------------------- 拼版
def contact_sheet(videos: list[tuple[int, Path]], out: Path, ff: str, cols: int = 12) -> bool:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return False
    tw, th = 150, 200
    rows = len(videos)
    sheet = Image.new("RGB", (cols * tw, rows * (th + 18)), (255, 255, 255))
    d = ImageDraw.Draw(sheet)
    tmp = Path(tempfile.mkdtemp())
    for r, (g, p) in enumerate(videos):
        subprocess.run([ff, "-y", "-v", "error", "-i", str(p),
                        "-vf", f"fps=1,scale={tw}:{th}:flags=lanczos",
                        "-frames:v", str(cols), str(tmp / f"g{g}_%02d.jpg")], check=True)
        files = sorted(tmp.glob(f"g{g}_*.jpg"))[:cols]
        d.text((4, r * (th + 18) + 2), f"G{g}", fill=(200, 0, 0))
        for c, f in enumerate(files):
            sheet.paste(Image.open(f).convert("RGB"), (c * tw, r * (th + 18) + 16))
            d.text((c * tw + 2, r * (th + 18) + 4), f"{c}s", fill=(0, 0, 0))
    sheet.save(out, quality=85)
    return True


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="款号参考体检：参考是否重复、动作类型够不够")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--source-root", default=DEFAULT_SOURCE)
    ap.add_argument("--local", action="store_true", help="改读项目 01_素材库")
    ap.add_argument("--out", help="输出目录（默认 <项目>/_check/<款号>/ref_audit；"
                                  "submit.py 从这个固定路径读闸门结果，别乱改）")
    ap.add_argument("--archive", action="store_true",
                    help="额外复制一份到 06_处理日志/<日期>_<款号>参考核对（留档用）")
    ap.add_argument("--pose-python", help="带 mediapipe 的 python；默认用 <项目>/.venv")
    ap.add_argument("--pose-model", help="姿态模型 .task；默认 <项目>/models/pose_landmarker_lite.task")
    ap.add_argument("--no-sheet", action="store_true")
    ap.add_argument("--no-cross-sku", action="store_true", help="跳过跨款参考复用扫描")
    ap.add_argument("--strict", action="store_true", help="WARN 也算失败")
    args = ap.parse_args()

    root = common.find_project(args.project)
    paths = common.project_paths(root)
    ff = common.ffmpeg(root)
    sku = args.sku.strip().upper()

    if args.local:
        mat_root = paths["materials"] / sku
        source_label = f"项目素材库 {mat_root}"
    else:
        mat_root = Path(args.source_root) / sku
        source_label = f"素材盘 {mat_root}"

    if not mat_root.is_dir():
        print(f"[FAIL] 找不到该款号素材：{mat_root}")
        print("       确认款号有没有写错；素材盘路径可用 --source-root 改。")
        return 1

    groups = sorted([d for d in mat_root.iterdir() if d.is_dir()],
                    key=lambda p: (len(p.name), p.name))
    if not groups:
        print(f"[FAIL] {mat_root} 下没有分组目录")
        return 1

    findings: list[dict] = []

    def add(level: str, code: str, msg: str, evidence: dict | None = None) -> None:
        findings.append({"level": level, "code": code, "message": msg,
                         "evidence": evidence or {}})

    rows = []
    refs: dict[int, Path] = {}
    imgs: dict[str, dict[int, Path | None]] = {"scene": {}, "back": {}, "flat": {}}

    for idx, gdir in enumerate(groups, 1):
        try:
            gno = int(gdir.name)
        except ValueError:
            gno = idx
        ref = pick(gdir, VIDEO_PATTERNS)
        imgs["scene"][gno] = pick(gdir, SCENE_PATTERNS)
        imgs["back"][gno] = pick(gdir, BACK_PATTERNS)
        imgs["flat"][gno] = pick(gdir, FLAT_PATTERNS)
        row = {"group": gno, "dir": gdir.name,
               "ref": None, "scene": None, "back": None, "flat": None}
        if ref:
            refs[gno] = ref
            meta = probe(ref, ff)
            row["ref"] = {"name": ref.name, "size": ref.stat().st_size,
                          "sha256": sha256_of(ref), "md5": hashlib.md5(ref.read_bytes()).hexdigest(),
                          **meta}
            if not meta["has_audio"]:
                add("WARN", "AUDIO_MISSING", f"G{gno} 动作参考没有音轨，成片只能另行配声",
                    {"group": gno, "file": ref.name})
        else:
            add("BLOCK", "REF_MISSING", f"G{gno} 没有动作参考视频", {"group": gno})
        for key, label in (("scene", "场景参考_01"), ("back", "场景参考_反面"), ("flat", "服装细节")):
            p = imgs[key][gno]
            if p:
                row[key] = {"name": p.name, "size": p.stat().st_size,
                            "md5": hashlib.md5(p.read_bytes()).hexdigest()}
        rows.append(row)

    # ---- 动作参考：内容级两两比对
    frames = {}
    for gno, p in refs.items():
        try:
            frames[gno] = gray_frames(p, ff)
        except Exception as exc:  # noqa: BLE001
            add("BLOCK", "REF_UNREADABLE", f"G{gno} 动作参考读不出帧：{exc}", {"group": gno})

    pairwise = []
    for a, b in itertools.combinations(sorted(frames), 2):
        mae, shift = align_mae(frames[a], frames[b])
        k = min(len(frames[a]), len(frames[b]))
        x, y = frames[a][:k].ravel(), frames[b][:k].ravel()
        corr = float(np.dot(x - x.mean(), y - y.mean()) /
                     (np.linalg.norm(x - x.mean()) * np.linalg.norm(y - y.mean()) + 1e-9))
        verdict = ("同一段内容" if mae < MAE_SAME else
                   "同场景同动作" if mae < MAE_DIFF else "不同内容")
        pairwise.append({"a": f"G{a}", "b": f"G{b}", "mae": round(mae, 2),
                         "shift_s": round(shift / FPS, 2), "corr": round(corr, 3),
                         "verdict": verdict})

    # 字节级重复
    by_md5: dict[str, list[int]] = {}
    for gno, p in refs.items():
        by_md5.setdefault(hashlib.md5(p.read_bytes()).hexdigest(), []).append(gno)
    for md5v, gs in by_md5.items():
        if len(gs) > 1:
            add("BLOCK", "REF_IDENTICAL",
                "动作参考字节完全相同：" + "、".join(f"G{g}" for g in gs) +
                "（同一文件复制多份，成片必然同动作）",
                {"groups": gs, "md5": md5v})

    # 内容级重复（不同 MD5、同一段）
    for row in pairwise:
        if row["mae"] < MAE_SAME:
            add("BLOCK", "REF_SAME_CONTENT",
                f"{row['a']} 与 {row['b']} 内容几乎一致（MAE {row['mae']}），"
                f"疑似同一条片子改封装/裁剪",
                {"pair": [row["a"], row["b"]], "mae": row["mae"], "shift_s": row["shift_s"]})

    # ---- 图片跨组重复
    for key, label, level in (("scene", "场景参考_01", "INFO"),
                              ("back", "场景参考_反面", "WARN"),
                              ("flat", "服装细节", "INFO")):
        m: dict[str, list[int]] = {}
        for gno, p in imgs[key].items():
            if p:
                m.setdefault(hashlib.md5(p.read_bytes()).hexdigest(), []).append(gno)
        for md5v, gs in m.items():
            if len(gs) > 1:
                if key == "flat":
                    add("INFO", "FLAT_SHARED",
                        "服装细节跨组是同一张（平铺图共用，正常）：" +
                        "、".join(f"G{g}" for g in gs), {"groups": gs, "md5": md5v})
                else:
                    add(level, "BACK_IDENTICAL" if key == "back" else "SCENE_IDENTICAL",
                        f"{label} 跨组完全相同：" + "、".join(f"G{g}" for g in gs) +
                        ("（同一张当各组背面，G2 以后背面一致性会崩）" if key == "back" else ""),
                        {"groups": gs, "md5": md5v})

    # ---- 跨款共用同一条参考（同质化的头号原因，只说事实不拦提交）
    if not args.no_cross_sku:
        cache_path = paths["check"] / "_ref_hash_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            if not isinstance(cache, dict):
                cache = {}
        except Exception:
            cache = {}
        scan_root = paths["materials"] if args.local else Path(args.source_root)
        index: dict[str, list[str]] = {}
        if scan_root.is_dir():
            for other in sorted(d for d in scan_root.iterdir() if d.is_dir()):
                if other.name.upper() == sku:
                    continue
                for gdir in sorted(d for d in other.iterdir() if d.is_dir()):
                    for p in gdir.glob("*.mp4"):
                        if p.is_file():
                            mv = md5_cached(p, cache)
                            if mv:
                                index.setdefault(mv, []).append(f"{other.name}/G{gdir.name}")
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        for r in rows:
            if not r["ref"]:
                continue
            others = index.get(r["ref"]["md5"])
            if others:
                add("WARN", "REF_SHARED_ACROSS_SKU",
                    f"G{r['group']} 的动作参考与其它款完全相同（{len(others)} 处）：" +
                    "、".join(others[:6]) + ("…" if len(others) > 6 else "") +
                    "；跨款共用同一条参考，成片动作必然趋同",
                    {"group": r["group"], "md5": r["ref"]["md5"], "others": others})

    # ---- 规格一致性
    specs = {(r["ref"]["width"], r["ref"]["height"], r["ref"]["audio_hz"])
             for r in rows if r["ref"]}
    if len(specs) > 1:
        add("WARN", "MIXED_SPEC",
            "各组动作参考的分辨率/音频采样率不一致：" +
            "；".join(f"G{r['group']}={r['ref']['width']}x{r['ref']['height']}@{r['ref']['audio_hz']}"
                      for r in rows if r["ref"]),
            {"specs": sorted(str(s) for s in specs)})

    # ---- 动作画像（自动找带 mediapipe 的解释器；找不到就只做参考比对）
    profiles = {}
    pose_py, pose_mdl = resolve_pose(root, args.pose_python, args.pose_model)
    if pose_py:
        for gno, p in sorted(refs.items()):
            prof = pose_profile(p, pose_py, ff, pose_mdl)
            profiles[gno] = prof
            if "error" in prof:
                add("WARN", "POSE_FAILED", f"G{gno} 姿态检测失败：{prof['error'][:80]}",
                    {"group": gno})
        good = {g: p for g, p in profiles.items() if "error" not in p}
        for g, p in good.items():
            walk = p["net_travel"] >= 0.30
            raise_ = p["raise_ratio"] >= 0.08
            p["class"] = ("走动+抬手" if walk and raise_ else
                          "走动" if walk else
                          "站定+抬手" if raise_ else "站定摆拍")
        kinds = {p.get("class") for p in good.values() if p.get("class")}
        if good:
            if len(kinds) <= 2 and len(good) >= 3:
                add("WARN", "REF_LOW_VARIETY",
                    f"{len(good)} 组参考只出现 {len(kinds)} 类动作（{'、'.join(sorted(kinds))}），"
                    "成片会自然收敛成这两类；要出差异化必须在窗口或提示词里补事件",
                    {"classes": sorted(kinds)})
            flat = [f"G{g}" for g, p in good.items()
                    if p["net_travel"] < 0.15 and p["raise_ratio"] < 0.08]
            if flat:
                detail = {g: {"net_travel": good[int(g[1:])]["net_travel"],
                              "arm_up_max": good[int(g[1:])].get("arm_up_max")} for g in flat}
                add("WARN", "REF_FLAT",
                    "动作贫瘠（位移 <0.15 肩宽且手部最大抬升 <0.10 体长）：" + "、".join(flat) +
                    "；这类参考最容易被模型自己加戏成抬手比耶",
                    {"groups": flat, "detail": detail})

    # ---- 汇总
    if not profiles:
        add("WARN", "POSE_SKIPPED",
            "没做动作画像（缺 mediapipe 或姿态模型）：只查了参考是否重复，没查四组动作类型够不够",
            {"hint": "init_project.py --venv --install"})
    blocks = [f for f in findings if f["level"] == "BLOCK"]
    warns = [f for f in findings if f["level"] == "WARN"]
    verdict = ("不合规，先换参考再提交" if blocks else
               "可用但有风险项" if warns else "通过")

    stamp = datetime.now().strftime("%Y%m%d")
    # ★ 固定落盘：submit.py 的参考闸门只认这个路径。
    #   换款、换素材都必须重跑，否则闸门按「报告过期」拦下。
    out_dir = Path(args.out) if args.out else (paths["check"] / sku / "ref_audit")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 素材指纹：给 submit.py 判断「报告是不是在这次素材之后跑的」
    mat_mtime = max((p.stat().st_mtime for p in mat_root.rglob("*") if p.is_file()), default=0)
    material_mtime_max = (datetime.fromtimestamp(mat_mtime).isoformat(timespec="seconds")
                          if mat_mtime else None)

    sheet_path = out_dir / "四条参考_1fps拼版.jpg"
    sheet_ok = False
    if not args.no_sheet and refs:
        sheet_ok = contact_sheet(sorted(refs.items()), sheet_path, ff)

    report = {
        "sku": sku, "generated_at": datetime.now().isoformat(timespec="seconds"),
        "source": source_label, "groups": len(rows), "verdict": verdict,
        "pass_gate": not blocks,
        "pose_checked": bool(profiles),
        "counts": {"block": len(blocks), "warn": len(warns)},
        "material_mtime_max": material_mtime_max,
        "refs": rows, "pairwise": pairwise,
        "profiles": {str(g): p for g, p in sorted(profiles.items())},
        "findings": findings,
        "evidence": {"sheet": sheet_path.name if sheet_ok else None,
                     "note": "MAE<8 判同一段；8-10 存疑；>10 判不同内容"},
    }
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    (out_dir / "pairwise.json").write_text(json.dumps(pairwise, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    if profiles:
        (out_dir / "action_profile.json").write_text(
            json.dumps({str(g): p for g, p in sorted(profiles.items())},
                       ensure_ascii=False, indent=1), encoding="utf-8")

    if args.archive:
        arch = paths["logs"] / f"{stamp}_{sku}参考核对"
        if arch.resolve() != out_dir.resolve():
            arch.mkdir(parents=True, exist_ok=True)
            for f in out_dir.iterdir():
                if f.is_file():
                    (arch / f.name).write_bytes(f.read_bytes())

    # ---- 控制台（纯 ASCII，避免 Windows 终端乱码；中文详情看 report.json）
    cls_ascii = {"站定摆拍": "stand", "站定+抬手": "stand+arm",
                 "走动": "walk", "走动+抬手": "walk+arm"}
    verdict_ascii = {"同一段内容": "SAME", "同场景同动作": "SIMILAR", "不同内容": "DIFFERENT"}
    hint = {
        "REF_IDENTICAL": "reference videos are byte-identical across groups",
        "REF_SAME_CONTENT": "two references are the same clip after re-encode/trim",
        "REF_MISSING": "a group has no action reference video",
        "REF_UNREADABLE": "reference cannot be decoded",
        "AUDIO_MISSING": "reference has no audio track",
        "BACK_IDENTICAL": "back-view image identical across groups",
        "SCENE_IDENTICAL": "scene image identical across groups",
        "FLAT_SHARED": "flat-lay detail image shared across groups (normal)",
        "MIXED_SPEC": "references differ in resolution / audio sample rate",
        "REF_LOW_VARIETY": "all references fall into very few action types",
        "REF_FLAT": "reference has almost no travel and no arm raise",
        "REF_SHARED_ACROSS_SKU": "reference is also used by other SKUs (motion will converge)",
        "POSE_FAILED": "pose detection failed on this group",
    }
    print("")
    print(f"{sku}  REF AUDIT  |  {len(rows)} groups  |  {mat_root}")
    print("-" * 92)
    print(f"{'grp':<5}{'MB':>7}{'md5_12':>15}{'res':>11}{'dur':>8}{'audio':>8}  class")
    for r in rows:
        if not r["ref"]:
            print(f"{r['group']:<5}{'-':>7}{'-':>15}{'-':>11}{'-':>8}{'-':>8}  MISSING")
            continue
        ref = r["ref"]
        prof = profiles.get(r["group"], {})
        cls = cls_ascii.get(prof.get("class", ""), "-")
        print(f"{r['group']:<5}{ref['size'] / 1048576:>7.2f}{ref['md5'][:12]:>15}"
              f"{str(ref['width']) + 'x' + str(ref['height']):>11}"
              f"{str(ref['duration']):>8}{str(ref['audio_hz']):>8}  {cls}")

    if pairwise:
        print("")
        print("pairwise (2 fps gray, +/-4 s align):  MAE<8 SAME | 8-10 SIMILAR | >10 DIFFERENT")
        print(f"{'pair':<10}{'MAE':>8}{'shift':>8}{'corr':>8}  verdict")
        for row in pairwise:
            print(f"{row['a'] + '-' + row['b']:<10}{row['mae']:>8}{row['shift_s']:>8}"
                  f"{row['corr']:>8}  {verdict_ascii.get(row['verdict'], row['verdict'])}")

    if findings:
        print("")
        print("findings:")
        for f in findings:
            print(f"  [{f['level']}] {f['code']:<18} {hint.get(f['code'], '')}")
            if f["code"] == "REF_SHARED_ACROSS_SKU":
                ev = f["evidence"]
                print(f"        G{ev['group']}: " + ", ".join(ev["others"][:8]) +
                      (" ..." if len(ev["others"]) > 8 else ""))

    print("-" * 92)
    state = "FAIL" if blocks else ("PASS (with warnings)" if warns else "PASS")
    print(f"VERDICT: {state}   block={len(blocks)} warn={len(warns)}")
    print(f"report: {out_dir / 'report.json'}")
    if sheet_ok:
        print(f"sheet : {sheet_path}")

    if blocks:
        return 1
    if warns and args.strict:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
