#!/usr/bin/env python3
"""轮询任务 + 下载供应商原片（只读，不花钱）。

注意两点：
  · 查询是 GET，不扣费，可以放心重试；下载必须用 `curl -L -C -` 续传，
    9 MB 的片子这条链路通常要断 1–2 次。
  · 供应商回的是 10.13 s / 243 帧，比 10.000 s 多几帧，交给 finalize.py 裁。

用法：
  python scripts/poll.py --sku KF96467710 --limit 900 --download
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="轮询任务并下载原片")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--limit", type=float, default=900, help="最长等待秒数")
    ap.add_argument("--download", action="store_true", help="完成后下载原片")
    ap.add_argument("--versions", nargs="*")
    args = ap.parse_args()

    root = common.find_project(args.project)
    cfg = common.load_config(root, args.sku)
    paths = common.project_paths(root)
    sys.path.insert(0, str(root / "src"))
    from kling_workflow.minimax_client import MiniMaxClient
    from kling_workflow.seedance_credentials import SeedanceCredentialStore

    log = paths["logs"] / f"{cfg['batch']}_{args.sku}{len(cfg['groups'])}组"
    log.mkdir(parents=True, exist_ok=True)

    tasks = {}
    for f in sorted((paths["bridge"] / "tasks" / args.sku).glob("v*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("task_id") and (not args.versions or f.stem in args.versions):
            tasks[f.stem] = d["task_id"]
    if not tasks:
        raise SystemExit("没有已提交的任务记录（.minimax_bridge/tasks/<款号>/），先跑 submit.py")

    client = MiniMaxClient(api_key_provider=SeedanceCredentialStore().read)
    done: dict[str, dict] = {}
    t0 = time.time()
    while time.time() - t0 < args.limit and len(done) < len(tasks):
        for ver, task in tasks.items():
            if ver in done:
                continue
            try:
                status, payload = client.get_task(cfg["model"], task)
            except Exception as exc:
                print(f"[{time.strftime('%H:%M:%S')}] {ver} 查询异常 {type(exc).__name__}", flush=True)
                continue
            payload = payload if isinstance(payload, dict) else {}
            state = str(payload.get("status") or payload.get("task_status") or "?")
            print(f"[{time.strftime('%H:%M:%S')}] {ver} HTTP {status} status={state} "
                  f"progress={payload.get('progress')}", flush=True)
            if state.lower() in {"succeeded", "success", "completed", "failed", "error", "cancelled"}:
                (log / f"task_query_final_{ver}.json").write_text(
                    json.dumps({"http_status": status, "payload": payload},
                               ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
                done[ver] = payload
        if len(done) < len(tasks):
            time.sleep(12)

    for ver in tasks:
        print(ver, "OK" if ver in done else "PENDING", tasks[ver])

    if not args.download:
        return 0

    grp = {g["version"]: g for g in cfg["groups"]}
    raw_dir = paths["delivery"] / args.sku / cfg["batch"] / "_记录" / "供应商原片"
    raw_dir.mkdir(parents=True, exist_ok=True)
    for ver, payload in done.items():
        url = payload.get("video_url") or (payload.get("output") or {}).get("video_url")
        if not url:
            print(f"{ver} 没有 video_url（可能失败），跳过下载")
            continue
        dst = raw_dir / f"{ver}_raw.mp4"
        for attempt in range(1, 4):
            r = subprocess.run(["curl", "-sS", "-L", "-C", "-", "--retry", "5", "--retry-all-errors",
                                "--max-time", "600", "-o", str(dst), url],
                               capture_output=True, text=True)
            if dst.is_file() and dst.stat().st_size > 100_000:
                break
            print(f"  {ver} 下载重试 {attempt}: {r.stderr.strip()[:120]}")
        if dst.is_file():
            meta = common.probe_meta(dst, root)
            print(f"{ver} -> {dst.name} {dst.stat().st_size/1e6:.2f} MB "
                  f"{meta['width']}x{meta['height']} {meta['stream_duration']:.2f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
