#!/usr/bin/env python3
"""共享工具：项目定位、款配置读写、ffmpeg、API Key、素材探测。

所有脚本都从这里取路径，禁止在业务脚本里写死盘符。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

MARKER = ".minimax_sku_project.json"
ENGINE = Path(__file__).resolve().parent.parent / "assets" / "engine"


# ---------------------------------------------------------------- 项目定位
def find_project(explicit: str | os.PathLike | None = None) -> Path:
    """按 --project > 环境变量 > 当前目录向上找标记 > 报错的顺序定位项目根。"""
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_dir():
            raise SystemExit(f"项目目录不存在：{p}")
        return p
    env = os.environ.get("MINIMAX_SKU_PROJECT")
    if env:
        return Path(env).expanduser().resolve()
    for base in [Path.cwd(), Path(__file__).resolve().parent]:
        for cand in [base, *base.parents]:
            if (cand / MARKER).is_file() or (cand / ".minimax_bridge").is_dir():
                return cand
    raise SystemExit(
        "找不到项目根。先在空目录里跑 `python <skill>/scripts/init_project.py --root <目录>`，"
        "或用 --project / 环境变量 MINIMAX_SKU_PROJECT 指定。"
    )


def project_paths(root: Path) -> dict[str, Path]:
    return {
        "root": root,
        "materials": root / "01_素材库",
        "delivery": root / "02_待审核" / "MiniMax",
        "packs": root / "04_历史版本",
        "logs": root / "06_处理日志",
        "check": root / "_check",
        "bridge": root / ".minimax_bridge",
        "tools": root / "tools",
        "src": root / "src",
    }


# ---------------------------------------------------------------- 款配置
def config_path(root: Path, sku: str) -> Path:
    return root / "_check" / sku / "sku.config.json"


def load_config(root: Path, sku: str) -> dict:
    p = config_path(root, sku)
    if not p.is_file():
        raise SystemExit(f"缺款配置：{p}\n先跑 scripts/new_sku.py --sku {sku} 生成模板。")
    # utf-8-sig：新同事常用记事本 / PowerShell Set-Content 改配置，会带 BOM
    return json.loads(p.read_text(encoding="utf-8-sig"))


def save_config(root: Path, sku: str, cfg: dict) -> None:
    p = config_path(root, sku)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def group_versions(cfg: dict) -> list[str]:
    return [g["version"] for g in cfg["groups"]]


def get_group(cfg: dict, group: int) -> dict:
    for g in cfg["groups"]:
        if int(g["group"]) == int(group):
            return g
    raise SystemExit(f"配置里没有第 {group} 组")


# ---------------------------------------------------------------- 环境
def ffmpeg(root: Path | None = None) -> str:
    env = os.environ.get("KLING_FFMPEG")
    if env and Path(env).is_file():
        return env
    if root:
        local = root / "tools" / "ffmpeg.exe"
        if local.is_file():
            return str(local)
    found = shutil.which("ffmpeg")
    if found:
        return found
    raise SystemExit("找不到 ffmpeg：设 KLING_FFMPEG，或把 ffmpeg.exe 放进项目的 tools/。")


def api_key(root: Path | None = None) -> str:
    env = os.environ.get("AIMAXHUG_API_KEY")
    if env:
        return env.strip()
    candidates = []
    if root:
        candidates.append(root / ".env")
    candidates.append(Path.home() / ".workbuddy" / "skills" / "aimaxhug-generation" / ".env")
    for f in candidates:
        if f.is_file():
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.startswith("AIMAXHUG_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("\r")
    try:  # 最后试系统钥匙串
        if root:
            sys.path.insert(0, str(root / "src"))
        from kling_workflow.seedance_credentials import SeedanceCredentialStore
        return SeedanceCredentialStore().read()
    except Exception:
        raise SystemExit(
            "没有 AIMAXHUG API Key。三选一：设环境变量 AIMAXHUG_API_KEY；"
            "在项目根写 .env（AIMAXHUG_API_KEY=...）；或装 aimaxhug-generation skill 并配好 key。"
        )


def ensure_engine(root: Path) -> None:
    """项目里必须有引擎代码（src/kling_workflow + tools），否则从技能资产里补。"""
    need = [root / "src" / "kling_workflow" / "minimax_bridge.py", root / "tools" / "submit_url_mode.py"]
    if all(p.is_file() for p in need):
        return
    if not ENGINE.is_dir():
        raise SystemExit(f"项目缺引擎且技能资产里没有 engine/：{ENGINE}")
    copy_engine(root)


def copy_engine(root: Path) -> int:
    n = 0
    for sub in ("src/kling_workflow", "tools"):
        src = ENGINE / sub
        dst = root / sub
        if not src.is_dir():
            continue
        dst.mkdir(parents=True, exist_ok=True)
        for f in src.glob("*.py"):
            target = dst / f.name
            if target.exists() and target.read_bytes() == f.read_bytes():
                continue
            shutil.copy2(f, target)
            n += 1
    return n


# ---------------------------------------------------------------- 素材探测
def probe_duration(video: Path, root: Path | None = None) -> float:
    """视频流真实时长（秒）。

    ★ 容器里的 Duration 会比视频流长 0.1–0.3 s；窗口上限必须用这个真值，
      否则 `end > duration + 0.001` 校验会把整组拒掉（KF96409880 G3 踩过）。
    """
    if root:
        try:
            sys.path.insert(0, str(root / "src"))
            from kling_workflow.muse_bridge import probe_video_source
            return float(probe_video_source(video))
        except Exception:
            pass
    out = subprocess.run([ffmpeg(root), "-hide_banner", "-i", str(video)],
                         capture_output=True, text=True).stderr
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+\.\d+)", out)
    if not m:
        raise SystemExit(f"读不到时长：{video}")
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


def probe_meta(video: Path, root: Path | None = None) -> dict:
    out = subprocess.run([ffmpeg(root), "-hide_banner", "-i", str(video)],
                         capture_output=True, text=True).stderr
    w = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", out)
    fps = re.search(r"(\d+(?:\.\d+)?)\s+fps", out)
    audio = "Audio:" in out
    return {
        "width": int(w.group(1)) if w else None,
        "height": int(w.group(2)) if w else None,
        "fps": float(fps.group(1)) if fps else None,
        "has_audio": audio,
        "stream_duration": probe_duration(video, root),
    }


def sha256(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
