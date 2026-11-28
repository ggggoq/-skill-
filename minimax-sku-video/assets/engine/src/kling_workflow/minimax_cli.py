"""Offline-first CLI for reviewing and carefully submitting MiniMax H3 batches."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Mapping

from .minimax_bridge import MiniMaxBridge, MiniMaxBridgeError, PreparedMiniMaxRequest, _regular_file
from .minimax_client import API_BASE, VIDEO_ROUTE, MiniMaxClientError
from .processing_log import ProcessingLog, ProcessingLogError
from .seedance_credentials import SeedanceCredentialStore


def _project_root() -> Path:
    value = os.environ.get("KLING_WORKFLOW_PROJECT_ROOT")
    return Path(value).resolve() if value else Path.cwd().resolve()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _approval(bridge: MiniMaxBridge, value: str) -> dict[str, object]:
    try:
        raw_path = Path(value)
        if raw_path.is_absolute():
            path = bridge._project_file(raw_path.absolute().relative_to(bridge.root).as_posix(), "主管审核文件")
        else:
            path = bridge._project_file(value, "主管审核文件")
    except Exception:
        raise MiniMaxBridgeError("主管审核文件必须是项目目录内的普通文件") from None
    try:
        data = json.loads(_regular_file(path).decode("utf-8"), object_pairs_hook=_unique_object)
    except Exception:
        raise MiniMaxBridgeError("主管审核文件无效") from None
    if not isinstance(data, dict):
        raise MiniMaxBridgeError("主管审核文件必须是JSON对象")
    return data


def _preview(bridge: MiniMaxBridge, prepared: PreparedMiniMaxRequest) -> dict[str, object]:
    review = bridge.review(prepared)
    fingerprints = [
        {"role": asset.role, "sha256": asset.sha256, "size_bytes": asset.size_bytes,
         "width": asset.width, "height": asset.height, "mime_type": asset.mime_type}
        for asset in prepared.assets
    ]
    approval_template = {
        "reviewer": "", "decision": "awaiting_supervisor",
        "sku": prepared.sku, "version": prepared.version,
        "group_number": prepared.group_number, "model": prepared.model,
        "prompt_sha256": prepared.prompt_sha256,
        "request_body_sha256": prepared.body_sha256, "roles": list(prepared.roles),
        "asset_fingerprints": fingerprints, "action_source": dict(prepared.action_source),
        "reference_video": prepared.reference_video,
        "duration": prepared.duration, "ratio": prepared.ratio,
    }
    return {
        "state": "awaiting_supervisor", "submitted": False,
        "sku": prepared.sku, "version": prepared.version,
        "group_number": prepared.group_number, "model": prepared.model,
        "video_create_endpoint": API_BASE + VIDEO_ROUTE,
        "video_query_endpoint_template": API_BASE + VIDEO_ROUTE + "/{model}/{task_id}",
        "duration": prepared.duration, "ratio": prepared.ratio,
        "prompt_text": prepared.prompt, "prompt_characters": len(prepared.prompt),
        "prompt_sha256": prepared.prompt_sha256, "roles": list(prepared.roles),
        "asset_fingerprints": fingerprints,
        "action_source": dict(prepared.action_source),
        "reference_video": prepared.reference_video,
        "request_json_bytes": prepared.request_json_bytes,
        "request_body_sha256": prepared.body_sha256,
        "review_record_path": review["review_record_path"],
        "prompt_review_path": review["prompt_review_path"],
        "approval_fields": [
            "reviewer", "decision=approved", "sku", "version", "group_number", "model",
            "prompt_sha256", "request_body_sha256", "roles", "asset_fingerprints", "action_source",
            "reference_video", "duration", "ratio",
        ],
        "approval_template": approval_template,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="minimax-workflow")
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="离线预检并保存主管审核材料")
    preview.add_argument("--manifest", required=True)
    batch_preview = sub.add_parser("batch-preview", help="批量离线预检；全部完成后才输出")
    batch_preview.add_argument("--manifest", action="append", required=True)
    submit = sub.add_parser("submit", help="按已审核清单单次付费提交")
    submit.add_argument("--manifest", required=True)
    submit.add_argument("--approval-file", required=True)
    submit.add_argument("--confirm-paid-request", action="store_true", required=True)
    batch_submit = sub.add_parser("batch-submit", help="先核对完整审核批次，再按序单次提交")
    batch_submit.add_argument("--manifest", action="append", required=True)
    batch_submit.add_argument("--approval-file", action="append", required=True)
    batch_submit.add_argument("--confirm-paid-request", action="store_true", required=True)
    resume = sub.add_parser("resume", help="按已登记模型和任务号精确续查")
    resume.add_argument("--sku", required=True)
    resume.add_argument("--version", required=True)
    resume.add_argument("--group-number", required=True, type=int)
    return parser


def _same_batch(prepared: list[PreparedMiniMaxRequest]) -> None:
    identities = [(item.sku, item.version) for item in prepared]
    if len(set(identities)) != len(identities):
        raise MiniMaxBridgeError("批次内款号版本重复")
    previous: dict[str, int] = {}
    for item in prepared:
        group = previous.get(item.sku)
        if group is not None and item.group_number <= group:
            raise MiniMaxBridgeError("同款批次必须按参考组编号升序提交")
        previous[item.sku] = item.group_number


def _credential_status(store: SeedanceCredentialStore) -> None:
    try:
        status = store.status()
    except Exception:
        raise MiniMaxBridgeError("无法读取Aimaxhug凭据状态") from None
    if not isinstance(status, Mapping) or status.get("api-key") != "PRESENT":
        raise MiniMaxBridgeError("Aimaxhug凭据未配置")


def _submit_items(root: Path, items: list[tuple[PreparedMiniMaxRequest, dict[str, object]]],
                  audit_log: ProcessingLog) -> dict[str, object]:
    _same_batch([prepared for prepared, _approval_doc in items])
    bridge = MiniMaxBridge(root, credential_store=SeedanceCredentialStore(), audit_log=audit_log)
    for prepared, approval_doc in items:
        bridge._assert_current(prepared)
        review = bridge.store.review_record(prepared.sku, prepared.version)
        if not isinstance(review, Mapping) or review.get("status") != "awaiting_supervisor":
            raise MiniMaxBridgeError("批次中有版本缺少主管审核预检")
        bridge.store.save_approval(prepared, approval_doc)
    # All manifests and approval records are checked before credentials or POST.
    _credential_status(bridge.credentials)
    from .minimax_client import MiniMaxClient
    bridge.client = MiniMaxClient(api_key_provider=bridge.credentials.read)
    submitted: list[dict[str, object]] = []
    try:
        for prepared, approval_doc in items:
            result = bridge.submit(prepared, supervisor_approval=approval_doc, paid_confirmation=True)
            submitted.append({"sku": prepared.sku, "version": prepared.version, **result})
            if result.get("state") != "task_known":
                break
    finally:
        bridge.client.close()
    return {"submitted": submitted, "stopped_after_non_task_known": bool(submitted and submitted[-1].get("state") != "task_known")}


def _run(args: argparse.Namespace, root: Path, audit_log: ProcessingLog | None) -> dict[str, object]:
    if args.command in {"preview", "batch-preview"}:
        bridge = MiniMaxBridge(root)
        manifests = [args.manifest] if args.command == "preview" else args.manifest
        prepared = [bridge.prepare(manifest) for manifest in manifests]
        _same_batch(prepared)
        previews = [_preview(bridge, item) for item in prepared]
        return previews[0] if args.command == "preview" else {"items": previews, "submitted": []}

    if args.command in {"submit", "batch-submit"}:
        manifests = [args.manifest] if args.command == "submit" else args.manifest
        approval_files = [args.approval_file] if args.command == "submit" else args.approval_file
        if len(manifests) != len(approval_files):
            raise MiniMaxBridgeError("每个manifest必须对应一份主管审核文件")
        bridge = MiniMaxBridge(root, credential_store=SeedanceCredentialStore(), audit_log=audit_log)
        items = []
        for manifest, approval_file in zip(manifests, approval_files, strict=True):
            prepared = bridge.prepare(manifest)
            approval_doc = _approval(bridge, approval_file)
            items.append((prepared, approval_doc))
        if audit_log is None:
            raise MiniMaxBridgeError("付费命令缺少处理日志")
        result = _submit_items(root, items, audit_log)
        if args.command == "submit":
            return result["submitted"][0]
        return result

    if args.command == "resume":
        credentials = SeedanceCredentialStore()
        _credential_status(credentials)
        from .minimax_client import MiniMaxClient
        client = MiniMaxClient(api_key_provider=credentials.read)
        bridge = MiniMaxBridge(root, client=client, credential_store=credentials)
        try:
            return bridge.resume(args.sku, args.version, group_number=args.group_number)
        finally:
            client.close()
    raise MiniMaxBridgeError("未知MiniMax命令")


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = _project_root()
    paid = args.command in {"submit", "batch-submit"}
    sku = None
    version = None
    if args.command == "resume":
        sku, version = args.sku, args.version
    elif args.command == "submit":
        try:
            early = MiniMaxBridge(root).prepare(args.manifest)
            sku, version = early.sku, early.version
        except Exception:
            pass
    elif args.command == "batch-submit" and args.manifest:
        try:
            early = MiniMaxBridge(root).prepare(args.manifest[0])
            sku, version = early.sku, early.version
        except Exception:
            pass
    log = None
    try:
        log = ProcessingLog.start(
            root, command=f"minimax-{args.command}", sku=sku, version=version,
            network_mode="paid" if paid else ("read_only" if args.command == "resume" else "offline"),
            paid_authorization_present=paid and args.confirm_paid_request is True,
        )
        result = _run(args, root, log)
        failed = result.get("state") in {
            "creation_unknown", "task_id_persistence_conflict", "task_known_audit_state_pending",
            "transport_error", "http_error", "identity_mismatch", "technical_validation_failed",
            "candidate_download_or_validation_failed", "candidate_collision",
        } or result.get("stopped_after_non_task_known") is True
        if paid and isinstance(result.get("submitted"), list):
            failed = bool(result["submitted"] and result["submitted"][-1].get("state") != "task_known")
        log.finish(status="failure" if failed else "success", exit_code=1 if failed else 0)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 1 if failed else 0
    except MiniMaxBridgeError as error:
        if log is not None:
            try:
                log.finish(status="refused", exit_code=2)
            except Exception:
                pass
        print(str(error), file=sys.stderr)
        return 2
    except (MiniMaxClientError, ProcessingLogError, OSError, ValueError):
        if log is not None:
            try:
                log.finish(status="failure", exit_code=1)
            except Exception:
                pass
        print("MiniMax本地命令执行失败；未输出请求正文、凭据或下载地址。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
