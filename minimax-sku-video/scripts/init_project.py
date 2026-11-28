#!/usr/bin/env python3
"""一键建立「图生视频」项目骨架。

做五件事：
  1. 建全部目录（素材库 / 待审核 / 历史版本 / 处理日志 / _check / .minimax_bridge）
  2. 把技能自带的引擎代码装进 src/kling_workflow 与 tools
  3. 写 requirements.txt、.env.example、项目说明、项目标记文件
  4. 可选：建 .venv 并装依赖（--venv / --install）
  5. 可选：下载姿态模型 pose_landmarker_lite.task（骨架分镜要用）
最后跑一次自检：ffmpeg 能否调用、引擎能否 import、目录是否齐、Key 是否配好。

用法：
  python scripts/init_project.py --root "D:/视频项目" --venv --install
  python scripts/init_project.py --root . --ffmpeg "D:/ShadowBot/shadowbot-6.3.39/ffmpeg.exe"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

DIRS = [
    "01_素材库",
    "02_待审核/MiniMax",
    "04_历史版本",
    "06_处理日志",
    "_check",
    ".minimax_bridge/manifests",
    ".minimax_bridge/prompts",
    ".minimax_bridge/tasks",
    ".minimax_bridge/receipts",
    ".minimax_bridge/approvals",
    ".minimax_bridge/previews",
    ".minimax_bridge/candidates",
    "tools",
    "src",
    "models",
    "04系统配置",
]

REQS = """httpx==0.28.1
pillow==11.3.0
numpy==2.2.6
keyring==25.7.0
opencv-python-headless==4.12.0.88
mediapipe==1.2.2
"""

POSE_URL = ("https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
            "pose_landmarker_lite/float16/latest/pose_landmarker_lite.task")

README = """# 图生视频项目（MiniMax 9:16 十条）

这个目录由 minimax-sku-video 技能生成。全部工作流都靠技能里的脚本跑，不要手搓一次性脚本。

## 一次性准备
1. `python scripts/init_project.py --root . --venv --install`（已跑过可跳过）
2. 配 API Key：项目根写 `.env`，内容 `AIMAXHUG_API_KEY=xxxx`；或设同名环境变量
3. ffmpeg：设环境变量 `KLING_FFMPEG` 指向 ffmpeg.exe，或把 ffmpeg.exe 放进 `tools/`

## 每接一款
```
python scripts/new_sku.py --sku <款号>
# 读 _check/<款号>/sku.config.json，把 TODO 填完（人物/服装/场景/动作节拍）
python scripts/skeleton.py --sku <款号>
python scripts/build_pack.py --sku <款号>
python scripts/make_prompts.py --sku <款号>
python scripts/submit.py --sku <款号> --stage a
python scripts/submit.py --sku <款号> --stage b --versions v001 v002 v003 v004
python scripts/poll.py --sku <款号>
python scripts/finalize.py --sku <款号> --group 1 --version v001
python scripts/qc.py --sku <款号>
```

成片落在 `02_待审核/MiniMax/<款号>/<日期>/00_直接看成片/`。
详细口径见技能里的 references/pipeline.md。
"""


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def main() -> int:
    ap = argparse.ArgumentParser(description="建立图生视频项目骨架")
    ap.add_argument("--root", default=".", help="项目根目录（不存在会创建）")
    ap.add_argument("--venv", action="store_true", help="创建 .venv")
    ap.add_argument("--install", action="store_true", help="在 .venv 里 pip install 依赖")
    ap.add_argument("--ffmpeg", help="ffmpeg.exe 路径；给了就复制进 tools/ 并写进环境提示")
    ap.add_argument("--skip-model", action="store_true", help="不下载姿态模型")
    ap.add_argument("--force", action="store_true", help="覆盖已有说明文件")
    args = ap.parse_args()

    root = Path(args.root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    print(f"项目根：{root}")

    for d in DIRS:
        (root / d).mkdir(parents=True, exist_ok=True)
    print(f"[1/5] 目录 {len(DIRS)} 个 就绪")

    n = common.copy_engine(root)
    print(f"[2/5] 引擎代码：复制 {n} 个文件（src/kling_workflow + tools）")

    req = root / "requirements.txt"
    if not req.exists() or args.force:
        req.write_text(REQS, encoding="utf-8")
    envf = root / ".env.example"
    if not envf.exists() or args.force:
        envf.write_text("AIMAXHUG_API_KEY=\n", encoding="utf-8")
    readme = root / "PROJECT_README.md"
    if not readme.exists() or args.force:
        readme.write_text(README, encoding="utf-8")
    marker = root / common.MARKER
    marker.write_text(json.dumps({
        "schema": "minimax-sku-project/1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "engine": "bundled",
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("[3/5] requirements.txt / .env.example / PROJECT_README.md / 项目标记 就绪")

    if args.ffmpeg:
        src = Path(args.ffmpeg).expanduser()
        if not src.is_file():
            print(f"  ! ffmpeg 路径无效：{src}")
        else:
            dst = root / "tools" / "ffmpeg.exe"
            if not dst.exists():
                shutil.copy2(src, dst)
            # 引擎里 media_audio 等模块会读这个文件，不写会报「找不到 ffmpeg」
            cfg_dir = root / "04系统配置"
            cfg_dir.mkdir(parents=True, exist_ok=True)
            (cfg_dir / "ffmpeg路径.txt").write_text(str(dst) + "\n", encoding="utf-8")
            print(f"[4/5] ffmpeg 已就位：{dst}")
    else:
        found = shutil.which("ffmpeg")
        print(f"[4/5] ffmpeg：{'PATH 上找到 ' + found if found else '未配置，请设 KLING_FFMPEG 或放 tools/ffmpeg.exe'}")

    model = root / "models" / "pose_landmarker_lite.task"
    if not args.skip_model and not model.exists():
        try:
            model.parent.mkdir(parents=True, exist_ok=True)
            print("      下载姿态模型…")
            urllib.request.urlretrieve(POSE_URL, model)
            print(f"[5/5] 姿态模型 {model.stat().st_size/1e6:.1f} MB")
        except Exception as exc:
            print(f"[5/5] 姿态模型下载失败（{type(exc).__name__}）：骨架分镜可改用 --fixed 模式，或手动放到 {model}")
    else:
        print(f"[5/5] 姿态模型：{'已存在' if model.exists() else '跳过'}")

    if args.venv:
        venv = root / ".venv"
        if not (venv / "Scripts" / "python.exe").exists() and not (venv / "bin" / "python").exists():
            print("创建 .venv …")
            r = run([sys.executable, "-m", "venv", str(venv)])
            if r.returncode:
                print(r.stderr[-500:])
        py = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if args.install and py.exists():
            print("安装依赖（可能要 1–3 分钟）…")
            r = run([str(py), "-m", "pip", "install", "-q", "-r", str(req)])
            print("  依赖安装完成" if r.returncode == 0 else f"  安装失败：{r.stderr[-500:]}")

    # ------------------------------------------------------------- 自检
    print("\n=== 自检 ===")
    ok = True
    missing = [d for d in DIRS if not (root / d).is_dir()]
    print(f"目录      : {'齐' if not missing else '缺 ' + ', '.join(missing)}")
    ok &= not missing
    for f in ("src/kling_workflow/minimax_bridge.py", "tools/submit_url_mode.py",
              "tools/check_camera.py", "tools/check_prompt_rules.py"):
        exists = (root / f).is_file()
        print(f"引擎 {f:<45}: {'OK' if exists else '缺'}")
        ok &= exists
    try:
        ff = common.ffmpeg(root)
        r = run([ff, "-version"])
        print(f"ffmpeg    : OK  {ff}")
        ok &= r.returncode == 0
    except SystemExit as exc:
        print(f"ffmpeg    : {exc}")
        ok = False
    try:
        key = common.api_key(root)
        print(f"API Key   : 已配置（{key[:6]}…{key[-4:]}）")
    except SystemExit as exc:
        print(f"API Key   : 未配置 —— {exc}")
        ok = False
    py = root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if py.is_file():
        r = run([str(py), "-c", "import mediapipe, numpy"])
        if r.returncode == 0:
            print("姿态环境  : OK  mediapipe 可用")
        else:
            print("姿态环境  : 缺 mediapipe —— pip install mediapipe（参考体检的动作画像要用）")
    else:
        print("姿态环境  : 无 .venv —— 加 --venv --install 重建")
    print(f"姿态模型  : {model}" + ("" if model.exists() else "（缺：参考体检/骨架只能降级）"))

    print("\n下一步：")
    print("  1) 读 references/pipeline.md 了解闸门口径")
    print(f"  2) python scripts/new_sku.py --sku <款号> --project \"{root}\"")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
