#!/usr/bin/env python3
"""Two-stage, fail-closed URL transport for approved MiniMax requests.

``stage-a`` uploads exact assets with ``compress_media=False``, freezes the
returned URLs and exact request bytes, then stops. ``stage-b`` accepts only a
supervisor-approved, byte-identical Stage A request and makes one create call.
There is intentionally no ambiguous ``--dry-run`` mode.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import mimetypes
from pathlib import Path
import sys
from typing import Callable
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path.home() / ".workbuddy/skills/aimaxhug-generation/scripts"))

from kling_workflow.minimax_bridge import (  # noqa: E402
    CURRENT_FOUR_IMAGE_ROLES,
    FIXED_MODEL,
    MiniMaxBridge,
    MiniMaxBridgeError,
    MiniMaxTaskStore,
    PreparedMiniMaxRequest,
    _BIND_FIELDS,
    _task_echoes,
)
from kling_workflow.minimax_client import MiniMaxClient  # noqa: E402
from kling_workflow.seedance_credentials import SeedanceCredentialStore  # noqa: E402

MAX_URL_REQUEST_BYTES = 100 * 1024
STAGE_A_DECISION = "approved_stage_a_upload_only"
STAGE_B_DECISION = "approved_stage_b_create_once"


class UrlSubmissionError(RuntimeError):
    """Safe failure whose message contains no credential or remote URL."""


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_new(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(raw)
    except FileExistsError:
        raise UrlSubmissionError(f"immutable output already exists: {path.name}") from None


def _write_json_new(path: Path, value: object) -> None:
    _write_new(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _load_json(path: Path, label: str) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        raise UrlSubmissionError(f"{label} is missing or invalid JSON") from None
    if not isinstance(value, dict):
        raise UrlSubmissionError(f"{label} must be a JSON object")
    return value


def _project_file(root: Path, value: str | Path, label: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        raise UrlSubmissionError(f"{label} must be an existing file inside the project") from None
    if not resolved.is_file():
        raise UrlSubmissionError(f"{label} must be an existing file inside the project")
    return resolved


def _relative(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        raise UrlSubmissionError("output must remain inside the project") from None


def _https_url(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise UrlSubmissionError("upload response did not contain a valid tmp_url")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UrlSubmissionError("tmp_url must be a credential-free HTTPS URL")
    return value


def upload(path: Path) -> str:
    """Upload one original file without compression and return its tmp_url."""
    from shared_client import AimaxhugClient

    mime, _ = mimetypes.guess_type(path.name)
    data = AimaxhugClient().post_file(
        "/api/v2/upload/file", str(path), mime or "application/octet-stream", compress_media=False
    )
    if not isinstance(data, dict):
        raise UrlSubmissionError("upload response was not an object")
    return _https_url(data.get("tmp_url"))


def _require_lifecycle(value: dict, *, used: int) -> None:
    if (value.get("lifetime_paid_create_limit") != 1
            or value.get("lifetime_paid_create_posts_used") != used
            or value.get("no_retry") is not True):
        raise UrlSubmissionError("lifetime gate must be limit=1, expected used value, no_retry=true")


def _load_current_manifest(root: Path, manifest_path: str | Path) -> tuple[Path, dict]:
    path = _project_file(root, manifest_path, "manifest")
    manifest = _load_json(path, "manifest")
    images = manifest.get("images")
    if not isinstance(images, dict):
        raise UrlSubmissionError("manifest images must use CURRENT_FOUR_IMAGE_ROLES")
    actual = set(images)
    required = set(CURRENT_FOUR_IMAGE_ROLES)
    if actual != required:
        historical = {"model_image", "reverse_flat", "action_storyboard", "reverse_worn"}
        if actual == historical:
            raise UrlSubmissionError(
                "historical four-image mode is preserved but not accepted; use CURRENT_FOUR_IMAGE_ROLES"
            )
        raise UrlSubmissionError("manifest must contain exactly CURRENT_FOUR_IMAGE_ROLES with no mixed roles")
    if (manifest.get("model") != FIXED_MODEL or manifest.get("duration") != 10
            or manifest.get("ratio") != "9:16"):
        raise UrlSubmissionError("manifest must use fixed model, duration=10, ratio=9:16")
    return path, manifest


def _validate_stage_a_approval(approval: dict, manifest: dict, manifest_hash: str) -> None:
    if (approval.get("decision") != STAGE_A_DECISION
            or approval.get("approved_by_supervisor") is not True
            or approval.get("stage_a_upload_authorized") is not True
            or approval.get("generation_submission_authorized") is not False):
        raise UrlSubmissionError("Stage A requires supervisor upload-only approval and forbids create POST")
    if (approval.get("sku") != manifest.get("sku") or approval.get("version") != manifest.get("version")
            or approval.get("manifest_sha256") != manifest_hash):
        raise UrlSubmissionError("Stage A approval does not match manifest bytes")
    if approval.get("roles") != list(CURRENT_FOUR_IMAGE_ROLES) or approval.get("compress_media") is not False:
        raise UrlSubmissionError("Stage A approval must bind current roles and compress_media=false")
    if approval.get("submit_url_tool_sha256") != _sha256(Path(__file__).resolve()):
        raise UrlSubmissionError("Stage A approval does not match the current URL tool bytes")
    _require_lifecycle(approval, used=0)


def _canonical_version_paths(root: Path, sku: str, version: str) -> tuple[Path, ...]:
    base = root / ".minimax_bridge"
    return (
        base / "previews" / sku / f"{version}.json",
        base / "previews" / sku / f"{version}.prompt.txt",
        base / "tasks" / sku / f"{version}.json",
        base / "receipts" / sku / f"{version}.json",
        base / "approvals" / sku / f"{version}.json",
        base / "candidates" / sku / f"{version}.json",
    )


def _reject_existing_canonical_version(root: Path, sku: str, version: str) -> None:
    if any(path.exists() or path.is_symlink() for path in _canonical_version_paths(root, sku, version)):
        raise UrlSubmissionError("canonical MiniMax version file already exists; never overwrite or re-upload")


def stage_a_upload_and_freeze(
    *, project_root: str | Path, manifest_path: str | Path, approval_path: str | Path,
    output_dir: str | Path, uploader: Callable[[Path], str] = upload,
) -> dict:
    """Upload exact assets, freeze URL request bytes, and stop before creation."""
    root = Path(project_root).resolve(strict=True)
    manifest_file, manifest = _load_current_manifest(root, manifest_path)
    approval_file = _project_file(root, approval_path, "Stage A approval")
    approval = _load_json(approval_file, "Stage A approval")
    manifest_hash = _sha256(manifest_file)
    _validate_stage_a_approval(approval, manifest, manifest_hash)

    output = Path(output_dir)
    if not output.is_absolute():
        output = root / output
    output = output.resolve()
    try:
        output.relative_to(root)
    except ValueError:
        raise UrlSubmissionError("Stage A output must remain inside the project") from None
    if output.exists():
        raise UrlSubmissionError("Stage A output already exists; immutable records cannot be overwritten")

    bridge = MiniMaxBridge(root)
    try:
        prepared = bridge.prepare(manifest_file)
    except MiniMaxBridgeError as exc:
        raise UrlSubmissionError(str(exc)) from None
    if prepared.roles != CURRENT_FOUR_IMAGE_ROLES:
        raise UrlSubmissionError("prepared roles are not CURRENT_FOUR_IMAGE_ROLES")

    try:
        with bridge.store.lock(prepared.sku, prepared.version):
            _reject_existing_canonical_version(root, prepared.sku, prepared.version)
            return _stage_a_upload_locked(
                root=root,
                manifest_file=manifest_file,
                manifest_hash=manifest_hash,
                approval_file=approval_file,
                output=output,
                prepared=prepared,
                store=bridge.store,
                uploader=uploader,
            )
    except MiniMaxBridgeError as exc:
        raise UrlSubmissionError(str(exc)) from None


def _stage_a_upload_locked(
    *, root: Path, manifest_file: Path, manifest_hash: str, approval_file: Path,
    output: Path, prepared: PreparedMiniMaxRequest, store: MiniMaxTaskStore,
    uploader: Callable[[Path], str],
) -> dict:
    """Perform Stage A while the canonical version lock is held."""
    image_records = []
    for index, asset in enumerate(prepared.assets, 1):
        remote_url = _https_url(uploader(asset.path))
        row = {
            "index": index, "role": asset.role, "source_path": _relative(root, asset.path),
            "source_sha256": asset.sha256, "source_size_bytes": asset.size_bytes,
            "tmp_url": remote_url, "compress_media": False,
        }
        _write_json_new(output / "asset_urls" / f"{index:02d}_{asset.role}.json", row)
        image_records.append(row)

    if not prepared.reference_video:
        raise UrlSubmissionError("one reference video is required")
    video_path = _project_file(root, prepared.reference_video["path"], "reference video")
    video_url = _https_url(uploader(video_path))
    video_record = {
        "index": 5, "role": "reference_video", "source_path": _relative(root, video_path),
        "source_sha256": prepared.reference_video["sha256"],
        "source_size_bytes": prepared.reference_video["size_bytes"],
        "tmp_url": video_url, "compress_media": False,
    }
    _write_json_new(output / "asset_urls" / "05_reference_video.json", video_record)

    asset_record = {
        "schema_version": 1, "created_at": _now(),
        "state": "stage_a_assets_uploaded_no_create_post", "sku": prepared.sku,
        "version": prepared.version, "roles": list(CURRENT_FOUR_IMAGE_ROLES),
        "compress_media": False, "images": image_records, "reference_video": video_record,
    }
    asset_record_path = output / "asset_url_record.json"
    _write_json_new(asset_record_path, asset_record)

    body = {
        "model": prepared.model, "prompt": prepared.prompt, "duration": prepared.duration,
        "ratio": prepared.ratio, "referenceImages": [row["tmp_url"] for row in image_records],
        "referenceVideos": [video_record["tmp_url"]],
    }
    request_bytes = _canonical_json(body)
    if len(request_bytes) >= MAX_URL_REQUEST_BYTES:
        raise UrlSubmissionError("URL request body must be smaller than 100KB")
    request_path = output / "request_body.json"
    _write_new(request_path, request_bytes)
    tool_path = Path(__file__).resolve()

    url_prepared = replace(
        prepared,
        body=body,
        body_sha256=_sha256_bytes(request_bytes),
        request_json_bytes=len(request_bytes),
    )
    canonical_paths = store.save_review(url_prepared)
    canonical_review = store.review_record(prepared.sku, prepared.version)
    if not isinstance(canonical_review, dict) or any(key not in canonical_review for key in _BIND_FIELDS):
        raise MiniMaxBridgeError("MiniMax canonical review binding is incomplete")
    canonical_review_path = _project_file(root, canonical_paths["review_record_path"], "canonical review")
    canonical_prompt_path = _project_file(root, canonical_paths["prompt_review_path"], "canonical prompt review")

    preview = {key: canonical_review[key] for key in _BIND_FIELDS}
    preview.update({
        "created_at": _now(),
        "state": "stage_a_complete_waiting_stage_b_approval", "sku": prepared.sku,
        "version": prepared.version, "group_number": prepared.group_number,
        "model": prepared.model, "duration": prepared.duration, "ratio": prepared.ratio,
        "roles": list(CURRENT_FOUR_IMAGE_ROLES),
        "manifest_path": _relative(root, manifest_file), "manifest_sha256": manifest_hash,
        "stage_a_approval_path": _relative(root, approval_file),
        "stage_a_approval_sha256": _sha256(approval_file),
        "asset_url_record_path": _relative(root, asset_record_path),
        "asset_url_record_sha256": _sha256(asset_record_path),
        "request_body_path": _relative(root, request_path),
        "request_body_sha256": _sha256_bytes(request_bytes),
        "request_body_bytes": len(request_bytes), "planned_input_mode": "URL",
        "canonical_review_path": _relative(root, canonical_review_path),
        "canonical_review_sha256": _sha256(canonical_review_path),
        "canonical_prompt_review_path": _relative(root, canonical_prompt_path),
        "canonical_prompt_review_sha256": _sha256(canonical_prompt_path),
        "submit_url_tool_path": str(tool_path),
        "submit_url_tool_sha256": _sha256(tool_path),
        "compress_media": False, "actual_remote_urls_recorded": True,
        "actual_create_posts": 0, "formal_submission_ready": False,
        "generation_submission_authorized": False, "lifetime_paid_create_limit": 1,
        "lifetime_paid_create_posts_used": 0, "no_retry": True,
    })
    _write_json_new(output / "upload_preview.json", preview)
    return preview


def _validate_stage_b_approval(approval: dict, preview: dict, preview_hash: str) -> None:
    if (approval.get("decision") != STAGE_B_DECISION
            or approval.get("approved_by_supervisor") is not True
            or approval.get("current_message_post_authorization") is not True
            or approval.get("requires_current_message_post_authorization") is not True
            or approval.get("generation_submission_authorized") is not True):
        raise UrlSubmissionError("Stage B requires supervisor approval for one create POST")
    if (approval.get("sku") != preview.get("sku") or approval.get("version") != preview.get("version")
            or approval.get("upload_preview_sha256") != preview_hash
            or approval.get("request_body_sha256") != preview.get("request_body_sha256")
            or approval.get("request_body_bytes") != preview.get("request_body_bytes")):
        raise UrlSubmissionError("Stage B approval does not byte-bind preview and request")
    current_tool_hash = _sha256(Path(__file__).resolve())
    if (approval.get("submit_url_tool_sha256") != current_tool_hash
            or preview.get("submit_url_tool_sha256") != current_tool_hash):
        raise UrlSubmissionError("Stage B approval or preview does not match the current URL tool bytes")
    _require_lifecycle(approval, used=0)


def _validate_frozen_request(preview: dict, raw: bytes) -> dict:
    if len(raw) >= MAX_URL_REQUEST_BYTES or len(raw) != preview.get("request_body_bytes"):
        raise UrlSubmissionError("request byte length differs from approved preview")
    if _sha256_bytes(raw) != preview.get("request_body_sha256"):
        raise UrlSubmissionError("request byte hash differs from approved preview")
    try:
        body = json.loads(raw)
    except (UnicodeError, ValueError):
        raise UrlSubmissionError("approved request bytes are invalid JSON") from None
    if not isinstance(body, dict) or _canonical_json(body) != raw:
        raise UrlSubmissionError("approved request bytes are not frozen canonical JSON")
    if (set(body) != {"model", "prompt", "duration", "ratio", "referenceImages", "referenceVideos"}
            or body.get("model") != FIXED_MODEL or body.get("duration") != 10
            or body.get("ratio") != "9:16"):
        raise UrlSubmissionError("approved request changed fixed model, duration, ratio, or fields")
    images, videos = body.get("referenceImages"), body.get("referenceVideos")
    if (not isinstance(images, list) or len(images) != len(CURRENT_FOUR_IMAGE_ROLES)
            or not isinstance(videos, list) or len(videos) != 1):
        raise UrlSubmissionError("approved request must contain four ordered images and one video")
    for value in images + videos:
        _https_url(value)
    if preview.get("roles") != list(CURRENT_FOUR_IMAGE_ROLES):
        raise UrlSubmissionError("preview role order is not CURRENT_FOUR_IMAGE_ROLES")
    return body


def stage_b_submit_once(
    *, project_root: str | Path, upload_preview_path: str | Path,
    approval_path: str | Path, client: MiniMaxClient | None = None,
) -> dict:
    """Submit one approved URL body and permanently consume its lifetime slot."""
    root = Path(project_root).resolve(strict=True)
    preview_file = _project_file(root, upload_preview_path, "upload preview")
    approval_file = _project_file(root, approval_path, "Stage B approval")
    preview = _load_json(preview_file, "upload preview")
    approval = _load_json(approval_file, "Stage B approval")
    _validate_stage_b_approval(approval, preview, _sha256(preview_file))
    if preview.get("formal_submission_ready") is not False or preview.get("actual_create_posts") != 0:
        raise UrlSubmissionError("upload preview is not in the Stage A stopped state")
    _require_lifecycle(preview, used=0)
    request_path = _project_file(root, preview.get("request_body_path", ""), "request body")
    body = _validate_frozen_request(preview, request_path.read_bytes())
    output = preview_file.parent
    consumption_path, result_path = output / "paid_create_consumption.json", output / "post_result.json"
    if consumption_path.exists():
        raise UrlSubmissionError("paid create lifetime already consumed; used=1 and no retry")
    if result_path.exists():
        raise UrlSubmissionError("a Stage B result already exists; no retry")
    sku, version = preview.get("sku"), preview.get("version")
    if not isinstance(sku, str) or not isinstance(version, str):
        raise UrlSubmissionError("upload preview identity is invalid")
    expected_review_path = root / ".minimax_bridge" / "previews" / sku / f"{version}.json"
    expected_prompt_path = root / ".minimax_bridge" / "previews" / sku / f"{version}.prompt.txt"
    review_path = _project_file(root, preview.get("canonical_review_path", ""), "canonical review")
    prompt_path = _project_file(root, preview.get("canonical_prompt_review_path", ""), "canonical prompt review")
    if review_path != expected_review_path or prompt_path != expected_prompt_path:
        raise UrlSubmissionError("upload preview does not reference the canonical MiniMax review files")
    if (_sha256(review_path) != preview.get("canonical_review_sha256")
            or _sha256(prompt_path) != preview.get("canonical_prompt_review_sha256")):
        raise UrlSubmissionError("canonical MiniMax review bytes differ from the approved Stage A preview")
    store = MiniMaxTaskStore(root)
    try:
        canonical_review = store.review_record(sku, version)
    except MiniMaxBridgeError as exc:
        raise UrlSubmissionError(str(exc)) from None
    if (not isinstance(canonical_review, dict)
            or any(preview.get(key) != canonical_review.get(key) for key in _BIND_FIELDS)):
        raise UrlSubmissionError("Stage A preview and canonical MiniMax review binding conflict")
    prompt = body.get("prompt")
    if not isinstance(prompt, str) or _sha256_bytes(prompt.encode("utf-8")) != canonical_review["prompt_sha256"]:
        raise UrlSubmissionError("request prompt differs from the canonical prompt review")
    if client is None:
        client = MiniMaxClient(api_key_provider=SeedanceCredentialStore().read)

    try:
        with store.lock(sku, version):
            if store.task_record(sku, version) is not None:
                raise UrlSubmissionError("task record already exists; no second create POST")
            intent = {key: canonical_review[key] for key in _BIND_FIELDS}
            intent.update({
                "state": "creation_unknown", "generation_attempts": 1,
                "failure_category": None, "watermark_status": "unproven",
                "transport": "url_mode_two_stage",
                "upload_preview_sha256": _sha256(preview_file),
                "asset_url_record_path": preview.get("asset_url_record_path"),
                "asset_url_record_sha256": preview.get("asset_url_record_sha256"),
                "lifetime_paid_create_limit": 1, "lifetime_paid_create_posts_used": 1,
                "no_retry": True,
            })
            store.create_task(intent)
            _write_json_new(consumption_path, {
                "schema_version": 1, "created_at": _now(), "state": "create_post_committed_no_retry",
                "sku": sku, "version": version, "request_body_sha256": preview["request_body_sha256"],
                "lifetime_paid_create_limit": 1, "lifetime_paid_create_posts_used": 1,
                "no_retry": True,
                "note": "Written before client call; any return or exception consumes the only slot.",
            })
            try:
                status, payload = client.post_video(body)
            except Exception as exc:
                _write_json_new(result_path, {
                    "schema_version": 1, "created_at": _now(), "state": "create_post_exception_no_retry",
                    "sku": sku, "version": version, "error_type": type(exc).__name__,
                    "lifetime_paid_create_posts_used": 1, "no_retry": True,
                })
                raise UrlSubmissionError("create POST failed after lifetime used=1; no retry") from None

            ids = _task_echoes(payload)
            task_id = next(iter(ids)) if len(ids) == 1 and "<invalid>" not in ids else None
            state = "task_known" if task_id else "creation_unknown"
            if task_id:
                known = dict(intent)
                known.update(
                    state="task_known", task_id=task_id,
                    failure_category=None,
                    http_status=status if isinstance(status, int) else None,
                )
                persistence = store.persist_task_id(known, task_id)
                if persistence != "both":
                    raise UrlSubmissionError(
                        "task id canonical persistence incomplete; preserve records for manual recovery"
                    )
            result = {
                "schema_version": 1, "created_at": _now(), "state": state, "sku": sku,
                "version": version, "task_id": task_id,
                "http_status": status if isinstance(status, int) else None,
                "request_body_sha256": preview["request_body_sha256"],
                "lifetime_paid_create_posts_used": 1, "lifetime_paid_create_limit": 1, "no_retry": True,
            }
            _write_json_new(result_path, result)
            return result
    except MiniMaxBridgeError as exc:
        raise UrlSubmissionError(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Two-stage MiniMax URL submission gate")
    sub = parser.add_subparsers(dest="command", required=True)
    stage_a = sub.add_parser("stage-a", help="upload exact assets and freeze request; never create")
    stage_a.add_argument("--manifest", required=True)
    stage_a.add_argument("--approval-file", required=True)
    stage_a.add_argument("--output-dir", required=True)
    stage_b = sub.add_parser("stage-b", help="send one byte-approved URL request")
    stage_b.add_argument("--upload-preview", required=True)
    stage_b.add_argument("--approval-file", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "stage-a":
            result = stage_a_upload_and_freeze(
                project_root=ROOT, manifest_path=args.manifest,
                approval_path=args.approval_file, output_dir=args.output_dir,
            )
        else:
            result = stage_b_submit_once(
                project_root=ROOT, upload_preview_path=args.upload_preview,
                approval_path=args.approval_file,
            )
    except UrlSubmissionError as exc:
        print(json.dumps({"state": "blocked", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
