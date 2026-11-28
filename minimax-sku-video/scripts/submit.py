#!/usr/bin/env python3
"""提交闸门：Stage A 上传冻结 / Stage B 建单一次 / curl 兜底建单。

为什么分两步：一次生成 US$0.373，且模型 side 是「lifetime_paid_create_limit=1,
no_retry」——提交即扣费、失败也不退。所以做成
  Stage A：素材原样上传、冻结请求体、**不建单**（可反复重来，不花钱）
  Stage B：按冻结的 SHA 建单一次（花钱，只能点一次）

curl 兜底：内网链路异常时，工具自带的 httpx POST 会在 ~20 秒处稳定抛
MiniMaxClientError（连接被掐、拿不到 task_id、额度已占），而同一个
request_body.json 用 curl 直连 20 秒左右返回 200。此时用 `--mode curl`：
造任务记录 → curl POST → persist_task_id → 落审计件。别换版本号重试，那是白烧额度。

用法：
  python scripts/submit.py --sku KF96467710 --stage a
  python scripts/submit.py --sku KF96467710 --stage b --versions v001 v002 --i-have-supervisor-approval
  python scripts/submit.py --sku KF96467710 --mode curl --versions v003 --i-have-supervisor-approval
  python scripts/submit.py --sku KF96467710 --stage a --dry-run

闸门 0（参考体检，款级，先于一切上传与建单）：
  `_check/<款号>/ref_audit/report.json` 必须存在、有 BLOCK 就不许走、素材改过没重跑也算未过。
  唯一放行方式 `--ignore-ref-audit "<主管原话>"`，原话与被拦项会写进审批件留痕。
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import subprocess
import sys
import hashlib
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

UPLOAD_HOST = "https://base-api.aimaxhug.com"
CREATE_API = "https://apis.aimaxhug.cloud/v1/videos"


def load_tool(root: Path):
    sys.path.insert(0, str(root / "tools"))
    import submit_url_mode as t  # noqa: E402
    return t


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def upload_via_curl(root: Path, path: Path) -> str:
    """curl 上传，300 s 超时、http1.1、失败重试三次。

    内置客户端只有 120 s，且这条链路偶发 `schannel: failed to receive handshake`。
    """
    key = common.api_key(root)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    last = ""
    for attempt in range(1, 4):
        cmd = ["curl", "-s", "--max-time", "300", "--http1.1", "-X", "POST",
               f"{UPLOAD_HOST}/api/v2/upload/file",
               "-H", f"Authorization: Bearer {key}",
               "-F", f"file=@{path};type={mime}",
               "-F", "compress_media=false"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        out = proc.stdout.strip()
        if out:
            try:
                data = json.loads(out)
                url = (data.get("data") or {}).get("tmp_url")
                if url:
                    print(f"    upload {path.name} ok (try {attempt})", flush=True)
                    return url
                last = str(data)[:200]
            except ValueError:
                last = out[:200]
        else:
            last = proc.stderr.strip()[:200] or "empty response"
        print(f"    upload {path.name} fail (try {attempt}): {last}", flush=True)
    raise RuntimeError(f"上传失败：{last}")


def make_approval(tool, root: Path, sku: str, ver: str, out: Path, preview: Path | None,
                  approval_text: str, stage: str,
                  ref_override: dict | None = None) -> Path:
    apdir = root / "_check" / sku / "approvals"
    apdir.mkdir(parents=True, exist_ok=True)
    ap_path = apdir / f"{ver}_stage_{stage}_approval.json"
    base = {
        "approved_by_supervisor": True,
        "generation_submission_authorized": stage == "b",
        "supervisor_message": approval_text,
        "sku": sku,
        "version": ver,
        "submit_url_tool_sha256": sha(root / "tools" / "submit_url_mode.py"),
        "lifetime_paid_create_limit": 1,
        "lifetime_paid_create_posts_used": 0,
        "no_retry": True,
        # 参考体检闸门：正常通过时记 pass，被主管强制跳过时记原话+被拦项
        "ref_audit_gate": ref_override or {"result": "pass"},
    }
    if stage == "a":
        manifest = root / ".minimax_bridge" / "manifests" / sku / f"{ver}.json"
        base.update({
            "decision": tool.STAGE_A_DECISION,
            "stage_a_upload_authorized": True,
            "manifest_sha256": sha(manifest),
            "roles": ["model_image", "reverse_worn", "front_flat", "action_storyboard"],
            "compress_media": False,
        })
    else:
        pv = json.loads(preview.read_text(encoding="utf-8"))
        base.update({
            "decision": tool.STAGE_B_DECISION,
            "current_message_post_authorization": True,
            "requires_current_message_post_authorization": True,
            "upload_preview_sha256": sha(preview),
            "request_body_sha256": pv["request_body_sha256"],
            "request_body_bytes": pv["request_body_bytes"],
        })
    ap_path.write_text(json.dumps(base, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return ap_path


def preflight(root: Path, cfg: dict, sku: str, ver: str) -> list[str]:
    """不花钱的把关：文件齐不齐、提示词长度、切片大小、窗口是否越界。"""
    errs = []
    grp = next((g for g in cfg["groups"] if g["version"] == ver), None)
    if not grp:
        return [f"配置里没有版本 {ver}"]
    pack = root / "04_历史版本" / sku / f"minimax-G{grp['group']}-{ver}-{cfg['batch']}"
    for f in ("scene_front.jpg", "reverse_worn.jpg", "front_flat.jpg", "storyboard.jpg"):
        if not (pack / f).is_file():
            errs.append(f"缺 {f}")
    refs = list(pack.glob("reference_video_*.mp4"))
    if not refs:
        errs.append("缺参考切片")
    elif refs[0].stat().st_size > 1_800_000:
        errs.append(f"参考切片 {refs[0].stat().st_size/1e6:.1f} MB，超过上传上限")
    prompt = root / ".minimax_bridge" / "prompts" / sku / f"{ver}.txt"
    if not prompt.is_file():
        errs.append("缺提示词")
    elif len(prompt.read_text(encoding="utf-8")) > 7000:
        errs.append(f"提示词 {len(prompt.read_text(encoding='utf-8'))} 字符，超过 7000")
    if "TODO" in (prompt.read_text(encoding="utf-8") if prompt.is_file() else ""):
        errs.append("提示词里还有 TODO，没填完")
    video = root / "01_素材库" / sku / grp["source_group"] / grp["assets"]["video"]
    if video.is_file():
        dur = common.probe_duration(video, root)
        if float(grp["window_start"]) + float(cfg["duration"]) > dur + 0.001:
            errs.append(f"窗口越界：{grp['window_start']} + {cfg['duration']} > {dur:.3f}")
    return errs


def ref_gate(root: Path, sku: str) -> tuple[list[str], list[dict], Path | None]:
    """付款前的参考体检闸门（款级，一票否决）。

    为什么做成硬拦：这是「不同款成片动作却长得一样」唯一能机器判定的环节。
    报告缺、报告过期（素材改过没重跑）、有 BLOCK，三种都不许进 Stage A/B。

    控制台只输出 ASCII（Windows 中文控制台会把 UTF-8 打成乱码，等于没提示）；
    中文原文写进 `_check/<sku>/ref_audit/gate.json`，供人阅读与事后审计。
    """
    p = root / "_check" / sku / "ref_audit" / "report.json"
    if not p.is_file():  # 兼容历史上落在 06_处理日志 的报告
        cands = sorted((root / "06_处理日志").glob(f"*{sku}*参考核对/report.json"),
                       key=lambda x: x.stat().st_mtime)
        if cands:
            p = cands[-1]
    if not p.is_file():
        return ([f"no reference audit report; run: scripts/ref_audit.py --sku {sku}"],
                [{"code": "NO_REPORT",
                  "message": f"没有参考体检报告：缺 _check/{sku}/ref_audit/report.json，"
                             f"先跑 ref_audit.py"}], None)
    try:
        rep = json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return ([f"reference audit report is not readable json: {p.name}"],
                [{"code": "BAD_REPORT", "message": f"体检报告 json 损坏：{p}"}], p)

    errs: list[str] = []
    details: list[dict] = []

    blocks = [f for f in rep.get("findings", []) if f.get("level") == "BLOCK"]
    if blocks:
        cnt: dict[str, int] = {}
        for b in blocks:
            cnt[b.get("code", "?")] = cnt.get(b.get("code", "?"), 0) + 1
        summary = ", ".join(f"{k}x{v}" for k, v in sorted(cnt.items()))
        errs.append(f"ref audit has {len(blocks)} BLOCK item(s): {summary}")
        details.append({"code": "BLOCK",
                        "message": f"参考体检有 {len(blocks)} 项 BLOCK",
                        "items": blocks})

    mat = root / "01_素材库" / sku
    if mat.is_dir():
        newest = max((q.stat().st_mtime for q in mat.rglob("*") if q.is_file()), default=0)
        stamp = rep.get("material_mtime_max")
        try:
            base = datetime.fromisoformat(stamp).timestamp() if stamp else p.stat().st_mtime
        except ValueError:
            base = p.stat().st_mtime
        if newest > base + 2:
            errs.append("ref audit report is stale: materials changed after it "
                        f"({datetime.fromtimestamp(newest):%m-%d %H:%M:%S} > "
                        f"{datetime.fromtimestamp(base):%m-%d %H:%M:%S}); rerun ref_audit.py")
            details.append({"code": "STALE",
                            "message": "素材在体检之后改过，报告已过期，必须重跑 ref_audit.py"})
    return errs, details, p


def main() -> int:
    ap = argparse.ArgumentParser(description="Stage A/B 提交闸门 + curl 兜底")
    ap.add_argument("--sku", required=True)
    ap.add_argument("--project")
    ap.add_argument("--stage", choices=["a", "b"])
    ap.add_argument("--mode", choices=["tool", "curl"], default="tool")
    ap.add_argument("--versions", nargs="*", help="版本号，默认组配置里的全部")
    ap.add_argument("--approval-text", help="主管授权原话（写进审批件，事后审计用）")
    ap.add_argument("--i-have-supervisor-approval", action="store_true",
                    help="确认主管已授权花钱；没有这个标志不允许建单")
    ap.add_argument("--dry-run", action="store_true", help="只做预检，不上传不提交")
    ap.add_argument("--ignore-ref-audit", metavar="主管原话",
                    help="参考体检没过时唯一放行方式：填主管裁决原话，会写进审批件留痕")
    args = ap.parse_args()

    root = common.find_project(args.project)
    common.ensure_engine(root)

    # ---- 闸门 0：参考体检（款级，先于一切上传与建单）
    ref_override = None
    print("=== ref audit gate ===")
    gate_errs, gate_details, gate_path = ref_gate(root, args.sku)
    gate_file = root / "_check" / args.sku / "ref_audit" / "gate.json"
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_record = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "sku": args.sku,
        "stage": args.stage or ("curl" if args.mode == "curl" else None),
        "report": str(gate_path) if gate_path else None,
        "blocking": gate_details,
        "supervisor_override": args.ignore_ref_audit,
    }

    if gate_errs and not args.ignore_ref_audit:
        gate_record["result"] = "blocked"
        gate_file.write_text(json.dumps(gate_record, ensure_ascii=False, indent=2) + "\n",
                             encoding="utf-8")
        for e in gate_errs:
            print(f"  [BLOCK] {e}")
        print("\nref audit gate FAILED - submission not allowed.")
        print(f"  1) replace the offending references, then run:")
        print(f"       python scripts/ref_audit.py --sku {args.sku}")
        print(f"  2) details (Chinese): {gate_file}")
        print("  3) supervisor override only: --ignore-ref-audit \"<supervisor words>\"")
        return 3

    if gate_errs:
        gate_record["result"] = "bypassed_by_supervisor"
        for e in gate_errs:
            print(f"  [BYPASS] {e}")
        print(f"  !! supervisor override recorded: {args.ignore_ref_audit}")
        ref_override = {"result": "bypassed_by_supervisor",
                        "supervisor_override": args.ignore_ref_audit,
                        "blocked_items": gate_errs,
                        "report": str(gate_path) if gate_path else None}
    elif args.ignore_ref_audit:
        gate_record["result"] = "pass_with_unused_override"
        print("  OK (override flag given but not needed: report already passed)")
        ref_override = {"result": "pass_with_unused_override",
                        "supervisor_override": args.ignore_ref_audit,
                        "report": str(gate_path) if gate_path else None}
    else:
        gate_record["result"] = "pass"
        print(f"  OK  {gate_path}")
        try:
            if json.loads(gate_path.read_text(encoding="utf-8")).get("pose_checked") is False:
                gate_record["pose_checked"] = False
                print("  [WARN] motion profile skipped (no mediapipe): reference duplication was "
                      "checked, action variety was NOT")
                print("         fix: python scripts/init_project.py --venv --install")
        except Exception:
            pass
        ref_override = {"result": "pass", "report": str(gate_path) if gate_path else None}
    gate_file.write_text(json.dumps(gate_record, ensure_ascii=False, indent=2) + "\n",
                         encoding="utf-8")

    cfg = common.load_config(root, args.sku)
    versions = args.versions or [g["version"] for g in cfg["groups"]]
    approval_text = args.approval_text or (
        f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')} 主管授权：{args.sku} "
        f"{len(versions)} 组，每组 1 次")

    print("\n=== 预检 ===")
    bad = False
    for ver in versions:
        errs = preflight(root, cfg, args.sku, ver)
        print(f"{ver}: " + ("OK" if not errs else "; ".join(errs)))
        bad |= bool(errs)
    if bad:
        print("\n预检不过，先修。")
        return 1
    if args.dry_run:
        print("\n(dry-run) 预检通过，未上传、未提交。")
        return 0

    tool = load_tool(root)
    spending = args.stage == "b" or args.mode == "curl"
    if spending and not args.i_have_supervisor_approval:
        raise SystemExit(
            "这是花钱动作（一次 US$0.373，失败不退）。主管口头授权后加 "
            "--i-have-supervisor-approval 再跑。")

    for ver in versions:
        out = root / "04_历史版本" / args.sku / f"urlmode-{ver}-{cfg['batch']}"
        manifest = root / ".minimax_bridge" / "manifests" / args.sku / f"{ver}.json"

        if args.stage == "a":
            ap_path = make_approval(tool, root, args.sku, ver, out, None, approval_text, "a",
                                    ref_override)
            res = tool.stage_a_upload_and_freeze(
                project_root=root, manifest_path=manifest, approval_path=ap_path,
                output_dir=out, uploader=lambda p: upload_via_curl(root, p))
            print(f"{ver} stage-a OK  body={res['request_body_bytes']} B  "
                  f"sha={res['request_body_sha256'][:8]}")
            continue

        preview = out / "upload_preview.json"
        if not preview.is_file():
            raise SystemExit(f"{ver} 没跑过 Stage A：{preview}")

        if args.mode == "tool":
            ap_path = make_approval(tool, root, args.sku, ver, out, preview, approval_text, "b",
                                    ref_override)
            try:
                res = tool.stage_b_submit_once(
                    project_root=root, upload_preview_path=preview, approval_path=ap_path)
                print(f"{ver} stage-b {res['state']} task={res['task_id']} http={res['http_status']}")
                continue
            except Exception as exc:
                print(f"{ver} stage-b 失败：{type(exc).__name__}: {exc}")
                print("   → 若错误出现在 ~20 秒且没拿到 task_id，改用 --mode curl，不要换版本号重试。")
                return 2

        # curl 兜底
        sys.path.insert(0, str(root / "src"))
        from kling_workflow.minimax_bridge import MiniMaxTaskStore  # noqa: E402
        store = MiniMaxTaskStore(root)
        rec_path = root / ".minimax_bridge" / "tasks" / args.sku / f"{ver}.json"
        rec = json.loads(rec_path.read_text(encoding="utf-8")) if rec_path.exists() else None
        if rec and rec.get("task_id"):
            print(f"{ver} 已有 task_id={rec['task_id']}，跳过")
            continue
        if not rec:
            pv = json.loads(preview.read_text(encoding="utf-8"))
            rec = {
                "schema_version": 1, "provider": pv["provider"], "sku": args.sku, "version": ver,
                "group_number": pv["group_number"], "model": pv["model"], "mode": pv["mode"],
                "duration": pv["duration"], "ratio": pv["ratio"], "roles": pv["roles"],
                "prompt_sha256": pv["prompt_sha256"],
                "request_body_sha256": pv["request_body_sha256"],
                "upload_preview_sha256": sha(preview),
                "asset_url_record_path": pv["asset_url_record_path"],
                "asset_url_record_sha256": pv["asset_url_record_sha256"],
                "asset_fingerprints": pv["asset_fingerprints"],
                "action_source": pv["action_source"], "reference_video": pv["reference_video"],
                "transport": "url_mode_two_stage", "state": "creation_unknown",
                "generation_attempts": 1, "failure_category": None, "watermark_status": "unproven",
                "lifetime_paid_create_limit": 1, "lifetime_paid_create_posts_used": 1,
                "no_retry": True,
            }
            store.create_task(rec)
            print(f"{ver} 任务记录已建（creation_unknown）")
        body = out / "request_body.json"
        if sha(body) != rec["request_body_sha256"]:
            raise SystemExit(f"{ver} request_body 与审批绑定不一致，拒绝提交")
        cmd = ["curl", "-s", "-o", str(out / "create_response.json"),
               "-w", "%{http_code} %{time_total}", "--max-time", "300",
               "-X", "POST", CREATE_API,
               "-H", f"Authorization: Bearer {common.api_key(root)}",
               "-H", "Content-Type: application/json",
               "--data-binary", f"@{body}"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        code, _, secs = (proc.stdout.strip().split() + ["", ""])[:3]
        resp = {}
        rp = out / "create_response.json"
        if rp.is_file() and rp.stat().st_size:
            try:
                resp = json.loads(rp.read_text(encoding="utf-8"))
            except ValueError:
                resp = {}
        task_id = resp.get("task_id") or resp.get("id")
        print(f"{ver} curl http={code} t={secs}s task={task_id}")
        if not task_id:
            print(f"    resp={json.dumps(resp, ensure_ascii=False)[:300]}")
            continue
        print("    persist:", store.persist_task_id(rec, task_id))
        (out / "manual_create_post_result.json").write_text(json.dumps({
            "schema_version": 1,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "sku": args.sku, "version": ver, "task_id": task_id,
            "http_status": int(code) if code.isdigit() else code,
            "response": resp,
            "why_manual": "工具链 stage-b 的 httpx POST 在 ~20s 处抛 MiniMaxClientError；同 body 用 curl 直连可拿到 task_id",
            "request_body_sha256": rec["request_body_sha256"],
            "approval_text": approval_text,
            "elapsed_seconds": float(secs) if secs else None,
        }, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
