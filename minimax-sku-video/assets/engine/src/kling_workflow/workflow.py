from __future__ import annotations

import hashlib
import hmac
import inspect
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterator, Mapping, Sequence

from .assets import (
    PreparedAssets,
    element_fingerprint,
    prepare_api_images,
    scan_project_materials,
    scan_sku_images,
    detect_media_kind,
)
from .client import (
    DownloadTargetExists,
    KlingError,
    NetworkUnknownOutcome,
    RemoteElement,
    RemoteTask,
    RemoteVideo,
)
from .models import ProjectPaths, RunRecord, ShotPlan, TaskStatus
from .mcp_retry import InMemoryAssetCache, McpRetryCoordinator, RetryScope
from .local_audit import write_local_audit
from .prompting import (
    PromptError,
    _shot_total_duration,
    build_video_payload,
    prompt_sha256,
    refresh_fixed_tail,
    render_prompt_markdown,
    validate_api_duration_contract,
)
from .media_audio import measure_audio, validate_audio
from .scene_image_analysis import (
    SceneImageAnalysisError,
    analyze_scene_images,
    scene_image_manifest_sha256,
)
from .task_store import TaskStore, _record_path, _validate_sku_segment
from .video_analysis import (
    ANALYSIS_VERSION,
    DECODE_CONFIG,
    SEMANTIC_FIELDS,
    AnalysisManifest,
    VideoAnalysisError,
    extract_analysis_manifest,
)
from .video_cache import VideoAnalysisCache


_METADATA_NAME = "workflow.json"
_INPUT_NAME = "动作分镜分析-输入.json"
_ANALYSIS_NAME = "动作分镜分析.md"
_PROMPT_NAME = "可灵提示词.md"
_FINAL_PROMPT_NAME = "可灵MCP提交提示词.txt"
_VIDEO_REPORT_NAME = "视频校验.json"
_AI_REVIEW_NAME = "ai_review.json"
_AUDIO_GATE_NAME = "audio_gate.json"
_REVIEW_SCHEMA_VERSION = 1
_CACHE_NAME = "element-cache.json"
_RUN_NAME = re.compile(r"^(?:\d{8}-\d{6}|v\d{3,})$")
_CANDIDATE_NONCE = re.compile(r"^[0-9a-f]{32}$")
_CANDIDATE_TOKEN_HEX_LENGTH = 16
_TASK4_TEMP_RANDOM_LENGTH = 8
_WINDOWS_MAX_PATH = 259
_MAX_CANDIDATE_COLLISIONS = 8
_JWT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?![A-Za-z0-9_-])"
)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_URL_PATTERN = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BASE64_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{80,}={0,2}(?![A-Za-z0-9+/])"
)
_KEY_PATTERN = re.compile(
    r"(?i)\b(?:access[-_ ]?key|secret[-_ ]?key|api[-_ ]?key)\s*[:=]\s*[^\s,;]+"
)
_AK_SK_VALUE_PATTERN = re.compile(r"(?i)\b(?:ak|sk)[_-][A-Za-z0-9_./+=-]{8,}")
_SENSITIVE_FIELD_FRAGMENTS = (
    "accesskey",
    "secretkey",
    "apikey",
    "authorization",
    "credential",
    "password",
    "bearer",
    "token",
    "jwt",
    "signature",
    "signedurl",
    "downloadurl",
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_GLOBAL_LOCK_GUARD = threading.Lock()
_THREAD_LOCKS: dict[str, threading.RLock] = {}


class WorkflowError(RuntimeError):
    """Safe, actionable errors raised by the local workflow coordinator."""


class ApprovalError(WorkflowError):
    """A generation request did not pass the current confirmation gate."""


class _SystemClock:
    @staticmethod
    def monotonic() -> float:
        return time.monotonic()

    @staticmethod
    def sleep(seconds: float) -> None:
        time.sleep(seconds)

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def now_iso(cls) -> str:
        return cls.now().isoformat()


class _UuidFactory:
    @staticmethod
    def new(kind: str, sku: str) -> str:
        return f"{kind}-{sku}-{uuid.uuid4()}"


class Workflow:
    """Coordinate offline preparation, explicit approval, and resumable Kling tasks."""

    def __init__(
        self,
        paths: ProjectPaths,
        store: TaskStore | None = None,
        credential_store: object | None = None,
        client: object | None = None,
        *,
        clock: object | None = None,
        ids: object | None = None,
        validator: Callable[[Path], object] | object | None = None,
        video_analyzer: Callable[..., AnalysisManifest] | None = None,
        processing_log: object | None = None,
        poll_interval_seconds: float = 10,
        poll_timeout_seconds: float = 1800,
    ) -> None:
        if poll_interval_seconds <= 0 or poll_timeout_seconds <= 0:
            raise ValueError("轮询间隔和截止时间必须大于 0")
        self.paths = paths
        self.store = store or TaskStore(paths)
        self.credential_store = credential_store
        self.client = client
        self.clock = clock or _SystemClock()
        self.ids = ids or _UuidFactory()
        self.validator = validator
        self._sensitive_values: set[str] = set()
        self.processing_log = processing_log
        self.video_analyzer = video_analyzer or extract_analysis_manifest
        self._uses_default_video_analyzer = video_analyzer is None
        self.video_cache = VideoAnalysisCache(
            paths.root / "04_历史版本" / ".video-analysis-cache"
        )
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.poll_timeout_seconds = float(poll_timeout_seconds)

    def prepare(
        self,
        sku: str,
        *,
        from_version: str | None = None,
        reference_group: str | Path | None = None,
    ) -> Path:
        """Create an offline run, prepare local derivatives, and record video inputs."""
        _validate_sku_segment(sku)
        self._check_project_paths()
        project_materials = None
        reference_group_record: dict[str, object] | None = None
        reference_group_sha256: str | None = None
        sku_source = self.paths.materials / sku
        if sku_source.is_dir():
            self._require_safe_source_directory(sku_source, "款号素材")
            self._check_source_children(sku_source)
            try:
                if reference_group is not None:
                    manifest_path = Path(reference_group)
                    reference_group_record, reference_group_sha256 = self._select_reference_group(
                        manifest_path, sku, None
                    )
                    project_materials = scan_project_materials(
                        self.paths,
                        sku,
                        reference_files=reference_group_record["reference_files"],
                    )
                    reference_group_record, reference_group_sha256 = self._select_reference_group(
                        manifest_path, sku, project_materials
                    )
                else:
                    project_materials = scan_project_materials(self.paths, sku)
            except (OSError, ValueError) as exc:
                raise WorkflowError(_safe_text(str(exc)) or "款号素材检查失败") from None
            assets = self._preparation_images(project_materials)
            video_source = sku_source
            action_video_paths = list(project_materials.action_videos)
            scene_video_paths = list(project_materials.scene_videos)
            video_paths = [*action_video_paths, *scene_video_paths]
            if reference_group is not None:
                reference_files = reference_group_record["reference_files"]
                selected_images = {
                    name
                    for role in ("主参考图", "服装细节参考", "场景参考", "动作参考图")
                    for name in reference_files.get(role, [])
                }
                selected_videos = set(reference_files["动作参考视频"]) | set(
                    reference_files["场景参考"]
                ) & {path.name for path in scene_video_paths}
                assets = tuple(image for image in assets if image.path.name in selected_images)
                video_paths = [path for path in video_paths if path.name in selected_videos]
        else:
            if reference_group is not None:
                raise WorkflowError("参考组清单只适用于 01_素材库 款号目录")
            sku_source = self.paths.root / "02款号参考图" / sku
            video_source = sku_source
            self._require_safe_source_directory(sku_source, "款号参考图")
            self._check_source_children(sku_source)
            assets = scan_sku_images(self.paths, sku)
            video_paths = self._reference_videos(video_source)
            if not video_paths:
                raise WorkflowError("当前款号目录缺少明确绑定的动作参考视频；拒绝读取共享效果视频目录")
            action_video_paths = list(video_paths)
            scene_video_paths = []
        modification_source = None
        if from_version is not None:
            if project_materials is None:
                raise WorkflowError("只有 01_素材库 款号目录支持按旧版本修改")
            modification_source = self._load_modification_source(sku, from_version)
        scene_image_assets = tuple(
            image for image in assets if image.role == "场景参考"
        ) if project_materials is not None else ()
        reference_group_id = (
            str(reference_group_record["group_id"])
            if reference_group_record is not None
            else "flat-layout" if project_materials is not None else "legacy-flat-layout"
        )
        try:
            scene_image_manifest = analyze_scene_images(
                scene_image_assets,
                sku=sku,
                reference_group_id=reference_group_id,
                allowed_root=video_source,
                cache_root=self.paths.history / ".scene-image-cache",
                required=False,
            ) if scene_image_assets else None
        except SceneImageAnalysisError as exc:
            raise WorkflowError(_safe_text(str(exc)) or "场景参考图分析失败") from None
        element_assets, excluded_element_assets = self._select_preparation_assets(
            assets, project_materials, video_paths
        )
        video_snapshot = (
            self._snapshot_reference_videos(video_source, video_paths)
            if video_paths
            else []
        )

        try:
            run = self.store.create_run(sku, now=self._now())
        except Exception:
            # Filesystem exceptions can include absolute paths and platform-specific
            # details. The user only needs the safe outcome; retain no raw exception.
            raise WorkflowError("无法创建新的任务目录") from None
        run_dir = Path(run.run_dir)
        try:
            self._assert_safe_run_dir(run_dir, sku)
            prepared = self._prepare_images(element_assets, run_dir)
            if video_paths:
                if self._uses_default_video_analyzer:
                    manifest = self.video_analyzer(
                        video_paths, run_dir, cache=self.video_cache
                    )
                else:
                    manifest = self.video_analyzer(video_paths, run_dir)
                if not isinstance(manifest, AnalysisManifest):
                    raise WorkflowError("参考视频分析器返回的数据结构无效")
                manifest_value = self._video_manifest_with_roles(
                    manifest, action_video_paths, scene_video_paths
                )
                self._verify_video_manifest(manifest_value, video_snapshot, video_source)
            else:
                manifest = AnalysisManifest([])
                manifest_value = manifest.to_dict()
            audio_policy = self._reference_audio_policy({"video_manifest": manifest_value})
            payload = self._prepared_payload(prepared, manifest)
            payload["video_manifest"] = manifest_value
            payload["audio_policy"] = audio_policy
            if scene_image_manifest is not None:
                payload["scene_image_manifest"] = scene_image_manifest
            if project_materials is not None:
                payload["project_request_text"] = project_materials.request_text
                payload["review_standard"] = project_materials.review_standard
            self._write_exclusive(run_dir / _INPUT_NAME, _json_bytes(payload))
            metadata = {
                    "schema_version": 1,
                    "sku": sku,
                    "stage": "analysis",
                    "element_fingerprint": element_fingerprint(prepared),
                    "prepared": self._prepared_record(prepared),
                    "video_manifest": manifest_value,
                    "source_layout": "gemini" if project_materials is not None else "legacy_sku",
                    "shots": None,
                    "analysis": None,
                    "audio_policy": audio_policy,
                }
            if project_materials is not None:
                metadata["source_layout"] = "gemini"
                metadata["project_request_text"] = project_materials.request_text
                metadata["review_standard"] = project_materials.review_standard
                metadata["material_roles"] = {
                    "主参考图": [image.path.name for image in assets if image.role == "主参考图"],
                    "服装细节参考": [image.path.name for image in assets if image.role == "服装细节参考"],
                    "场景参考": [image.path.name for image in assets if image.role == "场景参考"],
                    "动作参考图": [
                        image.path.name for image in project_materials.action_reference_images
                    ],
                    "动作参考视频": [path.name for path in video_paths],
                }
                if excluded_element_assets:
                    metadata["element_excluded_scene_references"] = [
                        image.path.name for image in excluded_element_assets
                    ]
            if scene_image_manifest is not None:
                scene_manifest_bytes = _json_bytes(scene_image_manifest)
                self._write_exclusive(run_dir / "scene_image_manifest.json", scene_manifest_bytes)
                metadata["scene_image_manifest_version"] = scene_image_manifest["analyzer_version"]
                metadata["scene_image_manifest_sha256"] = scene_image_manifest_sha256(scene_image_manifest)
                metadata["scene_image_manifest_path"] = "scene_image_manifest.json"
                metadata["scene_image_reference_group_id"] = reference_group_id
            if reference_group_record is not None and reference_group_sha256 is not None:
                metadata["reference_group_id"] = reference_group_record["group_id"]
                metadata["reference_group_sha256"] = reference_group_sha256
                metadata["reference_group_manifest"] = str(Path(reference_group).resolve())
                source_folder = reference_group_record.get("source_folder")
                if isinstance(source_folder, str):
                    reference_folder_name = Path(source_folder).name
                    try:
                        _validate_sku_segment(reference_folder_name)
                    except ValueError:
                        raise WorkflowError("参考文件夹名称不适合安全归档") from None
                    metadata["reference_folder_name"] = reference_folder_name
            if modification_source is not None:
                metadata.update(modification_source)
            self._finalize_preparation_metadata(metadata, run_dir)
            self._write_metadata(run_dir, metadata, create=True)
        except WorkflowError:
            raise
        except (OSError, ValueError, VideoAnalysisError, PromptError) as exc:
            raise WorkflowError(_safe_text(str(exc)) or "离线素材准备失败") from None
        except Exception as exc:
            raise WorkflowError(f"离线素材准备失败（{type(exc).__name__}）") from None
        return run_dir

    def _preparation_images(self, materials):
        return materials.images

    def _select_preparation_assets(self, assets, project_materials, video_paths):
        element_assets, excluded = assets, ()
        if project_materials is not None and len(element_assets) > 4:
            scene_assets = tuple(image for image in element_assets if image.role == "场景参考")
            candidate = tuple(image for image in element_assets if image.role != "场景参考")
            if scene_assets and 2 <= len(candidate) <= 4:
                element_assets, excluded = candidate, scene_assets
            else:
                raise WorkflowError(
                    "当前参考组相关图片超过可灵元素上限，且无法安全排除场景参考图；已停止付费流程"
                )
        if project_materials is not None and len(element_assets) < 2:
            raise WorkflowError(
                "可灵元素至少需要一张额外参考图；当前只有主参考图，已在离线分析阶段阻止"
            )
        return element_assets, excluded

    def _prepare_images(self, assets, run_dir):
        return prepare_api_images(assets, run_dir)

    def _finalize_preparation_metadata(self, metadata, run_dir):
        """Provider hook before the offline preparation record is published."""

    def _render_prompt_document(self, metadata, *args, **kwargs):
        return render_prompt_markdown(*args, **kwargs)

    def approve(self, sku: str, version: str) -> dict[str, object]:
        """Copy one completed version to approved storage only after this explicit call."""
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        number = int(version[1:])
        if number < 1 or version != f"v{number:03d}":
            raise WorkflowError("版本号必须是 v001 或更高版本")
        run_dir = self._version_run_dir(sku, version)
        flat_paths = self._uses_flat_version_paths(run_dir)
        try:
            run_dir = self._assert_safe_run_dir(run_dir, sku)
            run = self._load_run(run_dir)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("找不到有效的历史版本记录") from None
        if run.status is not TaskStatus.COMPLETED:
            raise WorkflowError("只有已完成并通过本地校验的待审核版本才能审核通过")

        source = (
            self.paths.pending / sku / f"{version}.mp4"
            if flat_paths
            else self.paths.pending / sku / version / f"{sku}_{version}.mp4"
        )
        target = (
            self.paths.approved / sku / f"{sku}_{version}.mp4"
            if flat_paths
            else self.paths.approved / sku / version / source.name
        )
        approval_record = run_dir / "approval.json"
        for path, boundary, label in (
            (source, self.paths.pending, "待审核视频"),
            (target, self.paths.approved, "审核通过目录"),
            (approval_record, self.paths.history, "审核记录"),
        ):
            try:
                _reject_reparse_components(path)
                path.resolve(strict=False).relative_to(boundary.resolve(strict=False))
            except (OSError, ValueError):
                raise WorkflowError(f"{label}路径超出固定目录") from None
        if not source.is_file() or source.stat().st_size == 0:
            raise WorkflowError("找不到非空待审核视频")
        if target.exists() or approval_record.exists():
            raise WorkflowError("该版本已审核或归档文件已存在，不覆盖")

        try:
            metadata = self._read_metadata(run_dir)
        except WorkflowError:
            raise WorkflowError("生成校验 SHA-256 记录缺失或无效") from None
        report = metadata.get("video_report")
        transaction = metadata.get("download_transaction")
        if (
            not isinstance(report, Mapping)
            or report.get("validated") is not True
            or report.get("watermark_verification") != "verified_no_watermark"
            or not isinstance(transaction, Mapping)
            or transaction.get("status") != "published"
            or transaction.get("watermark_enabled") is not False
        ):
            raise WorkflowError("生成校验 SHA-256 记录缺失或无效")
        expected_sha = report.get("sha256")
        if (
            not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
            or transaction.get("sha256") != expected_sha
            or report.get("size_bytes") != transaction.get("size_bytes")
            or transaction.get("planned_final") != source.name
            or transaction.get("final") != source.name
            or not _same_file_bytes(run_dir / _VIDEO_REPORT_NAME, _json_bytes(dict(report)))
        ):
            raise WorkflowError("生成校验 SHA-256 记录缺失或无效")
        source_digest = _sha256_file_stable(source)
        if source_digest != expected_sha:
            raise WorkflowError("待审核视频 SHA-256 与生成校验记录不匹配")
        # Hard release gates: measured audio must be audible and the candidate
        # must carry a visual review bound to this exact file and prompt.
        self._require_release_gates(run_dir, run, source, expected_sha, report)
        self._assert_owned_download_file(source, run_dir, transaction)
        payload = source.read_bytes()
        if not payload or hashlib.sha256(payload).hexdigest() != source_digest:
            raise WorkflowError("待审核视频读取期间发生变化，已停止审核")
        target.parent.mkdir(parents=True, exist_ok=True)
        created_copy = False
        try:
            with target.open("xb") as destination:
                created_copy = True
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            approved_digest = _sha256_file_stable(target)
            if source_digest != approved_digest:
                raise WorkflowError("审核副本 SHA-256 与待审核视频不一致")
            result: dict[str, object] = {
                "sku": sku,
                "version": version,
                "status": "审核通过",
                "approval_decision": "审核通过",
                "approved_at": self._now_iso(),
                "source_file": str(source),
                "approved_file": str(target),
                "source_sha256": source_digest,
                "approved_sha256": approved_digest,
            }
            self._write_exclusive(approval_record, _json_bytes(result))
        except Exception:
            if created_copy:
                try:
                    target.unlink()
                except OSError:
                    pass
            raise
        return result

    def reject(self, sku: str, version: str, reason: str) -> dict[str, object]:
        """Copy a human-rejected video and its reason into the review archive."""
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        number = int(version[1:])
        if number < 1 or version != f"v{number:03d}":
            raise WorkflowError("版本号必须是 v001 或更高版本")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise WorkflowError("审核不通过原因必须是 1 到 512 字的简短说明")
        _assert_no_sensitive_content(reason)
        logger = getattr(self.processing_log, "record_rejection_archive", None)
        if not callable(logger):
            raise WorkflowError("归档前必须启用 06_处理日志安全记录")

        run_dir = self._version_run_dir(sku, version)
        run_dir = self._assert_safe_run_dir(run_dir, sku)
        run = self._load_run(run_dir)
        if run.status is not TaskStatus.COMPLETED:
            raise WorkflowError("只有已完成并通过本地校验的待审核视频才能归档为审核不通过")
        metadata = self._read_metadata(run_dir)
        report = metadata.get("video_report")
        transaction = metadata.get("download_transaction")
        source = self._video_output_path(sku, version, run_dir)
        if (
            not isinstance(report, Mapping)
            or report.get("validated") is not True
            or report.get("watermark_verification") != "verified_no_watermark"
            or not isinstance(transaction, Mapping)
            or transaction.get("status") != "published"
            or transaction.get("watermark_enabled") is not False
            or transaction.get("planned_final") != source.name
            or transaction.get("final") != source.name
        ):
            raise WorkflowError("生成校验或无水印证明缺失，不能归档此视频")
        expected_sha = report.get("sha256")
        if (
            not isinstance(expected_sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_sha)
            or transaction.get("sha256") != expected_sha
            or report.get("size_bytes") != transaction.get("size_bytes")
            or not _same_file_bytes(run_dir / _VIDEO_REPORT_NAME, _json_bytes(dict(report)))
        ):
            raise WorkflowError("生成校验 SHA-256 记录缺失或无效")
        _reject_reparse_components(source)
        try:
            source.resolve(strict=True).relative_to(self.paths.pending.resolve(strict=True))
        except (OSError, ValueError):
            raise WorkflowError("待审核原视频路径超出固定目录") from None
        self._assert_regular_file(source)
        source_digest = _sha256_file_stable(source)
        if source_digest != expected_sha:
            raise WorkflowError("待审核视频 SHA-256 与生成校验记录不匹配")
        self._assert_owned_download_file(source, run_dir, transaction)
        payload = source.read_bytes()
        if not payload or hashlib.sha256(payload).hexdigest() != source_digest:
            raise WorkflowError("待审核视频读取期间发生变化，已停止归档")

        reference_folder = self._reference_folder_name(metadata, sku)
        archive_dir = self.paths.pending / "00-gpt审核未通过" / reference_folder
        self._ensure_output_directory(archive_dir)
        reason_bytes = (reason.strip() + "\n").encode("utf-8")
        archived_video: Path | None = None
        archived_reason: Path | None = None
        for index in range(1, 10001):
            stem = source.stem if index == 1 else f"{source.stem} ({index})"
            video_target = archive_dir / f"{stem}{source.suffix}"
            reason_target = archive_dir / f"{stem}_失败原因.txt"
            if video_target.exists() or reason_target.exists():
                continue
            try:
                self._write_exclusive_group(
                    ((video_target, payload), (reason_target, reason_bytes))
                )
            except WorkflowError:
                if video_target.exists() or reason_target.exists():
                    continue
                raise
            archived_video, archived_reason = video_target, reason_target
            break
        if archived_video is None or archived_reason is None:
            raise WorkflowError("归档文件名冲突过多，已保留所有既有文件")
        try:
            logger(
                sku=sku,
                reference_folder=reference_folder,
                source_path=str(source.resolve(strict=True)),
                archive_path=str(archived_video.resolve(strict=True)),
                rejection_reason=reason.strip(),
                version=version,
            )
        except Exception:
            raise WorkflowError("视频已复制归档，但 06_处理日志写入失败；请核对归档文件") from None
        return {
            "sku": sku,
            "version": version,
            "status": "审核不通过已归档",
            "reference_folder": reference_folder,
            "source_file": str(source),
            "archive_file": str(archived_video),
            "reason_file": str(archived_reason),
            "reason": reason.strip(),
        }

    def _reference_folder_name(
        self, metadata: Mapping[str, object], sku: str
    ) -> str:
        value = metadata.get("reference_folder_name")
        manifest_value = metadata.get("reference_group_manifest")
        if manifest_value is not None:
            if not isinstance(manifest_value, str):
                raise WorkflowError("参考组归档名称记录无效")
            manifest_path = Path(manifest_value)
            history_sku = self.paths.history / sku
            _reject_reparse_components(manifest_path)
            try:
                manifest_path.resolve(strict=True).relative_to(history_sku.resolve(strict=True))
                self._assert_regular_file(manifest_path)
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError, WorkflowError):
                raise WorkflowError("无法安全读取参考文件夹归档名称") from None
            if (
                not isinstance(document, Mapping)
                or document.get("sku") != sku
                or document.get("group_id") != metadata.get("reference_group_id")
                or not isinstance(document.get("source_folder"), str)
            ):
                raise WorkflowError("参考组与归档文件夹名称绑定不匹配")
            source_folder = document["source_folder"]
            manifest_name = Path(source_folder).name
            if PureWindowsPath(source_folder).name != manifest_name:
                raise WorkflowError("参考文件夹路径格式冲突，无法安全归档")
            if value is not None and value != manifest_name:
                raise WorkflowError("参考组任务记录与源文件夹名称不一致")
            value = manifest_name
        if value is None:
            value = sku
        try:
            _validate_sku_segment(value)
        except (TypeError, ValueError):
            raise WorkflowError("参考文件夹名称不适合安全归档") from None
        return value

    def save_prompt(
        self,
        run_dir: str | Path,
        analysis: object,
        shots: Sequence[ShotPlan],
    ) -> RunRecord:
        run_path = self._assert_safe_run_dir(Path(run_dir))
        with _locked(run_path / ".workflow.lock"):
            run = self._load_run(run_path)
            metadata = self._read_metadata(run_path)
            if not isinstance(analysis, Mapping) or not analysis:
                raise WorkflowError("动作与镜头分析必须是非空结构化字段映射")
            try:
                analysis_bytes = _json_bytes(dict(analysis))
                parsed_analysis = json.loads(analysis_bytes.decode("utf-8"))
                if metadata.get("source_layout") == "gemini":
                    _validate_semantic_review(parsed_analysis, metadata)
                if not isinstance(shots, Sequence) or isinstance(shots, (str, bytes)):
                    raise WorkflowError("分镜必须是结构化镜头列表")
                if not shots or any(not isinstance(shot, ShotPlan) for shot in shots):
                    raise WorkflowError("分镜必须是结构化镜头列表")
                shots_payload = [
                    {"index": shot.index, "duration": shot.duration, "prompt": shot.prompt}
                    for shot in shots
                ]
                source_dimensions = self._source_dimensions(metadata)
                reference_duration = self._reference_duration(metadata)
                prompt_markdown = self._render_prompt_document(
                    metadata,
                    shots,
                    duration=_shot_total_duration(shots),
                    sku=run.sku,
                    analysis=parsed_analysis,
                    source_dimensions=source_dimensions,
                    reference_duration_seconds=reference_duration,
                    modification_feedback=(
                        metadata.get("modification_feedback")
                        if isinstance(metadata.get("modification_feedback"), str)
                        else None
                    ),
                    audio_policy=str(metadata.get("audio_policy", "generate_music")),
                    scene_image_manifest=self._load_scene_image_manifest(run_path, metadata),
                    reference_mapping=self._reference_mapping(metadata),
                )
            except (PromptError, TypeError, ValueError, OverflowError) as exc:
                raise WorkflowError(_safe_text(str(exc)) or "分镜或结构化分析无效") from None
            analysis_markdown = _render_analysis_markdown(parsed_analysis)
            analysis_payload = analysis_markdown.encode("utf-8")
            prompt_payload = prompt_markdown.encode("utf-8")
            digest = prompt_sha256(prompt_markdown)
            _assert_no_sensitive_content(
                {
                    "analysis": parsed_analysis,
                    "shots": shots_payload,
                    "analysis_markdown": analysis_markdown,
                    "prompt_markdown": prompt_markdown,
                }
            )

            if metadata.get("sku") != run.sku:
                raise WorkflowError("任务分析记录不完整或款号不匹配")
            if metadata.get("stage") == "waiting_approval":
                if not self._same_prompt_transaction(
                    run_path,
                    run,
                    metadata,
                    parsed_analysis,
                    shots_payload,
                    analysis_payload,
                    prompt_payload,
                    digest,
                ):
                    raise WorkflowError("已有分析内容冲突，不会覆盖；请保留当前任务并人工处理")
                if run.status is TaskStatus.ANALYZING:
                    return self._update_run(
                        run_path,
                        status=TaskStatus.WAITING_APPROVAL,
                        prompt_sha256=digest,
                    )
                if run.status is TaskStatus.WAITING_APPROVAL and run.prompt_sha256 == digest:
                    return run
                raise WorkflowError("提示词事务与任务记录不一致，不会覆盖")

            if run.status is not TaskStatus.ANALYZING or metadata.get("stage") != "analysis":
                raise WorkflowError("只有分析中的任务可以保存提示词；已有任务内容不会被覆盖")
            manifest = metadata.get("video_manifest")
            if not isinstance(manifest, Mapping) or manifest.get("status") != "ready":
                raise WorkflowError("参考视频未全部成功离线分析，不能保存待确认提示词")
            if metadata.get("analysis") is not None or metadata.get("shots") is not None:
                raise WorkflowError("现有分析记录不符合初始状态，不会覆盖")

            next_metadata = dict(metadata)
            draft_event = {
                "model": "Luna",
                "role": "initial_draft",
                "reason": "记录当前款号已保存的初稿",
                "result": "draft_saved",
                "evidence": "当前提示词 SHA-256",
                "recorded_at": self._now_iso(),
                "sku": run.sku,
                "version": run_path.name,
                "prompt_sha256": digest,
            }
            prior_events = next_metadata.get("role_events", [])
            if not isinstance(prior_events, list):
                raise WorkflowError("职责日志状态损坏，拒绝保存提示词")
            next_metadata.update(
                {
                    "stage": "waiting_approval",
                    "analysis": parsed_analysis,
                    "shots": shots_payload,
                    "prompt_sha256": digest,
                    "role_events": [*prior_events, draft_event],
                }
            )
            # Validate the complete transaction before publishing any file.
            _json_bytes(next_metadata)
            self._assert_exclusive_compatible(run_path / _ANALYSIS_NAME, analysis_payload)
            self._assert_exclusive_compatible(run_path / _PROMPT_NAME, prompt_payload)
            self._assert_exclusive_compatible(run_path / _FINAL_PROMPT_NAME, prompt_payload)
            self._write_exclusive(run_path / _ANALYSIS_NAME, analysis_payload)
            self._write_exclusive(run_path / _PROMPT_NAME, prompt_payload)
            self._write_exclusive(run_path / _FINAL_PROMPT_NAME, prompt_payload)
            self._write_metadata(run_path, next_metadata)
            self._promote_semantic_cache(parsed_analysis, metadata, run.sku)
            if self.processing_log is not None:
                recorder = getattr(self.processing_log, "record_step", None)
                if callable(recorder):
                    recorder(
                        model="Luna", role="initial_draft",
                        reason=draft_event["reason"], result=draft_event["result"],
                        evidence=draft_event["evidence"], sku=run.sku,
                        version=run_path.name if re.fullmatch(r"v\d{3,6}", run_path.name) else None,
                    )
            return self._update_run(
                run_path,
                status=TaskStatus.WAITING_APPROVAL,
                prompt_sha256=digest,
            )

    def _promote_semantic_cache(
        self,
        analysis: Mapping[str, object],
        metadata: Mapping[str, object],
        sku: str,
    ) -> None:
        source_manifest = metadata.get("video_manifest")
        candidate_manifest = analysis.get("video_manifest")
        source_rows = (
            source_manifest.get("videos")
            if isinstance(source_manifest, Mapping)
            else None
        )
        candidate_rows = (
            candidate_manifest.get("videos")
            if isinstance(candidate_manifest, Mapping)
            else None
        )
        if (
            not isinstance(source_rows, list)
            or not isinstance(candidate_rows, list)
            or len(source_rows) != len(candidate_rows)
        ):
            return

        source_layout = metadata.get("source_layout")
        if source_layout == "gemini":
            source_root = self.paths.materials / sku
        elif source_layout == "legacy_sku":
            source_root = self.paths.root / "02款号参考图" / sku
        else:
            return
        try:
            self._require_safe_source_directory(source_root, "款号视频素材")
            resolved_root = source_root.resolve(strict=True)
        except (OSError, WorkflowError):
            return

        for source_row, row in zip(source_rows, candidate_rows, strict=True):
            if (
                not isinstance(source_row, Mapping)
                or not isinstance(row, Mapping)
                or source_row.get("status") != "success"
                or row.get("status") != "success"
            ):
                continue
            source_path_value = source_row.get("path")
            source_sha = source_row.get("sha256")
            candidate_path_value = row.get("path")
            review = row.get("semantic_review")
            if (
                not isinstance(source_path_value, str)
                or candidate_path_value != source_path_value
                or not isinstance(source_sha, str)
                or not re.fullmatch(r"[0-9a-f]{64}", source_sha)
                or row.get("sha256") != source_sha
                or not isinstance(review, Mapping)
            ):
                continue

            source_path = Path(source_path_value)
            if not source_path.is_absolute():
                if not source_path.parts or any(
                    part in {"", ".", ".."} for part in source_path.parts
                ):
                    continue
                source_path = source_root / source_path
            if (
                source_row.get("name", source_path.name) != source_path.name
                or row.get("name", source_path.name) != source_path.name
                or source_row.get("absolute_path", str(source_path))
                != str(source_path.resolve(strict=False))
                or row.get("absolute_path", str(source_path))
                != source_row.get("absolute_path", str(source_path))
            ):
                continue
            try:
                _reject_reparse_components(source_path)
                resolved_path = source_path.resolve(strict=True)
                resolved_path.relative_to(resolved_root)
                self._assert_regular_file(resolved_path)
            except (OSError, ValueError, WorkflowError):
                continue
            self.video_cache.promote_semantic_review(
                resolved_path, ANALYSIS_VERSION, DECODE_CONFIG, review
            )

    def _same_prompt_transaction(
        self,
        run_dir: Path,
        run: RunRecord,
        metadata: Mapping[str, object],
        analysis: object,
        shots: object,
        analysis_markdown: bytes,
        prompt_markdown: bytes,
        digest: str,
    ) -> bool:
        expected_keys = {
            "schema_version",
            "sku",
            "stage",
            "element_fingerprint",
            "prepared",
            "video_manifest",
            "analysis",
            "shots",
            "prompt_sha256",
        }
        optional_keys = {
            "source_layout",
            "project_request_text",
            "review_standard",
            "material_roles",
            "from_version",
            "modification_feedback",
            "source_video_sha256",
            "source_feedback_sha256",
            "role_events",
            "sol_review",
            "reference_duration_override",
            "audio_policy",
        }
        if not expected_keys.issubset(metadata) or not (set(metadata) - expected_keys) <= optional_keys:
            return False
        modification_keys = {
            "from_version",
            "modification_feedback",
            "source_video_sha256",
            "source_feedback_sha256",
        }
        if bool(modification_keys & set(metadata)) and not modification_keys <= set(metadata):
            return False
        if metadata.get("prompt_sha256") != digest:
            return False
        if not hmac.compare_digest(_json_bytes(metadata.get("analysis")), _json_bytes(analysis)):
            return False
        if not hmac.compare_digest(_json_bytes(metadata.get("shots")), _json_bytes(shots)):
            return False
        if not _same_file_bytes(run_dir / _ANALYSIS_NAME, analysis_markdown):
            return False
        if not _same_file_bytes(run_dir / _PROMPT_NAME, prompt_markdown):
            return False
        if not _same_file_bytes(run_dir / _FINAL_PROMPT_NAME, prompt_markdown):
            return False
        return run.prompt_sha256 in {None, digest}

    def generate(
        self,
        sku: str,
        prompt_sha: str,
        paid_confirmation: bool,
        *,
        version: str | None = None,
    ) -> RunRecord:
        if paid_confirmation is not True:
            raise ApprovalError("缺少用户当前明确的开始生成确认")
        run_dir = self._find_waiting_run(sku, version=version)
        with _locked(run_dir / ".workflow.lock"):
            current = self._find_waiting_run(sku, version=version)
            if current != run_dir:
                raise ApprovalError("该款号的待确认任务已发生变化，请重新显示并确认")
            run = self._load_run(run_dir)
            if run.status is not TaskStatus.WAITING_APPROVAL:
                raise ApprovalError("该任务不是等待确认状态")
            metadata = self._read_metadata(run_dir)
            if metadata.get("external_provider") in {"mcp", "api"}:
                raise ApprovalError("外部付费任务已登记；请完成本地结果导入，禁止再次创建任务")
            if (
                not isinstance(prompt_sha, str)
                or not isinstance(run.prompt_sha256, str)
                or not hmac.compare_digest(run.prompt_sha256, prompt_sha)
            ):
                raise ApprovalError("提示词版本不一致，请重新显示并确认")
            self._verify_saved_prompt(run, metadata)
            self._require_persisted_sol_review(run, metadata)
            try:
                validate_api_duration_contract(self._saved_shots(metadata))
            except PromptError as exc:
                raise WorkflowError(_safe_text(str(exc)) or "当前 API 无法精确表达分镜时长") from None
            prepared = self._prepare_current_assets(run, metadata)
            self._verify_reference_video_sources(metadata, run_dir)
            self._read_credentials_if_configured()

            run = self._update_run(run_dir, status=TaskStatus.APPROVED)
            metadata.update({"stage": "approved", "approved_at": self._now_iso()})
            self._write_metadata(run_dir, metadata)

            try:
                if run.video_external_id:
                    remote_video = self._query_video_external(run, run.video_external_id)
                    run = self._persist_video_task(run, remote_video)
                    return self._poll_download_and_validate(run, remote_video)
                if run.video_task_id:
                    remote_video = self._query_video_task(run, run.video_task_id)
                    return self._poll_download_and_validate(run, remote_video)

                element = self._confirmed_cached_element(run, metadata)
                if element is None:
                    if run.element_external_id:
                        element = self._query_element_external(run, run.element_external_id)
                        run = self._persist_element_task(run, element)
                        element = self._poll_element(run, element)
                    else:
                        external_id = self._new_external_id("element", sku)
                        run = self._update_run(
                            run_dir,
                            status=TaskStatus.ELEMENT_SUBMITTING,
                            element_external_id=external_id,
                        )
                        metadata.update({"stage": "element_submitting"})
                        self._write_metadata(run_dir, metadata)
                        remote = self._create_or_recover_element(run, prepared, external_id)
                        run = self._persist_element_task(run, remote)
                        element = self._poll_element(run, remote)
                else:
                    run = self._persist_cached_element_task(run, element)

                if element.status != "succeed" or not self._valid_element_id(element.element_id):
                    raise WorkflowError("可灵元素尚未完成，任务保留待续查；不会重复提交")
                self._cache_element(run, metadata, element)
                metadata.update(
                    {
                        "stage": "element_ready",
                        "element_id": element.element_id,
                        "element_task_id": self._safe_remote_identifier(element.task_id),
                    }
                )
                self._write_metadata(run_dir, metadata)
                return self._submit_video(run, metadata, prepared, element)
            except WorkflowError:
                raise
            except Exception as exc:
                raise WorkflowError(f"工作流处理中断（{type(exc).__name__}）；任务可继续查询") from None

    def record_role_step(
        self,
        *,
        sku: str,
        version: str,
        model: str,
        role: str,
        reason: str,
        result: str,
        evidence: str = "",
    ) -> dict[str, object]:
        """Persist bounded role evidence against the current SKU/version/prompt."""
        if model not in {"Luna", "Sol", "Web", "local"} or role not in {
            "initial_draft", "review", "source_lookup", "query", "download"
        }:
            raise WorkflowError("处理职责无效")
        expected_roles = {
            "Luna": "initial_draft", "Sol": "review", "Web": "source_lookup"
        }
        if model in expected_roles and role != expected_roles[model]:
            raise WorkflowError("模型与处理职责不匹配")
        if model == "local" and role not in {"query", "download"}:
            raise WorkflowError("本地程序职责只允许 query 或 download")
        for value in (reason, result, evidence):
            if not isinstance(value, str) or len(value) > 240 or _find_sensitive(value):
                raise WorkflowError("职责日志包含无效或敏感内容")
        if not reason.strip() or not result.strip():
            raise WorkflowError("职责日志必须包含原因和结果")
        if role == "review" and result not in {"approved", "changes_required"}:
            raise WorkflowError("Sol 审核结果必须是 approved 或 changes_required")
        run_dir = self._find_waiting_run(sku, version=version)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if run.sku != sku or run_dir.name != version:
                raise WorkflowError("款号或版本与当前待确认任务不匹配")
            if run.status is not TaskStatus.WAITING_APPROVAL:
                raise WorkflowError("只有已保存提示词的当前版本可以记录职责结果")
            prompt_digest = run.prompt_sha256
            if not isinstance(prompt_digest, str) or metadata.get("prompt_sha256") != prompt_digest:
                raise WorkflowError("当前提示词 SHA 缺失，不能记录职责结果")
            events = metadata.get("role_events", [])
            if not isinstance(events, list):
                raise WorkflowError("职责日志状态损坏，拒绝更新")
            if role == "review" and any(
                isinstance(event, Mapping)
                and event.get("model") == "Sol"
                and event.get("role") == "review"
                and event.get("prompt_sha256") == prompt_digest
                for event in events
            ):
                raise WorkflowError("当前提示词版本的 Sol 最终审核只允许一次")
            event = {
                "model": model,
                "role": role,
                "reason": reason.strip(),
                "result": result.strip(),
                "evidence": evidence.strip(),
                "recorded_at": self._now_iso(),
                "sku": sku,
                "version": version,
                "prompt_sha256": prompt_digest,
            }
            updated = dict(metadata)
            updated["role_events"] = [*events, event]
            if role == "review":
                updated["sol_review"] = {
                    "status": result,
                    "sku": sku,
                    "version": version,
                    "prompt_sha256": prompt_digest,
                    "recorded_at": event["recorded_at"],
                }
            self._write_metadata(run_dir, updated)
            if self.processing_log is not None:
                recorder = getattr(self.processing_log, "record_step", None)
                if callable(recorder):
                    recorder(
                        model=model, role=role, reason=reason.strip(), result=result.strip(),
                        evidence=evidence.strip(), sku=sku, version=version,
                    )
            return event

    def refresh_prompt_version(self, *, sku: str, version: str) -> dict[str, object]:
        """Re-render one saved prompt through the current prompt contract.

        This is the sanctioned regeneration path for an existing run: the
        authored action text is preserved verbatim, the fixed protection tail is
        rebuilt, the audio policy is re-derived from measured reference audio,
        and the Sol review is invalidated because the prompt digest changes.
        """
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if run.sku != sku or run_dir.name != version:
                raise WorkflowError("款号或版本与当前任务不匹配")
            if (
                run.status is not TaskStatus.WAITING_APPROVAL
                or metadata.get("stage") != "waiting_approval"
            ):
                raise WorkflowError("只有等待确认的版本可以按当前约束重渲染提示词")
            shots = self._saved_shots(metadata)
            try:
                refreshed = [
                    ShotPlan(
                        index=shot.index,
                        duration=shot.duration,
                        prompt=refresh_fixed_tail(shot.prompt),
                    )
                    for shot in shots
                ]
            except PromptError as exc:
                raise WorkflowError(_safe_text(str(exc)) or "分镜提示词无法按当前约束重建") from None
            audio_policy = self._reference_audio_policy(metadata)
            next_metadata = dict(metadata)
            next_metadata["shots"] = [
                {"index": shot.index, "duration": shot.duration, "prompt": shot.prompt}
                for shot in refreshed
            ]
            next_metadata["audio_policy"] = audio_policy
            previous_digest = run.prompt_sha256
            prompt_markdown = self._render_saved_prompt(run, next_metadata)
            digest = prompt_sha256(prompt_markdown)
            if (
                hmac.compare_digest(digest, str(previous_digest or ""))
                and metadata.get("prompt_sha256") == digest
            ):
                return {
                    "ok": True,
                    "sku": sku,
                    "version": version,
                    "changed": False,
                    "audio_policy": audio_policy,
                    "prompt_sha256": digest,
                }
            next_metadata["prompt_sha256"] = digest
            next_metadata["sol_review"] = {
                "status": "pending_review",
                "sku": sku,
                "version": version,
                "prompt_sha256": digest,
                "recorded_at": self._now_iso(),
            }
            _json_bytes(next_metadata)
            payload = prompt_markdown.encode("utf-8")
            self._atomic_replace(run_dir / _PROMPT_NAME, payload)
            self._atomic_replace(run_dir / _FINAL_PROMPT_NAME, payload)
            self._write_metadata(run_dir, next_metadata)
            self._update_run(run_dir, prompt_sha256=digest)
            return {
                "ok": True,
                "sku": sku,
                "version": version,
                "changed": True,
                "audio_policy": audio_policy,
                "previous_prompt_sha256": previous_digest,
                "prompt_sha256": digest,
                "sol_review": "pending_review",
            }

    def record_ai_review(
        self, *, sku: str, version: str, review: Mapping[str, object]
    ) -> dict[str, object]:
        """Persist the mandatory post-generation visual review for one candidate.

        The pipeline owns the binding: the candidate SHA-256 and the prompt
        SHA-256 are computed here and cannot be supplied by the reviewer.
        """
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        if not isinstance(review, Mapping):
            raise WorkflowError("复审记录必须是结构化对象")
        decision = review.get("decision")
        if decision not in {"pass", "fail"}:
            raise WorkflowError("复审判定必须是 pass 或 fail")
        reviewer = self._review_text(review.get("reviewer"), "复审人", limit=60)
        summary = self._review_text(review.get("summary"), "复审结论摘要", limit=600)
        root_cause = self._review_text(review.get("root_cause", ""), "根本原因", limit=600, optional=True)
        scores = self._review_scores(review.get("scores"))
        findings = self._review_findings(review.get("findings"))
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            if run.sku != sku or run_dir.name != version:
                raise WorkflowError("款号或版本与当前任务不匹配")
            source = self._video_output_path(sku, version, run_dir)
            if not source.is_file() or source.stat().st_size == 0:
                raise WorkflowError("找不到非空待审核视频，无法记录复审")
            digest = _sha256_file_stable(source)
            archived = self._archive_existing_review(run_dir)
            record = {
                "schema_version": _REVIEW_SCHEMA_VERSION,
                "review_kind": "post_generation_visual",
                "sku": sku,
                "version": version,
                "decision": decision,
                "scores": scores,
                "findings": findings,
                "summary": summary,
                "root_cause": root_cause,
                "reviewer": reviewer,
                "file_sha256": digest,
                "prompt_sha256": run.prompt_sha256,
                "reviewed_at": self._now_iso(),
                "stage_written": False,
            }
            _assert_no_sensitive_content(record)
            self._atomic_replace(run_dir / _AI_REVIEW_NAME, _json_bytes(record))
            record["previous_record"] = archived
            return dict(record)

    def check_audio_gate(
        self, *, sku: str, version: str, path: str | Path | None = None, ffmpeg: str | None = None
    ) -> dict[str, object]:
        """Measure the candidate audio and persist the hard silence gate."""
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            if run.sku != sku or run_dir.name != version:
                raise WorkflowError("款号或版本与当前任务不匹配")
            source = Path(path) if path is not None else self._video_output_path(sku, version, run_dir)
            if not source.is_file() or source.stat().st_size == 0:
                raise WorkflowError("找不到非空待审核视频，无法执行音频闸门")
            digest = _sha256_file_stable(source)
            report = validate_audio(source, ffmpeg=ffmpeg)
            record = {
                "schema_version": _REVIEW_SCHEMA_VERSION,
                "gate": "audio_silence",
                "sku": sku,
                "version": version,
                "video_file": source.name,
                "file_sha256": digest,
                "checked_at": self._now_iso(),
                "decision": "pass" if report.get("audio_ok") is True else "fail",
                "errors": list(report.get("audio_errors") or []),
            }
            record.update({key: report[key] for key in report if key.startswith("audio_")})
            _assert_no_sensitive_content(record)
            self._atomic_replace(run_dir / _AUDIO_GATE_NAME, _json_bytes(record))
            if record["decision"] != "pass":
                detail = "；".join(str(item) for item in record["errors"]) or "音频闸门未通过"
                raise WorkflowError(f"音频闸门未通过：{detail}")
            return record

    @staticmethod
    def _review_text(
        value: object, label: str, *, limit: int, optional: bool = False
    ) -> str:
        if value is None and optional:
            return ""
        if not isinstance(value, str) or len(value) > limit or _find_sensitive(value):
            raise WorkflowError(f"{label}必须是 {limit} 字以内且不含敏感信息的文本")
        text = value.strip()
        if not text and not optional:
            raise WorkflowError(f"{label}不能为空")
        return text

    @staticmethod
    def _review_scores(value: object) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise WorkflowError("复审评分必须是结构化对象")
        scores: dict[str, str] = {}
        for key, item in value.items():
            if key not in {"A", "B", "C", "D", "E"} or item not in {"pass", "warn", "fail"}:
                raise WorkflowError("复审评分只允许 A–E 维度与 pass/warn/fail 取值")
            scores[key] = item
        return scores

    @staticmethod
    def _review_findings(value: object) -> list[dict[str, object]]:
        if value is None:
            return []
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise WorkflowError("复审发现必须是列表")
        if len(value) > 60:
            raise WorkflowError("复审发现不得超过 60 条")
        findings: list[dict[str, object]] = []
        for row in value:
            if not isinstance(row, Mapping):
                raise WorkflowError("复审发现行必须是结构化对象")
            severity = row.get("severity")
            if severity not in {"blocker", "minor"}:
                raise WorkflowError("复审发现严重级别只允许 blocker 或 minor")
            code = row.get("code")
            note = row.get("note")
            if (
                not isinstance(code, str)
                or not code.strip()
                or len(code) > 40
                or not isinstance(note, str)
                or not note.strip()
                or len(note) > 240
                or _find_sensitive(code)
                or _find_sensitive(note)
            ):
                raise WorkflowError("复审发现必须包含不超过 40 字的编号和不超过 240 字的说明")
            findings.append(
                {
                    "code": code.strip(),
                    "severity": severity,
                    "note": note.strip(),
                    "timecode": str(row.get("timecode") or "")[:16] or None,
                }
            )
        return findings

    def _archive_existing_review(self, run_dir: Path) -> str | None:
        path = run_dir / _AI_REVIEW_NAME
        if not path.is_file():
            return None
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkflowError("现有复审记录损坏，拒绝覆盖") from None
        stamp = str(previous.get("reviewed_at") or self._now_iso()) if isinstance(previous, Mapping) else self._now_iso()
        safe = re.sub(r"[^0-9A-Za-z]", "", stamp)[:20] or "previous"
        archive_dir = run_dir / "ai_review_history"
        archive_dir.mkdir(exist_ok=True)
        target = archive_dir / f"{safe}.json"
        counter = 1
        while target.exists():
            target = archive_dir / f"{safe}-{counter}.json"
            counter += 1
        self._write_exclusive(target, path.read_bytes())
        return target.name

    def _read_gate_record(self, path: Path, label: str) -> dict[str, object]:
        _reject_reparse_components(path)
        self._assert_regular_file(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkflowError(f"{label}记录损坏或无法读取") from None
        if not isinstance(value, dict):
            raise WorkflowError(f"{label}记录结构无效")
        return value

    def _require_release_gates(
        self,
        run_dir: Path,
        run: RunRecord,
        source: Path,
        expected_sha: str,
        report: Mapping[str, object] | None = None,
    ) -> None:
        """Refuse archival unless the measured-audio and visual-review gates pass."""
        audio_path = run_dir / _AUDIO_GATE_NAME
        audio: dict[str, object] | None = None
        if audio_path.is_file():
            audio = self._read_gate_record(audio_path, "音频实测闸门")
            if audio.get("file_sha256") != expected_sha:
                raise WorkflowError("音频实测记录与待审核视频 SHA 不匹配；请重新运行音频闸门")
            if (
                audio.get("decision") != "pass"
                or audio.get("audio_status") != "present"
                or audio.get("audio_silent") is not False
            ):
                raise WorkflowError("音频实测闸门未通过：成片无音轨或为数字静音，不得归档")
        elif not (
            isinstance(report, Mapping)
            and report.get("sha256") == expected_sha
            and report.get("audio_ok") is True
            and report.get("audio_silent") is False
        ):
            raise WorkflowError(
                "缺少音频实测闸门记录：成片必须完成音轨实测（或校验报告带音频实测结论）后才能归档"
            )
        review_path = run_dir / _AI_REVIEW_NAME
        if not review_path.is_file():
            raise WorkflowError("缺少成片复审记录；每次生成都必须先完成人工/智能复审")
        review = self._read_gate_record(review_path, "成片复审")
        if review.get("file_sha256") != expected_sha:
            raise WorkflowError("复审记录与待审核视频 SHA 不匹配；请对当前成片重新复审")
        if review.get("prompt_sha256") != run.prompt_sha256:
            raise WorkflowError("复审记录与当前提示词 SHA 不匹配；请重新复审")
        if review.get("decision") != "pass":
            raise WorkflowError("复审判定不是通过，不得归档")

    def verify_no_watermark(self, run_dir: str | Path, *, evidence: str) -> dict[str, object]:
        """Persist explicit human frame-by-frame watermark review for a downloaded candidate."""
        run_path = self._assert_safe_run_dir(Path(run_dir))
        if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 240 or _find_sensitive(evidence):
            raise WorkflowError("人工水印复核证据无效")
        with _locked(run_path / ".workflow.lock"):
            run = self._load_run(run_path)
            metadata = self._read_metadata(run_path)
            transaction = metadata.get("download_transaction")
            if not isinstance(transaction, Mapping) or transaction.get("status") != "downloaded":
                raise WorkflowError("没有等待水印人工复核的下载候选")
            if (
                transaction.get("video_task_id") != run.video_task_id
                or transaction.get("video_external_id") != run.video_external_id
            ):
                raise WorkflowError("水印复核候选与当前版本任务编号不匹配")
            if isinstance(metadata.get("watermark_review"), Mapping):
                raise WorkflowError("当前候选已有水印复核记录；禁止覆盖审核状态")
            candidate, _final = self._download_paths(run_path, run.sku, transaction)
            self._assert_owned_download_file(candidate, run_path, transaction)
            review = {
                "status": "verified_no_watermark",
                "sku": run.sku,
                "version": run_path.name,
                "candidate_sha256": _sha256_file_stable(candidate),
                "video_task_id": transaction.get("video_task_id"),
                "video_external_id": transaction.get("video_external_id"),
                "evidence": evidence.strip(),
                "recorded_at": self._now_iso(),
                "review_type": "human_visual_review",
            }
            updated = dict(metadata)
            updated["watermark_review"] = review
            events = updated.get("role_events", [])
            if not isinstance(events, list):
                raise WorkflowError("职责日志状态损坏，拒绝写入水印复核")
            updated["role_events"] = [
                *events,
                {
                    "model": "local", "role": "download",
                    "reason": "保存人工水印复核状态",
                    "result": "verified_no_watermark", "evidence": evidence.strip(),
                    "recorded_at": review["recorded_at"], "sku": run.sku,
                    "version": run_path.name, "prompt_sha256": run.prompt_sha256,
                },
            ]
            self._write_metadata(run_path, updated)
            if self.processing_log is not None:
                recorder = getattr(self.processing_log, "record_step", None)
                if callable(recorder):
                    recorder(
                        model="local", role="download",
                        reason="保存人工水印复核状态",
                        result="verified_no_watermark", evidence="当前候选 SHA-256 已绑定",
                        sku=run.sku,
                        version=run_path.name if re.fullmatch(r"v\d{3,6}", run_path.name) else None,
                    )
            return review

    @staticmethod
    def _require_persisted_sol_review(run: RunRecord, metadata: Mapping[str, object]) -> None:
        review = metadata.get("sol_review")
        if (
            not isinstance(review, Mapping)
            or review.get("status") != "approved"
            or review.get("sku") != run.sku
            or review.get("version") != run.run_dir.name
            or review.get("prompt_sha256") != run.prompt_sha256
            or not isinstance(review.get("recorded_at"), str)
        ):
            raise ApprovalError("缺少绑定当前款号、版本和提示词 SHA 的 Sol 最终审核记录")

    def register_external_task(
        self,
        *,
        sku: str,
        version: str,
        prompt_sha256: str,
        task_id: str,
        external_task_id: str,
        paid_confirmation: bool,
        provider: str = "mcp",
    ) -> RunRecord:
        """Record an already-authorized MCP/API task without storing URLs or tokens."""
        if paid_confirmation is not True:
            raise ApprovalError("登记外部付费任务需要本轮明确的开始生成授权")
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if provider not in {"mcp", "api"}:
            raise WorkflowError("外部任务来源只允许 mcp 或 api")
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        if int(version[1:]) < 1 or version != f"v{int(version[1:]):03d}":
            raise WorkflowError("版本号必须是 v001 或更高版本")
        safe_task = self._safe_remote_identifier(task_id)
        safe_external = self._safe_remote_identifier(external_task_id)
        if safe_task is None or safe_external is None:
            raise WorkflowError("外部任务编号无效，未登记任务")
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if run.sku != sku or not self._uses_flat_version_paths(run_dir):
                raise WorkflowError("外部任务必须关联本项目扁平款号历史版本")
            existing_provider = metadata.get("external_provider")
            if existing_provider in {"mcp", "api"}:
                if (
                    existing_provider != provider
                    or metadata.get("video_task_id") != safe_task
                    or metadata.get("video_external_id") != safe_external
                    or metadata.get("prompt_sha256") != prompt_sha256
                ):
                    raise WorkflowError("该版本已绑定其他外部任务，禁止覆盖")
                return self._update_run(
                    run_dir,
                    status=TaskStatus.VIDEO_PROCESSING,
                    video_task_id=safe_task,
                    video_external_id=safe_external,
                )
            if run.status is not TaskStatus.WAITING_APPROVAL:
                raise WorkflowError("只有已保存并等待确认的版本可以登记外部任务")
            if (
                not isinstance(prompt_sha256, str)
                or not isinstance(run.prompt_sha256, str)
                or not hmac.compare_digest(run.prompt_sha256, prompt_sha256)
                or metadata.get("prompt_sha256") != prompt_sha256
            ):
                raise ApprovalError("外部任务提示词 SHA 与已确认版本不一致")
            self._verify_saved_prompt(run, metadata)
            self._require_persisted_sol_review(run, metadata)
            if run.video_task_id or run.video_external_id:
                raise WorkflowError("该版本已有视频任务编号，禁止重复创建外部任务")
            metadata.update(
                {
                    "stage": "external_video_processing",
                    "external_provider": provider,
                    "video_task_id": safe_task,
                    "video_external_id": safe_external,
                    "external_task_registered_at": self._now_iso(),
                    "paid_confirmation_recorded": True,
                }
            )
            self._write_metadata(run_dir, metadata)
            return self._update_run(
                run_dir,
                status=TaskStatus.VIDEO_PROCESSING,
                video_task_id=safe_task,
                video_external_id=safe_external,
            )

    def create_mcp_retry_coordinator(
        self,
        *,
        sku: str,
        version: str,
        cache: InMemoryAssetCache,
        required_assets: Sequence[str],
    ) -> McpRetryCoordinator:
        """Bind an in-memory MCP retry to one existing local run.

        This method reads only the selected version sidecar.  It never reads a
        different SKU and never persists connector addresses; the caller must
        supply any MCP upload result to the returned coordinator in memory.
        """
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        if int(version[1:]) < 1 or version != f"v{int(version[1:]):03d}":
            raise WorkflowError("版本号必须是 v001 或更高版本")
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if run.sku != sku or run.run_dir.name != version:
                raise WorkflowError("当前版本与款号不匹配，拒绝建立 MCP 重试")
            if run.status is not TaskStatus.WAITING_APPROVAL:
                raise WorkflowError("只有当前待确认版本可以建立 MCP 重试")
            if not isinstance(run.prompt_sha256, str) or metadata.get("prompt_sha256") != run.prompt_sha256:
                raise WorkflowError("当前版本提示词缓存缺失或不一致，拒绝重试")
            self._verify_saved_prompt(run, metadata)
            analysis_path = run_dir / _ANALYSIS_NAME
            prompt_path = run_dir / _PROMPT_NAME
            final_prompt_path = run_dir / _FINAL_PROMPT_NAME
            input_path = run_dir / _INPUT_NAME
            for path, label in (
                (analysis_path, "动作分析缓存"),
                (prompt_path, "提示词缓存"),
                (final_prompt_path, "最终提交提示词缓存"),
                (input_path, "当前素材输入清单"),
            ):
                if not path.is_file():
                    raise WorkflowError(f"{label}缺失，不能复用缓存重试")
            reference_group = metadata.get("reference_group_id", "default")
            if not isinstance(reference_group, str) or not _SAFE_IDENTIFIER.fullmatch(reference_group):
                raise WorkflowError("当前参考组标识无效，拒绝重试")
            try:
                scope = RetryScope(
                    sku=sku,
                    version=version,
                    reference_group=reference_group,
                    analysis_sha256=_sha256_file_stable(analysis_path),
                    prompt_sha256=run.prompt_sha256,
                    asset_fingerprint=_sha256_file_stable(input_path),
                )
            except (OSError, ValueError):
                raise WorkflowError("当前版本缓存指纹无效，拒绝重试") from None
        return McpRetryCoordinator(
            cache=cache,
            scope=scope,
            required_assets=tuple(required_assets),
        )

    def complete_external_task(
        self, *, sku: str, version: str, task_id: str, video_path: Path
    ) -> RunRecord:
        """Validate and publish a local external-task result through the shared gate."""
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("版本号必须是 v001 或更高版本")
        if int(version[1:]) < 1 or version != f"v{int(version[1:]):03d}":
            raise WorkflowError("版本号必须是 v001 或更高版本")
        safe_task = self._safe_remote_identifier(task_id)
        if safe_task is None:
            raise WorkflowError("外部任务编号无效，未导入本地结果")
        run_dir = self._assert_safe_run_dir(self._version_run_dir(sku, version), sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if run.sku != sku or not self._uses_flat_version_paths(run_dir):
                raise WorkflowError("外部结果必须关联本项目扁平款号历史版本")
            if run.status is TaskStatus.COMPLETED:
                transaction = metadata.get("download_transaction")
                final = self._video_output_path(sku, version, run_dir)
                if (
                    isinstance(transaction, Mapping)
                    and transaction.get("status") == "published"
                    and transaction.get("video_task_id") == safe_task
                    and final.is_file()
                    and transaction.get("sha256") == _sha256_file_stable(final)
                ):
                    return run
                raise WorkflowError("该版本已完成但结果身份不匹配，需人工处理")
            if (
                run.status is not TaskStatus.VIDEO_PROCESSING
                or metadata.get("stage") != "external_video_processing"
                or metadata.get("external_provider") not in {"mcp", "api"}
                or metadata.get("video_task_id") != safe_task
                or run.video_task_id != safe_task
                or metadata.get("video_external_id") != run.video_external_id
            ):
                raise WorkflowError("本地版本与外部任务登记不匹配，拒绝导入")
            expected_sha = run.prompt_sha256
            if (
                not isinstance(expected_sha, str)
                or metadata.get("prompt_sha256") != expected_sha
                or not _same_file_bytes(
                    run_dir / _PROMPT_NAME,
                    self._render_saved_prompt(run, metadata).encode("utf-8"),
                )
                or not _same_file_bytes(
                    run_dir / _FINAL_PROMPT_NAME,
                    self._render_saved_prompt(run, metadata).encode("utf-8"),
                )
                or not hmac.compare_digest(
                    prompt_sha256(self._render_saved_prompt(run, metadata)), expected_sha
                )
            ):
                raise WorkflowError("外部任务提示词 SHA 校验失败，拒绝导入结果")
            if metadata.get("download_transaction") is not None:
                raise WorkflowError("该外部任务已有下载事务；请用 resume-local-validation 继续")
            source = Path(video_path)
            _reject_reparse_components(source)
            self._assert_regular_file(source)
            source_digest = _sha256_file_stable(source)
            source_size = source.stat().st_size
            if source.suffix.lower() != ".mp4" or source_size <= 0:
                raise WorkflowError("外部结果必须是非空本地 MP4 文件")
            final = self._video_output_path(sku, version, run_dir)
            _reject_reparse_components(final)
            if final.exists():
                raise WorkflowError("待审核正式视频已存在，拒绝覆盖")
            nonce, candidate_name = self._new_candidate_identity(sku, run_dir)
            candidate = run_dir / candidate_name
            self._assert_download_path(candidate, run_dir)
            self._assert_task4_temporary_path_budget(candidate)
            try:
                with source.open("rb") as input_stream, candidate.open("xb") as output_stream:
                    while True:
                        block = input_stream.read(1024 * 1024)
                        if not block:
                            break
                        output_stream.write(block)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except FileExistsError:
                raise WorkflowError("候选视频路径发生冲突，原文件均已保留") from None
            self._assert_regular_output(candidate, run_dir)
            if _sha256_file_stable(candidate) != source_digest:
                raise WorkflowError("外部视频复制期间内容发生变化；候选文件已保留")
            candidate_stat = candidate.stat()
            transaction: dict[str, object] = {
                "status": "downloaded",
                "candidate": candidate_name,
                "candidate_nonce": nonce,
                "candidate_collisions": [],
                "planned_final": final.name,
                "video_task_id": safe_task,
                "video_external_id": run.video_external_id,
                "watermark_enabled": self._trusted_external_watermark(metadata, run),
                "size_bytes": source_size,
                "sha256": source_digest,
                "file_identity": {
                    "device": int(candidate_stat.st_dev),
                    "inode": int(candidate_stat.st_ino),
                },
                "source": "local_external_result",
            }
            metadata.update({"stage": "downloaded", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
            try:
                if transaction.get("watermark_enabled") is not False:
                    raise WorkflowError("缺少绑定当前任务的可核验服务端 watermark_info.enabled=False；候选保留待处理")
                manual_watermark_review = metadata.get("watermark_review")
                manual_watermark_verified = (
                    transaction.get("watermark_enabled") is False
                    and isinstance(manual_watermark_review, Mapping)
                    and manual_watermark_review.get("status") == "verified_no_watermark"
                    and manual_watermark_review.get("sku") == run.sku
                    and manual_watermark_review.get("version") == run.run_dir.name
                    and manual_watermark_review.get("candidate_sha256") == transaction.get("sha256")
                    and manual_watermark_review.get("video_task_id") == transaction.get("video_task_id")
                    and manual_watermark_review.get("video_external_id") == transaction.get("video_external_id")
                )
                validation = self._validate_video(
                    candidate, watermark_verified=manual_watermark_verified
                )
            except WorkflowError:
                metadata.update(
                    {
                        "stage": "validation_pending",
                        "download_transaction": transaction,
                        "validation_pending": "等待/未通过真实视频校验",
                    }
                )
                self._write_metadata(run_dir, metadata)
                raise
            report: dict[str, object] = {
                "validated": True,
                "watermark_verification": "verified_no_watermark",
                "candidate": candidate.name,
                "size_bytes": source_size,
                "sha256": source_digest,
                "video_task_id": safe_task,
                "video_external_id": run.video_external_id,
                "validation": validation,
                "source": "local_external_result",
            }
            self._write_exclusive(run_dir / _VIDEO_REPORT_NAME, _json_bytes(report))
            transaction.update({"status": "validated", "validation": validation})
            metadata.update(
                {
                    "stage": "validated",
                    "download_transaction": transaction,
                    "video_report": report,
                }
            )
            self._write_metadata(run_dir, metadata)
            self._require_trusted_portrait_report(metadata, transaction)
            if final.parent != run_dir:
                self._ensure_output_directory(final.parent)
            self._publish_video_candidate(candidate, final)
            self._assert_owned_download_file(final, run_dir, transaction)
            transaction.update({"status": "published", "final": final.name})
            metadata.update({"stage": "complete", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
            self._record_local_audit(run, final, metadata)
            return self._update_run(run_dir, status=TaskStatus.COMPLETED)

    @staticmethod
    def _trusted_external_watermark(metadata: Mapping[str, object], run: RunRecord) -> bool | None:
        proof = metadata.get("server_response_proof")
        if (
            isinstance(proof, Mapping)
            and proof.get("source") == "authenticated_client_query"
            and proof.get("task_id") == run.video_task_id
            and proof.get("external_task_id") == run.video_external_id
            and proof.get("watermark_enabled") is False
        ):
            return False
        return None

    def resume_local_validation(
        self, sku: str, version: str, *, task_id: str | None = None
    ) -> RunRecord:
        """Complete an externally imported candidate using only persisted local evidence."""
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("本地恢复必须指定有效的 v001 版本号")
        run_dir = self._version_run_dir(sku, version)
        if not run_dir.exists():
            raise WorkflowError("指定版本没有可本地恢复的任务")
        run_dir = self._assert_safe_run_dir(run_dir, sku)
        with _locked(run_dir / ".workflow.lock"):
            run = self._load_run(run_dir)
            if run.sku != sku or run_dir.name != version:
                raise WorkflowError("指定版本与款号任务记录不匹配")
            if task_id is not None and run.video_task_id != task_id:
                raise WorkflowError("指定 task-id 与本地任务不匹配")
            metadata = self._read_metadata(run_dir)
            transaction = metadata.get("download_transaction")
            if (
                metadata.get("external_provider") not in {"mcp", "api"}
                or metadata.get("stage") != "validation_pending"
                or not isinstance(transaction, Mapping)
                or transaction.get("status") != "downloaded"
            ):
                raise WorkflowError("没有等待本地恢复的外部结果候选")
            if self._trusted_external_watermark(metadata, run) is not False:
                raise WorkflowError("服务端无水印证明缺失或未知；候选保留待处理")
            review = metadata.get("watermark_review")
            candidate, final = self._download_paths(run_dir, run.sku, transaction)
            self._assert_owned_download_file(candidate, run_dir, transaction)
            if (
                not isinstance(review, Mapping)
                or review.get("status") != "verified_no_watermark"
                or review.get("candidate_sha256") != _sha256_file_stable(candidate)
                or review.get("video_task_id") != run.video_task_id
                or review.get("video_external_id") != run.video_external_id
            ):
                raise WorkflowError("缺少绑定候选 SHA 的人工无水印复核；候选保留")
            validation = self._validate_video(
                candidate,
                watermark_verified=True,
                require_audio=True,
            )
            report = {
                "validated": True, "watermark_verification": "verified_no_watermark",
                "candidate": candidate.name, "size_bytes": candidate.stat().st_size,
                "sha256": _sha256_file_stable(candidate), "video_task_id": run.video_task_id,
                "video_external_id": run.video_external_id, "validation": validation,
                "source": "local_external_result",
            }
            self._write_exclusive(run_dir / _VIDEO_REPORT_NAME, _json_bytes(report))
            transaction.update({"status": "validated", "validation": validation})
            metadata.update({"stage": "validated", "download_transaction": transaction, "video_report": report})
            self._write_metadata(run_dir, metadata)
            self._require_trusted_portrait_report(metadata, transaction)
            if final.parent != run_dir:
                self._ensure_output_directory(final.parent)
            self._publish_video_candidate(candidate, final)
            self._assert_owned_download_file(final, run_dir, transaction)
            transaction.update({"status": "published", "final": final.name})
            metadata.update({"stage": "complete", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
            self._record_local_audit(run, final, metadata)
            return self._update_run(run_dir, status=TaskStatus.COMPLETED)

    def resume(self, sku: str) -> RunRecord:
        run_dir = self._find_latest_run(sku)
        if run_dir is None:
            raise WorkflowError("该款号没有可续查的本地任务")
        with _locked(run_dir / ".workflow.lock"):
            latest = self._find_latest_run(sku)
            if latest != run_dir:
                raise WorkflowError("续查期间出现了更新任务，请重新选择该款号最近任务")
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if metadata.get("external_provider") in {"mcp", "api"} and metadata.get("stage") == "validation_pending":
                raise WorkflowError("外部结果仅允许纯本地 resume-local-validation 恢复；不会发起 API 查询")
            if metadata.get("external_provider") in {"mcp", "api"} and metadata.get("stage") == "external_video_processing":
                if metadata.get("external_provider") == "api":
                    self._query_video_external(run, run.video_external_id)
                    return self.store.load(run_dir)
                raise WorkflowError(
                    "外部任务已登记；请用 complete-external-task 导入本地结果，不会自动查询或重新提交"
                )
            if run.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                return run
            if run.status is TaskStatus.WAITING_APPROVAL:
                return run
            if run.status is TaskStatus.ANALYZING:
                return run
            if run.status is TaskStatus.APPROVED and not run.element_external_id:
                raise WorkflowError("已确认但尚无远程任务标识；请人工处理，本程序不会自动提交")

            stage = metadata.get("stage")
            if run.element_external_id and not run.element_task_id and not run.video_external_id:
                element = self._query_element_external(run, run.element_external_id)
                run = self._persist_element_task(run, element)
                element = self._poll_element(run, element)
                if element.status == "succeed" and self._valid_element_id(element.element_id):
                    self._cache_element(run, metadata, element)
                    metadata.update(
                        {
                            "stage": "element_ready",
                            "element_id": element.element_id,
                            "element_task_id": self._safe_remote_identifier(element.task_id),
                        }
                    )
                    self._write_metadata(run_dir, metadata)
                return self.store.load(run_dir)
            if run.element_task_id and stage in {
                "element_submitting",
                "element_processing",
                "element_ready",
                "element_timed_out",
            } and not run.video_external_id:
                element = self._query_element_task(run, run.element_task_id)
                element = self._poll_element(run, element)
                if element.status == "succeed" and self._valid_element_id(element.element_id):
                    self._cache_element(run, metadata, element)
                    metadata.update({"stage": "element_ready", "element_id": element.element_id})
                    self._write_metadata(run_dir, metadata)
                return self.store.load(run_dir)

            if run.video_external_id and not run.video_task_id:
                remote_video = self._query_video_external(run, run.video_external_id)
                run = self._persist_video_task(run, remote_video)
                return self._poll_download_and_validate(run, remote_video)
            if run.video_task_id:
                remote_video = self._query_video_task(run, run.video_task_id)
                return self._poll_download_and_validate(run, remote_video)
            if run.status is TaskStatus.TIMED_OUT:
                raise WorkflowError("超时任务缺少可查询的远程任务标识；请人工处理")
            raise WorkflowError("当前任务没有安全的只读续查路径；不会创建新任务")

    def _submit_video(
        self,
        run: RunRecord,
        metadata: dict[str, object],
        prepared: PreparedAssets,
        element: RemoteElement,
    ) -> RunRecord:
        if run.video_external_id:
            remote = self._query_video_external(run, run.video_external_id)
            run = self._persist_video_task(run, remote)
            return self._poll_download_and_validate(run, remote)

        external_id = self._new_external_id("video", run.sku)
        audio_policy = str(metadata.get("audio_policy", "generate_music"))
        if audio_policy == "preserve_original":
            raise WorkflowError(
                "当前本地 API 图生视频入口不能可靠保留参考原音；"
                "请使用可灵 MCP 的 enable_audio=false、keepOriginalSound=true 提交"
            )
        try:
            payload = build_video_payload(
                prepared,
                self._saved_shots(metadata),
                int(element.element_id),
                external_id,
                modification_feedback=(
                    metadata.get("modification_feedback")
                    if isinstance(metadata.get("modification_feedback"), str)
                    else None
                ),
                full_prompt_markdown=self._render_saved_prompt(run, metadata),
                confirmed_prompt_sha256=run.prompt_sha256,
                audio_policy=audio_policy,
            )
        except (PromptError, OSError, ValueError, TypeError) as exc:
            raise WorkflowError(_safe_text(str(exc)) or "无法构造可灵视频请求") from None
        run = self._update_run(
            run.run_dir,
            status=TaskStatus.VIDEO_SUBMITTING,
            video_external_id=external_id,
            element_task_id=self._safe_remote_identifier(element.task_id),
        )
        metadata.update({"stage": "video_submitting"})
        self._write_metadata(run.run_dir, metadata)
        try:
            remote = self._client().create_video(payload)
        except NetworkUnknownOutcome:
            remote = self._query_video_external(run, external_id, unknown_outcome=True)
        except Exception as exc:
            self._fail_safely(run, metadata, "video_post", exc)
            raise WorkflowError(f"可灵视频创建失败（{type(exc).__name__}）；不会自动重试") from None
        run = self._persist_video_task(run, remote)
        return self._poll_download_and_validate(run, remote)

    def _create_or_recover_element(
        self, run: RunRecord, prepared: PreparedAssets, external_id: str
    ) -> RemoteElement:
        try:
            remote = self._client().create_element(prepared, run.sku, external_id)
        except NetworkUnknownOutcome:
            return self._query_element_external(run, external_id, unknown_outcome=True)
        except Exception as exc:
            self._fail_safely(run, self._read_metadata(run.run_dir), "element_post", exc)
            raise WorkflowError(f"可灵元素创建失败（{type(exc).__name__}）；不会自动重试") from None
        return self._as_element(remote)

    def _query_element_external(
        self, run: RunRecord, external_id: str, *, unknown_outcome: bool = False
    ) -> RemoteElement:
        try:
            remote = self._client().query_element(external_task_id=external_id)
        except Exception as exc:
            if self._is_not_found(exc):
                raise WorkflowError(
                    "可灵未找到已提交元素；为避免重复付费，请人工处理，不会再次提交"
                ) from None
            raise WorkflowError("无法安全查询可灵元素；保留 external_task_id 供后续续查") from None
        if not isinstance(remote, RemoteElement):
            remote = self._as_element(remote)
        if remote.external_task_id != external_id:
            raise WorkflowError("可灵元素查询编号与本地 external_task_id 不一致，已停止恢复")
        return remote

    def _query_element_task(self, run: RunRecord, task_id: str) -> RemoteElement:
        try:
            remote = self._client().query_element(task_id=task_id)
        except Exception:
            raise WorkflowError("无法安全查询可灵元素任务；保留本地状态供后续续查") from None
        return remote if isinstance(remote, RemoteElement) else self._as_element(remote)

    def _confirmed_cached_element(
        self, run: RunRecord, metadata: dict[str, object]
    ) -> RemoteElement | None:
        fingerprint = metadata.get("element_fingerprint")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise WorkflowError("任务缺少有效素材指纹，不能安全提交")
        entries = self._read_cache(fingerprint)
        for entry in entries:
            try:
                remote = self._client().query_element(task_id=entry["task_id"])
            except Exception as exc:
                if self._is_not_found(exc):
                    continue
                raise WorkflowError("无法确认缓存元素状态；为避免重复付费，已停止") from None
            if (
                isinstance(remote, RemoteElement)
                and remote.status == "succeed"
                and remote.element_id == entry["element_id"]
                and self._valid_element_id(remote.element_id)
            ):
                return remote
            if isinstance(remote, RemoteElement) and remote.status in {"failed", "succeed"}:
                continue
            if isinstance(remote, RemoteElement):
                run = self._update_run(
                    run.run_dir,
                    status=TaskStatus.ELEMENT_PROCESSING,
                    element_task_id=self._safe_remote_identifier(remote.task_id),
                )
                metadata.update({"stage": "element_processing"})
                self._write_metadata(run.run_dir, metadata)
                remote = self._poll_element(run, remote)
                if remote.status == "succeed" and remote.element_id == entry["element_id"]:
                    return remote
                if remote.status != "failed":
                    raise WorkflowError("缓存元素仍在处理中；为避免重复付费，已停止")
        return None

    def _poll_element(self, run: RunRecord, initial: RemoteElement | RemoteTask) -> RemoteElement:
        remote = self._as_element(initial)
        deadline = self._monotonic() + self.poll_timeout_seconds
        while remote.status not in {"succeed", "failed"}:
            if self._monotonic() >= deadline:
                self._mark_timed_out(run, "element")
                return remote
            self._sleep(min(self.poll_interval_seconds, deadline - self._monotonic()))
            if run.element_task_id:
                remote = self._query_element_task(run, run.element_task_id)
            elif run.element_external_id:
                remote = self._query_element_external(run, run.element_external_id)
            else:
                raise WorkflowError("元素任务缺少可查询的远程标识")
            run = self._update_run(
                run.run_dir,
                status=TaskStatus.ELEMENT_PROCESSING,
                element_task_id=self._safe_remote_identifier(remote.task_id),
            )
            if self._monotonic() >= deadline and remote.status not in {"succeed", "failed"}:
                self._mark_timed_out(run, "element")
                return remote
        if remote.status == "failed":
            self._record_remote_failure(run, remote, "element")
            return remote
        if not self._valid_element_id(remote.element_id):
            raise WorkflowError("可灵元素任务已成功，但缺少有效 element_id")
        return remote

    def _persist_element_task(self, run: RunRecord, remote: RemoteTask) -> RunRecord:
        task_id = self._safe_remote_identifier(remote.task_id)
        if task_id is None:
            raise WorkflowError("可灵元素响应缺少安全任务编号；保留 external_task_id")
        if remote.external_task_id and remote.external_task_id != run.element_external_id:
            raise WorkflowError("可灵元素响应编号与本地 external_task_id 不一致")
        run = self._update_run(
            run.run_dir,
            status=TaskStatus.ELEMENT_PROCESSING,
            element_task_id=task_id,
        )
        metadata = self._read_metadata(run.run_dir)
        metadata.update({"stage": "element_processing"})
        self._write_metadata(run.run_dir, metadata)
        return run

    def _persist_cached_element_task(self, run: RunRecord, remote: RemoteElement) -> RunRecord:
        task_id = self._safe_remote_identifier(remote.task_id)
        if task_id is None or not self._valid_element_id(remote.element_id):
            raise WorkflowError("缓存元素查询缺少有效任务或元素编号，已停止复用")
        run = self._update_run(
            run.run_dir,
            status=TaskStatus.ELEMENT_PROCESSING,
            element_task_id=task_id,
        )
        metadata = self._read_metadata(run.run_dir)
        metadata.update({"stage": "element_processing", "cached_element": True})
        self._write_metadata(run.run_dir, metadata)
        return run

    def _persist_video_task(self, run: RunRecord, remote: RemoteTask) -> RunRecord:
        task_id = self._safe_remote_identifier(remote.task_id)
        if task_id is None:
            raise WorkflowError("可灵视频响应缺少安全任务编号；保留 external_task_id")
        if remote.external_task_id and remote.external_task_id != run.video_external_id:
            raise WorkflowError("可灵视频响应编号与本地 external_task_id 不一致")
        run = self._update_run(
            run.run_dir,
            status=TaskStatus.VIDEO_PROCESSING,
            video_task_id=task_id,
        )
        metadata = self._read_metadata(run.run_dir)
        metadata.update({"stage": "video_processing"})
        self._write_metadata(run.run_dir, metadata)
        return run

    def _poll_download_and_validate(
        self, run: RunRecord, initial: RemoteTask | RemoteVideo
    ) -> RunRecord:
        remote = self._as_video(initial)
        deadline = self._monotonic() + self.poll_timeout_seconds
        while remote.status not in {"succeed", "failed"}:
            if self._monotonic() >= deadline:
                self._mark_timed_out(run, "video")
                return self.store.load(run.run_dir)
            self._sleep(min(self.poll_interval_seconds, deadline - self._monotonic()))
            if run.video_task_id:
                remote = self._query_video_task(run, run.video_task_id)
            elif run.video_external_id:
                remote = self._query_video_external(run, run.video_external_id)
            else:
                raise WorkflowError("视频任务缺少可查询的远程标识")
            run = self._update_run(
                run.run_dir,
                status=TaskStatus.VIDEO_PROCESSING,
                video_task_id=self._safe_remote_identifier(remote.task_id),
            )
            if self._monotonic() >= deadline and remote.status not in {"succeed", "failed"}:
                self._mark_timed_out(run, "video")
                return self.store.load(run.run_dir)
        if remote.status == "failed":
            return self._record_remote_failure(run, remote, "video")
        if (
            not isinstance(remote, RemoteVideo)
            or not remote.download_url
            or remote.watermark_enabled is not False
        ):
            raise WorkflowError("可灵结果缺少明确标记为无水印的下载地址")
        return self._download_and_validate(run, remote)

    def _download_and_validate(self, run: RunRecord, remote: RemoteVideo) -> RunRecord:
        if remote.watermark_enabled is not False or not remote.download_url:
            raise WorkflowError("可灵结果缺少明确标记为无水印的下载地址")
        run_dir = self._assert_safe_run_dir(run.run_dir, run.sku)
        final = self._video_output_path(run.sku, run_dir.name, run_dir)
        _reject_reparse_components(final)
        metadata = self._read_metadata(run_dir)
        transaction = metadata.get("download_transaction")
        if transaction is None:
            if final.exists():
                raise WorkflowError(f"视频输出已存在，已保留原文件：{final.name}")
            candidate_nonce, candidate_name = self._new_candidate_identity(run.sku, run_dir)
            task_id = self._safe_remote_identifier(run.video_task_id or remote.task_id)
            external_id = run.video_external_id
            if task_id is None and external_id is None:
                raise WorkflowError("下载缺少本任务视频编号，不能建立归属记录")
            transaction = {
                "status": "planned",
                "candidate": candidate_name,
                "candidate_nonce": candidate_nonce,
                "candidate_collisions": [],
                "planned_final": final.name,
                "video_task_id": task_id,
                "video_external_id": external_id,
                "watermark_enabled": remote.watermark_enabled,
            }
            metadata.update({"stage": "download_planned", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
        if not isinstance(transaction, Mapping):
            raise WorkflowError("下载事务记录无效；候选与正式文件均保留")
        transaction = dict(transaction)
        candidate, final = self._download_paths(run_dir, run.sku, transaction)
        self._assert_task4_temporary_path_budget(candidate)
        self._verify_download_transaction_identity(run, remote, transaction)
        run = self._update_run(run_dir, status=TaskStatus.DOWNLOADING)

        state = transaction.get("status")
        if state == "planned":
            if candidate.exists():
                raise WorkflowError("计划中的候选文件已存在但没有持久归属证明；已保留并停止")
            if final.exists():
                raise WorkflowError(f"正式视频输出已存在且不属于本次任务；已保留：{final.name}")
            while True:
                try:
                    downloaded = self._client().download(
                        remote.download_url,
                        candidate,
                        allowed_root=self.paths.results_for(run.sku),
                    )
                except DownloadTargetExists:
                    transaction = self._replan_after_candidate_collision(
                        run_dir, metadata, transaction, candidate, run.sku
                    )
                    candidate, final = self._download_paths(run_dir, run.sku, transaction)
                    self._assert_task4_temporary_path_budget(candidate)
                    continue
                except Exception:
                    self._record_processing_step(
                        run=run,
                        role="download",
                        reason="记录本地任务结果下载异常",
                        result="下载失败",
                        evidence="候选视频尚未完成本地校验",
                        exception="候选下载失败",
                    )
                    raise WorkflowError("候选视频下载失败；下载计划已保留，可只读续查") from None
                break
            try:
                if Path(downloaded).resolve(strict=True) != candidate.resolve(strict=True):
                    raise WorkflowError("下载器返回的候选路径与已批准路径不一致")
                self._assert_regular_output(candidate, run_dir)
                candidate_stat = candidate.lstat()
                transaction.update(
                    {
                        "status": "downloaded",
                        "file_identity": {
                            "device": int(candidate_stat.st_dev),
                            "inode": int(candidate_stat.st_ino),
                        },
                        "size_bytes": int(candidate_stat.st_size),
                        "sha256": _sha256_file(candidate),
                    }
                )
            except WorkflowError:
                raise
            except Exception:
                raise WorkflowError("候选下载文件无法安全归属；已保留证据") from None
            metadata.update({"stage": "downloaded", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
            state = "downloaded"

        if state == "downloaded":
            self._assert_owned_download_file(candidate, run_dir, transaction)
            if final.exists():
                raise WorkflowError("校验前正式视频文件已存在且不属于本次任务；已保留")
            try:
                manual_watermark_review = metadata.get("watermark_review")
                manual_watermark_verified = (
                    transaction.get("watermark_enabled") is False
                    and isinstance(manual_watermark_review, Mapping)
                    and manual_watermark_review.get("status") == "verified_no_watermark"
                    and manual_watermark_review.get("sku") == run.sku
                    and manual_watermark_review.get("version") == run.run_dir.name
                    and manual_watermark_review.get("candidate_sha256") == transaction.get("sha256")
                    and manual_watermark_review.get("video_task_id") == transaction.get("video_task_id")
                    and manual_watermark_review.get("video_external_id") == transaction.get("video_external_id")
                )
                validation = self._validate_video(
                    candidate,
                    watermark_verified=manual_watermark_verified,
                    require_audio=True,
                )
            except WorkflowError:
                metadata.update(
                    {
                        "stage": "validation_pending",
                        "download_transaction": transaction,
                        "validation_pending": "等待/未通过真实视频校验",
                    }
                )
                self._write_metadata(run_dir, metadata)
                self._record_processing_step(
                    run=run,
                    role="download",
                    reason="记录下载后本地视频校验异常",
                    result="校验失败",
                    evidence="候选视频已保留",
                    exception="本地视频校验未通过",
                )
                raise
            self._record_processing_step(
                run=run,
                role="download",
                reason="记录下载后本地视频校验结果",
                result="校验通过",
                evidence="可灵无水印标记与本地视频校验",
            )
            self._assert_owned_download_file(candidate, run_dir, transaction)
            report = {
                "validated": True,
                "watermark_verification": "verified_no_watermark",
                "candidate": candidate.name,
                "size_bytes": transaction["size_bytes"],
                "sha256": transaction["sha256"],
                "video_task_id": transaction.get("video_task_id"),
                "video_external_id": transaction.get("video_external_id"),
                "validation": validation,
            }
            report_bytes = _json_bytes(report)
            self._write_exclusive(run_dir / _VIDEO_REPORT_NAME, report_bytes)
            transaction.update({"status": "validated", "validation": validation})
            metadata.update(
                {
                    "stage": "validated",
                    "download_transaction": transaction,
                    "video_report": report,
                    "remote_result": self._safe_remote_fields(remote),
                }
            )
            self._write_metadata(run_dir, metadata)
            state = "validated"

        if state == "validated":
            self._require_trusted_portrait_report(metadata, transaction)
            if candidate.exists():
                self._assert_owned_download_file(candidate, run_dir, transaction)
                if final.exists():
                    raise WorkflowError("正式视频文件已存在；候选与正式输出均保留，需人工处理")
                if final.parent != run_dir:
                    self._ensure_output_directory(final.parent)
                try:
                    self._publish_video_candidate(candidate, final)
                except FileExistsError:
                    raise WorkflowError("正式视频文件发生并发冲突；已保留现有文件") from None
                self._assert_owned_download_file(final, run_dir, transaction)
                if candidate.exists():
                    raise WorkflowError("正式视频发布后候选文件仍存在；需人工检查")
            elif final.exists():
                # Recover only a completed same-volume rename backed by the durable
                # validated identity, size, digest, and remote task identifiers.
                self._assert_owned_download_file(final, run_dir, transaction)
            else:
                raise WorkflowError("已验证候选与正式视频均不存在；保留事务并需人工处理")
            transaction.update({"status": "published", "final": final.name})
            metadata.update({"stage": "complete", "download_transaction": transaction})
            self._write_metadata(run_dir, metadata)
            state = "published"

        if state != "published":
            raise WorkflowError("下载事务状态不支持安全续查；候选文件已保留")
        if candidate.exists() or not final.exists():
            raise WorkflowError("正式视频发布状态与本地文件不一致；需人工处理")
        self._assert_owned_download_file(final, run_dir, transaction)
        report = metadata.get("video_report")
        if not isinstance(report, Mapping) or report.get("validated") is not True:
            raise WorkflowError("正式视频缺少已验证报告；未标记完成")
        self._require_trusted_portrait_report(metadata, transaction)
        if self._uses_flat_version_paths(run_dir) and metadata.get("source_layout") == "gemini":
            self._record_local_audit(run, final, metadata)
        return self._update_run(run_dir, status=TaskStatus.COMPLETED)

    @staticmethod
    def _require_trusted_portrait_report(
        metadata: Mapping[str, object], transaction: Mapping[str, object]
    ) -> None:
        report = metadata.get("video_report")
        validation = report.get("validation") if isinstance(report, Mapping) else None
        transaction_validation = transaction.get("validation")
        if (
            not isinstance(report, Mapping)
            or report.get("validated") is not True
            or report.get("sha256") != transaction.get("sha256")
            or not isinstance(validation, Mapping)
            or not isinstance(transaction_validation, Mapping)
            or dict(validation) != dict(transaction_validation)
        ):
            raise WorkflowError("正式视频缺少可信真实校验报告；候选保留，未发布")
        width, height = validation.get("width"), validation.get("height")
        if (
            isinstance(width, bool)
            or isinstance(height, bool)
            or not isinstance(width, (int, float))
            or not isinstance(height, (int, float))
            or width <= 0
            or height <= 0
            or width >= height
        ):
            raise WorkflowError("真实校验报告缺少有效 width < height；候选保留，未发布")

    def _record_local_audit(
        self, run: RunRecord, video_path: Path, metadata: dict[str, object]
    ) -> None:
        report = metadata.get("video_report")
        validation = report.get("validation") if isinstance(report, Mapping) else None
        roles = metadata.get("material_roles")
        reference_names = [
            str(name)
            for names in roles.values()
            if isinstance(names, (list, tuple))
            for name in names
            if isinstance(name, str)
        ] if isinstance(roles, Mapping) else []
        dimensions = self._source_dimensions(metadata)
        expected_ratio = None
        if dimensions:
            divisor = math.gcd(*dimensions)
            expected_ratio = f"{dimensions[0] // divisor}:{dimensions[1] // divisor}"
        try:
            artifacts = write_local_audit(
                sku=run.sku,
                version=run.run_dir.name,
                video_path=video_path,
                history_sku_dir=self.paths.history / run.sku,
                validation=dict(validation) if isinstance(validation, Mapping) else {},
                reference_names=reference_names,
                expected_aspect_ratio=expected_ratio,
            )
            metadata.update(
                {
                    "audit_mode": "local_only",
                    "audit_status": "需要人工复核",
                    "audit_artifacts": artifacts,
                }
            )
            metadata.pop("audit_error", None)
        except Exception as exc:
            metadata.update(
                {
                    "audit_mode": "local_only",
                    "audit_status": "审核失败，需人工复核",
                    "audit_error": _safe_text(str(exc)) or "本地审核产物写入失败",
                }
            )
        self._write_metadata(run.run_dir, metadata)

    def _download_paths(
        self,
        run_dir: Path,
        sku: str,
        transaction: Mapping[str, object],
    ) -> tuple[Path, Path]:
        candidate_name = transaction.get("candidate")
        candidate_nonce = transaction.get("candidate_nonce")
        final_name = transaction.get("planned_final")
        expected_final = self._video_output_path(sku, run_dir.name, run_dir)
        if (
            not isinstance(candidate_name, str)
            or not isinstance(candidate_nonce, str)
            or not _CANDIDATE_NONCE.fullmatch(candidate_nonce)
            or candidate_name != self._candidate_name(sku, run_dir, candidate_nonce)
            or final_name != expected_final.name
        ):
            raise WorkflowError("下载候选或正式路径不符合本任务目录约束")
        candidate = run_dir / candidate_name
        final = expected_final
        self._assert_download_path(candidate, run_dir)
        self._assert_download_path(final, run_dir)
        return candidate, final

    def _new_candidate_identity(self, sku: str, run_dir: Path) -> tuple[str, str]:
        nonce = uuid.uuid4().hex
        return nonce, self._candidate_name(sku, run_dir, nonce)

    def _candidate_name(self, sku: str, run_dir: Path, nonce: str) -> str:
        if not _CANDIDATE_NONCE.fullmatch(nonce):
            raise WorkflowError("下载候选随机编号无效")
        return f".c-{nonce[:_CANDIDATE_TOKEN_HEX_LENGTH]}.mp4"

    def _assert_task4_temporary_path_budget(self, candidate: Path) -> None:
        if os.name != "nt":
            return
        # Task 4 uses mkstemp(prefix=f".{target.name}.", suffix=".download");
        # tempfile currently appends eight random characters to that name.
        temporary_name = (
            f".{candidate.name}.{'x' * _TASK4_TEMP_RANDOM_LENGTH}.download"
        )
        try:
            normalized_path = os.path.normpath(
                os.path.abspath(os.fspath(candidate.parent / temporary_name))
            )
            path_code_units = len(normalized_path.encode("utf-16-le")) // 2
        except (OSError, TypeError, UnicodeError):
            raise WorkflowError(
                "无法安全检查本地视频输出路径，尚未开始下载"
            ) from None
        if path_code_units > _WINDOWS_MAX_PATH:
            raise WorkflowError(
                "本地视频输出路径过长，尚未开始下载；请缩短项目路径后重新续查"
            )

    def _replan_after_candidate_collision(
        self,
        run_dir: Path,
        metadata: dict[str, object],
        transaction: dict[str, object],
        candidate: Path,
        sku: str,
    ) -> dict[str, object]:
        _reject_reparse_components(candidate)
        try:
            metadata_stat = candidate.lstat()
        except OSError:
            raise WorkflowError("下载候选冲突后无法安全复核；已保留事务并停止") from None
        if not stat.S_ISREG(metadata_stat.st_mode):
            raise WorkflowError("下载候选冲突文件不是普通文件；已保留并停止")

        collisions = transaction.get("candidate_collisions", [])
        if not isinstance(collisions, list) or not all(
            isinstance(name, str) for name in collisions
        ):
            raise WorkflowError("下载候选冲突历史无效；已停止")
        collisions = list(collisions)
        if candidate.name not in collisions:
            collisions.append(candidate.name)
        if len(collisions) >= _MAX_CANDIDATE_COLLISIONS:
            raise WorkflowError("下载候选连续发生排他冲突；已保留文件并停止")

        for _ in range(_MAX_CANDIDATE_COLLISIONS - len(collisions)):
            nonce, name = self._new_candidate_identity(sku, run_dir)
            if name in collisions or (run_dir / name).exists():
                if name not in collisions:
                    collisions.append(name)
                if len(collisions) >= _MAX_CANDIDATE_COLLISIONS:
                    raise WorkflowError(
                        "下载候选连续发生排他冲突；已保留文件并停止"
                    )
                continue
            replanned = dict(transaction)
            replanned.update(
                {
                    "status": "planned",
                    "candidate": name,
                    "candidate_nonce": nonce,
                    "candidate_collisions": collisions,
                }
            )
            metadata.update(
                {"stage": "download_planned", "download_transaction": replanned}
            )
            self._write_metadata(run_dir, metadata)
            return replanned
        raise WorkflowError("无法为下载候选生成新的排他编号；已保留所有冲突文件")

    def _assert_download_path(self, path: Path, run_dir: Path) -> None:
        _reject_reparse_components(path)
        self._check_output_parent(path.parent)
        try:
            resolved = Path(path).resolve(strict=False)
            try:
                resolved.relative_to(run_dir.resolve(strict=True))
                return
            except ValueError:
                sku = (
                    run_dir.parent.parent.name
                    if run_dir.parent.name == ".workflow"
                    else run_dir.parent.name
                )
                expected_final = self._video_output_path(sku, run_dir.name, run_dir)
                if resolved == expected_final.resolve(strict=False):
                    resolved.relative_to(self.paths.pending.resolve(strict=False))
                    return
        except (OSError, ValueError):
            pass
        raise WorkflowError("下载路径逃逸出当前任务目录或固定待审核目录")

    def _video_output_path(self, sku: str, version: str, run_dir: Path) -> Path:
        if self._uses_flat_version_paths(run_dir):
            return self.paths.pending / sku / f"{version}.mp4"
        if (self.paths.materials / sku).is_dir():
            return self.paths.pending / sku / version / f"{sku}_{version}.mp4"
        return run_dir / f"{sku}_{version}.mp4"

    def _version_run_dir(self, sku: str, version: str) -> Path:
        sidecar = self.paths.history / sku / ".workflow" / version
        if sidecar.is_dir():
            return sidecar
        return self.paths.history / sku / version

    @staticmethod
    def _uses_flat_version_paths(run_dir: Path) -> bool:
        return Path(run_dir).parent.name == ".workflow"

    def _ensure_output_directory(self, directory: Path) -> None:
        _reject_reparse_components(directory)
        self._check_output_parent(directory)
        missing: list[Path] = []
        current = directory
        while current != self.paths.root and not current.exists():
            missing.append(current)
            current = current.parent
        if current != self.paths.root and not current.is_dir():
            raise WorkflowError("待审核输出路径组件不是文件夹")
        for item in reversed(missing):
            try:
                item.mkdir()
            except FileExistsError:
                if item.is_symlink() or not item.is_dir():
                    raise WorkflowError("待审核输出路径发生冲突，已停止") from None
            except OSError:
                raise WorkflowError("无法创建待审核输出目录") from None
            _reject_reparse_components(item)

    def _verify_download_transaction_identity(
        self,
        run: RunRecord,
        remote: RemoteVideo,
        transaction: Mapping[str, object],
    ) -> None:
        task_id = self._safe_remote_identifier(run.video_task_id or remote.task_id)
        if task_id is None or transaction.get("video_task_id") != task_id:
            raise WorkflowError("下载事务与本地视频 task_id 不一致，禁止认领")
        if transaction.get("video_external_id") != run.video_external_id:
            raise WorkflowError("下载事务与本地 video external_task_id 不一致，禁止认领")
        if remote.watermark_enabled is not False or transaction.get("watermark_enabled") is not False:
            raise WorkflowError("缺少服务端明确无水印状态；下载事务保持未完成")

    def _assert_owned_download_file(
        self,
        path: Path,
        run_dir: Path,
        transaction: Mapping[str, object],
    ) -> None:
        self._assert_download_path(path, run_dir)
        self._assert_regular_output(path, run_dir)
        identity = transaction.get("file_identity")
        if not isinstance(identity, Mapping):
            raise WorkflowError("候选/正式视频缺少持久归属证明；已保留文件并停止")
        candidate_stat = path.lstat()
        if (
            identity.get("device") != int(candidate_stat.st_dev)
            or identity.get("inode") != int(candidate_stat.st_ino)
            or transaction.get("size_bytes") != int(candidate_stat.st_size)
            or transaction.get("sha256") != _sha256_file(path)
        ):
            raise WorkflowError("候选/正式视频身份或 SHA 与持久归属证明不符；已保留文件")

    def _publish_video_candidate(self, candidate: Path, final: Path) -> None:
        _reject_reparse_components(candidate)
        _reject_reparse_components(final)
        if final.exists():
            raise FileExistsError(final.name)
        if os.name == "nt":
            os.rename(candidate, final)
        else:
            os.link(candidate, final)
            candidate.unlink()

    def _validate_video(
        self,
        path: Path,
        *,
        watermark_verified: bool = False,
        require_audio: bool = False,
    ) -> object:
        if self.validator is None:
            raise WorkflowError("等待真实视频校验器；未通过真实视频校验，候选文件已保留")
        try:
            validate = self.validator if callable(self.validator) else getattr(self.validator, "validate", None)
            if not callable(validate):
                raise TypeError("validator must be callable")
            expected_ratio = self._source_aspect_ratio(path.parent)
            signature = inspect.signature(validate)
            supports_ratio = "expected_aspect_ratio" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            supports_audio = "require_audio" in signature.parameters or any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            kwargs: dict[str, object] = {}
            if supports_ratio and expected_ratio is not None:
                kwargs["expected_aspect_ratio"] = expected_ratio
            if require_audio and supports_audio:
                kwargs["require_audio"] = True
            value = validate(path, **kwargs)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("未通过真实视频校验，验证器执行失败；候选文件已保留") from None
        try:
            if isinstance(value, Mapping):
                report = dict(value)
            elif is_dataclass(value) and not isinstance(value, type):
                report = asdict(value)
            elif hasattr(value, "ok") and hasattr(value, "errors"):
                report = dict(vars(value)) if hasattr(value, "__dict__") else {}
                report.setdefault("ok", getattr(value, "ok"))
                report.setdefault("errors", getattr(value, "errors"))
            else:
                raise WorkflowError("等待真实视频校验：验证器返回结构未知，候选文件已保留")
            ok = report.get("ok")
            errors = report.get("errors")
            if ok is not True or not isinstance(errors, (list, tuple)) or errors:
                raise WorkflowError("未通过真实视频校验；候选文件已保留，可在校验器可用后续查")
            if report.get("watermark_detected") is True:
                raise WorkflowError("下载视频检测到右下角可灵 AI 水印；候选文件已保留，禁止登记完成")
            if (
                report.get("watermark_verification") != "verified_no_watermark"
                and watermark_verified is not True
            ):
                raise WorkflowError(
                    "水印验证状态未知或验证器不支持；人工复核待处理，候选文件已保留"
                )
            if any(not isinstance(error, str) for error in errors):
                raise WorkflowError("等待真实视频校验：错误列表结构无效，候选文件已保留")
            _assert_no_sensitive_content(report)
            if not self._report_is_portrait(report):
                raise WorkflowError("未通过真实视频校验：必须提供真实宽高且满足 width < height；候选文件已隔离保留")
            if require_audio:
                if supports_audio and "audio_ok" in report and report.get("audio_ok") is not True:
                    raise WorkflowError("未通过真实视频校验：成片必须有与视频时长一致的音轨；候选文件已保留")
            safe_report = _json_safe_value(report)
            if not isinstance(safe_report, dict):
                raise WorkflowError("等待真实视频校验：校验报告结构无效，候选文件已保留")
            safe_report["watermark_verification"] = "verified_no_watermark"
            return safe_report
        except WorkflowError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise WorkflowError("等待真实视频校验：校验报告无法安全保存，候选文件已保留") from None

    def _query_video_external(
        self, run: RunRecord, external_id: str, *, unknown_outcome: bool = False
    ) -> RemoteVideo:
        try:
            query_by_external = getattr(self._client(), "query_video_by_external_id", None)
            if callable(query_by_external):
                remote = query_by_external(external_id)
            else:
                remote = self._client().query_video(external_task_id=external_id)
        except Exception as exc:
            if self._is_not_found(exc):
                raise WorkflowError(
                    "可灵未找到该 external_task_id；为避免重复付费，请人工处理，不会再次提交"
                ) from None
            self._record_processing_step(
                run=run,
                role="query",
                reason="查询本地外部任务编号状态",
                result="查询失败",
                evidence="当前款号任务记录",
                exception="可灵查询未完成",
            )
            raise WorkflowError("无法安全查询可灵视频任务；保留 external_task_id 供后续续查") from None
        result = self._as_video(remote)
        if result.external_task_id != external_id:
            raise WorkflowError("可灵视频查询编号与本地 external_task_id 不一致，已停止恢复")
        if type(result.watermark_enabled) is bool:
            metadata = self._read_metadata(run.run_dir)
            metadata["server_response_proof"] = {
                "source": "authenticated_client_query",
                "task_id": result.task_id or run.video_task_id,
                "external_task_id": result.external_task_id,
                "watermark_enabled": result.watermark_enabled,
                "recorded_at": self._now_iso(),
            }
            self._write_metadata(run.run_dir, metadata)
        self._record_processing_step(
            run=run,
            role="query",
            reason="通过本地外部任务编号查询视频状态",
            result=result.status,
            evidence="任务编号已关联当前款号",
        )
        return result

    def _query_video_task(self, run: RunRecord, task_id: str) -> RemoteVideo:
        try:
            remote = self._client().query_video(task_id=task_id)
        except Exception:
            self._record_processing_step(
                run=run,
                role="query",
                reason="查询本地任务编号状态",
                result="查询失败",
                evidence="当前款号任务记录",
                exception="可灵查询未完成",
            )
            raise WorkflowError("无法安全查询可灵视频任务；保留本地状态供后续续查") from None
        result = self._as_video(remote)
        if type(result.watermark_enabled) is bool:
            metadata = self._read_metadata(run.run_dir)
            metadata["server_response_proof"] = {
                "source": "authenticated_client_query",
                "task_id": result.task_id,
                "external_task_id": result.external_task_id or run.video_external_id,
                "watermark_enabled": result.watermark_enabled,
                "recorded_at": self._now_iso(),
            }
            self._write_metadata(run.run_dir, metadata)
        self._record_processing_step(
            run=run,
            role="query",
            reason="通过本地任务编号查询视频状态",
            result=result.status,
            evidence="任务编号已关联当前款号",
        )
        return result

    def _record_remote_failure(
        self, run: RunRecord, remote: RemoteTask, stage: str
    ) -> RunRecord:
        metadata = self._read_metadata(run.run_dir)
        metadata.update(
            {
                "stage": f"{stage}_failed",
                "remote_error": self._safe_remote_fields(remote),
            }
        )
        self._write_metadata(run.run_dir, metadata)
        return self._update_run(run.run_dir, status=TaskStatus.FAILED)

    def _fail_safely(
        self,
        run: RunRecord,
        metadata: dict[str, object],
        stage: str,
        error: Exception,
    ) -> None:
        request_id = _safe_identifier(getattr(error, "request_id", None))
        metadata.update(
            {
                "stage": f"{stage}_failed",
                "remote_error": {
                    "error_type": type(error).__name__,
                    "request_id": request_id,
                },
            }
        )
        self._write_metadata(run.run_dir, metadata)
        self._update_run(run.run_dir, status=TaskStatus.FAILED)

    def _mark_timed_out(self, run: RunRecord, stage: str) -> None:
        metadata = self._read_metadata(run.run_dir)
        metadata.update({"stage": f"{stage}_timed_out"})
        self._write_metadata(run.run_dir, metadata)
        self._update_run(run.run_dir, status=TaskStatus.TIMED_OUT)

    def _cache_element(
        self, run: RunRecord, metadata: dict[str, object], element: RemoteElement
    ) -> None:
        fingerprint = metadata.get("element_fingerprint")
        task_id = self._safe_remote_identifier(element.task_id)
        element_id = element.element_id
        if (
            not isinstance(fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
            or task_id is None
            or not self._valid_element_id(element_id)
        ):
            raise WorkflowError("无法安全保存已确认的服装元素缓存")
        cache_path = self.paths.results / _CACHE_NAME
        self._check_output_parent(self.paths.results)
        with _locked(self.paths.results / ".element-cache.lock"):
            current = self._read_cache_document(cache_path)
            entries = current.setdefault(fingerprint, [])
            if not isinstance(entries, list):
                raise WorkflowError("元素缓存结构冲突，已保留原文件")
            candidate = {"element_id": element_id, "task_id": task_id}
            if candidate in entries:
                return
            entries.append(candidate)
            encoded = _json_bytes(current)
            if cache_path.exists():
                self._atomic_replace(cache_path, encoded)
            else:
                self._write_exclusive(cache_path, encoded)

    def _read_cache(self, fingerprint: str) -> list[dict[str, object]]:
        document = self._read_cache_document(self.paths.results / _CACHE_NAME)
        if not document:
            return []
        raw = document.get(fingerprint, [])
        if not isinstance(raw, list):
            raise WorkflowError("元素缓存结构无效；为避免重复付费，已停止")
        entries: list[dict[str, object]] = []
        for item in raw:
            if (
                not isinstance(item, Mapping)
                or not self._valid_element_id(item.get("element_id"))
                or self._safe_remote_identifier(item.get("task_id")) is None
            ):
                raise WorkflowError("元素缓存条目无效；为避免重复付费，已停止")
            entries.append(
                {"element_id": item["element_id"], "task_id": str(item["task_id"])}
            )
        return entries

    def _read_cache_document(self, path: Path) -> dict[str, object]:
        _reject_reparse_components(path)
        if not path.exists():
            return {}
        self._assert_regular_file(path)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkflowError("元素缓存无法安全读取；为避免覆盖原内容，已停止") from None
        if not isinstance(document, dict):
            raise WorkflowError("元素缓存结构无效；为避免覆盖原内容，已停止")
        for key, value in document.items():
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
                raise WorkflowError("元素缓存键无效；为避免覆盖原内容，已停止")
            if not isinstance(value, list):
                raise WorkflowError("元素缓存条目无效；为避免覆盖原内容，已停止")
            for item in value:
                if (
                    not isinstance(item, Mapping)
                    or set(item) != {"element_id", "task_id"}
                    or not self._valid_element_id(item.get("element_id"))
                    or self._safe_remote_identifier(item.get("task_id")) is None
                ):
                    raise WorkflowError("元素缓存条目无效；为避免覆盖原内容，已停止")
        return document

    def _read_credentials_if_configured(self) -> None:
        if self.credential_store is None:
            return
        read = getattr(self.credential_store, "read", None)
        if not callable(read):
            raise WorkflowError("凭据读取器配置无效")
        try:
            values = read()
        except Exception as exc:
            raise WorkflowError(f"可灵凭据不可用（{type(exc).__name__}）") from None
        finally:
            if "values" in locals():
                if isinstance(values, str) and values:
                    self._sensitive_values.add(values)
                elif isinstance(values, (tuple, list)):
                    self._sensitive_values.update(
                        item for item in values if isinstance(item, str) and item
                    )
                del values

    def _prepare_current_assets(self, run: RunRecord, metadata: dict[str, object]) -> PreparedAssets:
        if metadata.get("source_layout") == "gemini":
            source = self.paths.materials / run.sku
            self._require_safe_source_directory(source, "款号素材")
            try:
                assets = self._scan_current_project_materials(run.sku, metadata).images
                prepared = prepare_api_images(assets, run.run_dir)
            except Exception as exc:
                raise WorkflowError(_safe_text(str(exc)) or "无法核对本次款号素材") from None
        else:
            source = self.paths.root / "02款号参考图" / run.sku
            self._require_safe_source_directory(source, "款号参考图")
            try:
                assets = scan_sku_images(self.paths, run.sku)
                prepared = prepare_api_images(assets, run.run_dir)
            except Exception as exc:
                raise WorkflowError(_safe_text(str(exc)) or "无法核对本次款号素材") from None
        self._check_source_children(source)
        expected = metadata.get("element_fingerprint")
        actual = element_fingerprint(prepared)
        if not isinstance(expected, str) or not hmac.compare_digest(expected, actual):
            raise WorkflowError("款号参考图自分析后发生变化，请重新离线分析并确认")
        return prepared

    def _scan_current_project_materials(
        self, sku: str, metadata: Mapping[str, object]
    ):
        manifest_value = metadata.get("reference_group_manifest")
        group_id = metadata.get("reference_group_id")
        if manifest_value is None and group_id is None:
            return scan_project_materials(self.paths, sku)
        if not isinstance(manifest_value, str) or not isinstance(group_id, str):
            raise WorkflowError("参考组身份记录缺失，请重新离线分析并确认")
        expected_group_sha = metadata.get("reference_group_sha256")
        if not isinstance(expected_group_sha, str) or not re.fullmatch(r"[0-9a-f]{64}", expected_group_sha):
            raise WorkflowError("参考组 SHA-256 记录缺失，请重新离线分析并确认")
        manifest_path = Path(manifest_value)
        record, actual_group_sha = self._select_reference_group(manifest_path, sku, None)
        if record.get("group_id") != group_id or not hmac.compare_digest(
            actual_group_sha, expected_group_sha
        ):
            raise WorkflowError("参考组清单在提示词确认后发生变化，请重新离线分析并确认")
        materials = scan_project_materials(
            self.paths,
            sku,
            reference_files=record["reference_files"],
        )
        verified_record, verified_group_sha = self._select_reference_group(
            manifest_path, sku, materials
        )
        if verified_record.get("group_id") != group_id or not hmac.compare_digest(
            verified_group_sha, expected_group_sha
        ):
            raise WorkflowError("参考组清单在素材校验期间发生变化，请重新离线分析并确认")
        return materials

    def _verify_reference_video_sources(
        self, metadata: Mapping[str, object], run_dir: Path | None = None
    ) -> None:
        if metadata.get("source_layout") == "gemini":
            try:
                materials = self._scan_current_project_materials(
                    str(metadata.get("sku", "")), metadata
                )
            except Exception as exc:
                raise WorkflowError(_safe_text(str(exc)) or "无法核对款号素材映射") from None
            if (
                materials.request_text != metadata.get("project_request_text")
                or materials.review_standard != metadata.get("review_standard")
            ):
                raise WorkflowError("需求或审核标准在提示词确认后发生变化，请重新离线分析")
            if run_dir is not None:
                self._verify_scene_image_manifest(
                    metadata, materials, run_dir=run_dir
                )
            self._verify_modification_source(metadata, materials.folder)
            manifest = metadata.get("video_manifest")
            if not isinstance(manifest, Mapping):
                raise WorkflowError("参考视频分析记录缺失，请重新离线分析并确认")
            selected_videos = [*materials.action_videos, *materials.scene_videos]
            action_rows = [
                row for row in manifest.get("videos", [])
                if isinstance(row, Mapping) and row.get("role", "action_reference") == "action_reference"
            ] if isinstance(manifest.get("videos"), list) else []
            if not materials.action_videos:
                rows = manifest.get("videos")
                if rows not in ([], None):
                    raise WorkflowError("动作参考视频集合发生变化，请重新离线分析并确认")
                return
            snapshot = self._snapshot_reference_videos(
                materials.folder, selected_videos
            )
            self._verify_video_manifest(manifest, snapshot, materials.folder)
            return
        source = self.paths.root / "02款号参考图" / str(metadata.get("sku", ""))
        manifest = metadata.get("video_manifest")
        if not isinstance(manifest, Mapping):
            raise WorkflowError("参考视频分析记录缺失，请重新离线分析并确认")
        if not source.is_dir():
            raise WorkflowError("当前款号参考目录缺失；拒绝读取共享效果参考视频目录")
        rows = manifest.get("videos")
        if not isinstance(rows, list) or not rows:
            raise WorkflowError("显式绑定的当前款号动作视频清单缺失")
        bound: list[Path] = []
        for row in rows:
            if not isinstance(row, Mapping) or row.get("role") != "action_reference":
                raise WorkflowError("旧目录布局只接受显式绑定的当前款号动作视频")
            path = Path(str(row.get("path", "")))
            if path.is_absolute():
                try:
                    path = path.resolve(strict=True)
                    path.relative_to(source.resolve(strict=True))
                except (OSError, ValueError):
                    raise WorkflowError("动作参考视频超出当前款号目录") from None
            else:
                if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
                    raise WorkflowError("动作参考视频相对路径无效")
                path = source / path
            bound.append(path)
        if {path.resolve() for path in self._reference_videos(source)} != {
            path.resolve() for path in bound
        }:
            raise WorkflowError("当前款号显式绑定的视频集合发生变化，请重新离线分析")
        snapshot = self._snapshot_reference_videos(source, bound)
        self._verify_video_manifest(manifest, snapshot, source)

    def _load_modification_source(self, sku: str, version: str) -> dict[str, object]:
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise WorkflowError("修改源版本必须是 v001 或更高版本")
        number = int(version[1:])
        if number < 1 or version != f"v{number:03d}":
            raise WorkflowError("修改源版本必须是 v001 或更高版本")
        history_run = self._version_run_dir(sku, version)
        try:
            history_run = self._assert_safe_run_dir(history_run, sku)
            source_run = self._load_run(history_run)
        except WorkflowError:
            raise WorkflowError("修改源版本必须存在于历史版本记录中") from None
        if source_run.sku != sku:
            raise WorkflowError("修改源历史记录与款号不匹配")
        if source_run.status is not TaskStatus.COMPLETED:
            raise WorkflowError("修改源版本必须是已完成并处于待审核的版本")
        approval_record = history_run / "approval.json"
        approved_video = (
            self.paths.approved / sku / f"{sku}_{version}.mp4"
            if self._uses_flat_version_paths(history_run)
            else self.paths.approved / sku / version / f"{sku}_{version}.mp4"
        )
        if approval_record.exists() or approved_video.exists():
            raise WorkflowError("已审核通过版本不能作为待审核修改源")

        source_video = (
            self.paths.pending / sku / f"{version}.mp4"
            if self._uses_flat_version_paths(history_run)
            else self.paths.pending / sku / version / f"{sku}_{version}.mp4"
        )
        try:
            _reject_reparse_components(source_video)
            self._require_within(source_video, self.paths.pending)
            self._assert_regular_file(source_video)
            if source_video.stat().st_size <= 0:
                raise WorkflowError("修改源待审核视频不能为空")
            video_digest = _sha256_file_stable(source_video)
        except (OSError, WorkflowError):
            raise WorkflowError("修改源版本缺少有效的待审核视频") from None

        feedback_path = self.paths.materials / sku / "修改反馈.txt"
        try:
            _reject_reparse_components(feedback_path)
            self._require_within(feedback_path, self.paths.materials / sku)
            self._assert_regular_file(feedback_path)
            before = feedback_path.stat()
            feedback = feedback_path.read_text(encoding="utf-8").strip()
            after = feedback_path.stat()
        except (OSError, UnicodeError, WorkflowError):
            raise WorkflowError("缺少有效的修改反馈.txt") from None
        if _stat_identity(before) != _stat_identity(after):
            raise WorkflowError("修改反馈读取期间发生变化，请重新分析")
        if not feedback:
            raise WorkflowError("修改反馈.txt 不能为空")
        feedback_digest = hashlib.sha256(feedback.encode("utf-8")).hexdigest()
        return {
            "from_version": version,
            "modification_feedback": feedback,
            "source_video_sha256": video_digest,
            "source_feedback_sha256": feedback_digest,
        }

    def _verify_modification_source(
        self, metadata: Mapping[str, object], sku_folder: Path
    ) -> None:
        modification_keys = {
            "from_version",
            "modification_feedback",
            "source_video_sha256",
            "source_feedback_sha256",
        }
        present = modification_keys & set(metadata)
        if not present:
            return
        if present != modification_keys:
            raise WorkflowError("修改源记录不完整，已在付费提交前停止")
        sku = metadata.get("sku")
        version = metadata.get("from_version")
        if not isinstance(sku, str) or not isinstance(version, str):
            raise WorkflowError("修改源记录无效，已在付费提交前停止")
        expected = self._load_modification_source(sku, version)
        for key in modification_keys:
            if metadata.get(key) != expected.get(key):
                raise WorkflowError("修改源视频或反馈在提示词确认后发生变化，请重新分析")
        feedback_path = sku_folder / "修改反馈.txt"
        try:
            current_feedback = feedback_path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            raise WorkflowError("修改反馈在提示词确认后不可读取，请重新分析") from None
        if current_feedback != metadata.get("modification_feedback"):
            raise WorkflowError("修改反馈在提示词确认后发生变化，请重新分析")

    def _snapshot_reference_videos(
        self, directory: Path, source_paths: Sequence[Path] | None = None
    ) -> list[dict[str, str]]:
        self._require_safe_source_directory(directory, "效果参考视频")
        self._check_source_children(directory)
        paths = list(source_paths) if source_paths is not None else self._reference_videos(directory)
        snapshot: list[dict[str, str]] = []
        for path in paths:
            self._assert_regular_file(path)
            self._require_within(path, directory)
            try:
                before = path.stat()
                digest = _sha256_file_stable(path)
                after = path.stat()
            except (OSError, WorkflowError):
                raise WorkflowError("参考视频不可读取或读取期间发生变化，请重新离线分析并确认") from None
            if _stat_identity(before) != _stat_identity(after):
                raise WorkflowError("参考视频在校验期间发生变化，请重新离线分析并确认")
            snapshot.append(
                {"name": path.relative_to(directory).as_posix(), "sha256": digest}
            )
        if not snapshot:
            raise WorkflowError("参考视频集合为空，请重新离线分析并确认")
        return snapshot

    def _verify_video_manifest(
        self,
        manifest: Mapping[str, object],
        current: Sequence[Mapping[str, str]],
        source_dir: Path,
    ) -> None:
        rows = manifest.get("videos")
        if manifest.get("status") != "ready" or not isinstance(rows, list):
            raise WorkflowError("参考视频未全部成功分析，请重新离线分析并确认")
        expected: list[dict[str, str]] = []
        resolved_root = source_dir.resolve(strict=True)
        for row in rows:
            if not isinstance(row, Mapping) or row.get("status") != "success":
                raise WorkflowError("参考视频分析记录不完整，请重新离线分析并确认")
            saved_path = row.get("path")
            digest = row.get("sha256")
            if not isinstance(saved_path, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise WorkflowError("参考视频分析记录缺少文件名或 SHA-256，请重新离线分析并确认")
            path = Path(saved_path)
            if path.is_absolute():
                try:
                    name = path.resolve(strict=False).relative_to(resolved_root).as_posix()
                except OSError:
                    raise WorkflowError("参考视频分析路径无效，请重新离线分析并确认") from None
                except ValueError:
                    raise WorkflowError("参考视频分析路径不属于原始视频目录") from None
            else:
                if not path.parts or any(part in {"", ".", ".."} for part in path.parts):
                    raise WorkflowError("参考视频分析路径无效，请重新离线分析并确认")
                name = path.as_posix()
            expected.append({"name": name, "sha256": digest})
        expected.sort(key=lambda item: (item["name"].casefold(), item["name"]))
        observed = [dict(item) for item in current]
        if expected != observed:
            raise WorkflowError("阶段 A 后参考视频新增、删除、改名或内容变化，请重新离线分析并确认")

    def _verify_saved_prompt(self, run: RunRecord, metadata: dict[str, object]) -> None:
        if metadata.get("sku") != run.sku or metadata.get("stage") != "waiting_approval":
            raise ApprovalError("任务不是完整的待确认记录")
        prompt = self._render_saved_prompt(run, metadata)
        expected_sha = prompt_sha256(prompt)
        actual_file = run.run_dir / _PROMPT_NAME
        if not _same_file_bytes(actual_file, prompt.encode("utf-8")):
            raise ApprovalError("本地提示词文件与保存版本不一致，请重新确认")
        final_file = run.run_dir / _FINAL_PROMPT_NAME
        if not _same_file_bytes(final_file, prompt.encode("utf-8")):
            raise ApprovalError("本地最终提交提示词与审核版本不一致，请重新确认")
        if not isinstance(run.prompt_sha256, str) or not hmac.compare_digest(
            run.prompt_sha256, expected_sha
        ):
            raise ApprovalError("提示词版本不一致，请重新显示并确认")
        saved_sha = metadata.get("prompt_sha256")
        if not isinstance(saved_sha, str) or not hmac.compare_digest(saved_sha, expected_sha):
            raise ApprovalError("本地提示词 SHA 与确认记录不一致，请重新分析并确认")

    def _render_saved_prompt(self, run: RunRecord, metadata: dict[str, object]) -> str:
        analysis = metadata.get("analysis")
        if not isinstance(analysis, Mapping):
            raise WorkflowError("待确认任务缺少结构化动作分析")
        shots = self._saved_shots(metadata)
        try:
            return self._render_prompt_document(
                metadata,
                shots,
                duration=_shot_total_duration(shots),
                sku=run.sku,
                analysis=analysis,
                source_dimensions=self._source_dimensions(metadata),
                reference_duration_seconds=self._reference_duration(metadata),
                modification_feedback=(
                    metadata.get("modification_feedback")
                    if isinstance(metadata.get("modification_feedback"), str)
                    else None
                ),
                audio_policy=str(metadata.get("audio_policy", "generate_music")),
                scene_image_manifest=self._load_scene_image_manifest(run.run_dir, metadata),
                reference_mapping=self._reference_mapping(metadata),
            )
        except (PromptError, TypeError, ValueError) as exc:
            raise WorkflowError(_safe_text(str(exc)) or "待确认分镜结构无效") from None

    def _saved_shots(self, metadata: Mapping[str, object]) -> list[ShotPlan]:
        rows = metadata.get("shots")
        if not isinstance(rows, list) or not rows:
            raise WorkflowError("待确认任务缺少分镜内容")
        try:
            shots = [
                ShotPlan(index=row["index"], duration=row["duration"], prompt=row["prompt"])
                for row in rows
                if isinstance(row, Mapping)
            ]
        except (KeyError, TypeError, ValueError):
            raise WorkflowError("待确认分镜记录无效") from None
        if len(shots) != len(rows):
            raise WorkflowError("待确认分镜记录无效")
        return shots

    def _prepared_payload(
        self, prepared: PreparedAssets, manifest: AnalysisManifest
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "sku": prepared.sku,
            "element_fingerprint": element_fingerprint(prepared),
            "prepared": self._prepared_record(prepared),
            "video_manifest": manifest.to_dict(),
        }

    @staticmethod
    def _video_manifest_with_roles(
        manifest: AnalysisManifest,
        action_paths: Sequence[Path],
        scene_paths: Sequence[Path],
    ) -> dict[str, object]:
        roles = {path.resolve(): "action_reference" for path in action_paths}
        roles.update({path.resolve(): "scene_reference" for path in scene_paths})
        value = manifest.to_dict()
        rows = value.get("videos")
        if not isinstance(rows, list):
            raise WorkflowError("参考视频清单结构无效")
        for row in rows:
            if not isinstance(row, dict):
                raise WorkflowError("参考视频清单行无效")
            try:
                path = Path(str(row.get("path"))).resolve(strict=True)
            except OSError:
                raise WorkflowError("参考视频清单路径无效") from None
            role = roles.get(path)
            if role is None:
                raise WorkflowError("参考视频未绑定到当前款号的动作或场景角色")
            row["role"] = role
        return value

    def _select_reference_group(
        self, manifest_path: Path, sku: str, materials: object
    ) -> tuple[dict[str, object], str]:
        """Validate one imported group and bind analysis to exactly its references."""
        if not manifest_path.is_absolute():
            raise WorkflowError("参考组清单必须使用绝对本地路径")
        group_root = self.paths.history / sku
        _reject_reparse_components(group_root)
        try:
            root_real = group_root.resolve(strict=True)
            path_real = manifest_path.resolve(strict=True)
            path_real.relative_to(root_real)
            self._assert_regular_file(path_real)
            if path_real.stat().st_size > 1024 * 1024:
                raise WorkflowError("参考组清单超过本地读取上限")
            raw = path_real.read_bytes()
            record = json.loads(raw.decode("utf-8"))
        except WorkflowError:
            raise
        except (OSError, UnicodeError, ValueError, TypeError):
            raise WorkflowError("无法安全读取参考组清单") from None
        if not isinstance(record, dict) or record.get("sku") != sku:
            raise WorkflowError("参考组清单款号与当前任务不匹配")
        group_id = record.get("group_id")
        if not isinstance(group_id, str) or not re.fullmatch(r"group-\d{4,}", group_id):
            raise WorkflowError("参考组清单编号无效")
        reference_files = record.get("reference_files")
        expected_roles = (
            "主参考图",
            "服装细节参考",
            "场景参考",
            "动作参考图",
            "动作参考视频",
        )
        legacy_roles = set(expected_roles) - {"动作参考图"}
        if not isinstance(reference_files, Mapping) or frozenset(reference_files) not in {
            frozenset(expected_roles),
            frozenset(legacy_roles),
        }:
            raise WorkflowError("参考组清单缺少完整素材角色映射")
        normalized: dict[str, list[str]] = {}
        for role in expected_roles:
            values = reference_files.get(role, [])
            if not isinstance(values, list) or any(
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or PureWindowsPath(name).name != name
                for name in values
            ):
                raise WorkflowError("参考组素材映射必须是款号目录内的文件名")
            normalized[role] = list(values)
        if len(normalized["主参考图"]) != 1 or not normalized["动作参考视频"]:
            raise WorkflowError("参考组必须包含一张主图和至少一段动作参考视频")

        rows = record.get("files")
        if not isinstance(rows, list):
            raise WorkflowError("参考组缺少素材 SHA-256 记录")
        expected_digests: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise WorkflowError("参考组素材 SHA-256 记录无效")
            name = row.get("target_name")
            digest = row.get("sha256")
            if (
                not isinstance(name, str)
                or Path(name).name != name
                or PureWindowsPath(name).name != name
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise WorkflowError("参考组素材 SHA-256 记录无效")
            expected_digests[name] = digest

        images = getattr(materials, "images", ())
        action_reference_images = getattr(materials, "action_reference_images", ())
        videos = getattr(materials, "action_videos", ())
        scene_videos = getattr(materials, "scene_videos", ())
        image_by_name = {image.path.name: image for image in images}
        action_image_by_name = {image.path.name: image for image in action_reference_images}
        video_by_name = {path.name: path for path in (*videos, *scene_videos)}
        scene_video_by_name = {path.name: path for path in scene_videos}
        expected_roles_by_name: dict[str, str] = {}
        for role in ("主参考图", "服装细节参考", "场景参考", "动作参考图"):
            for name in normalized[role]:
                if name in expected_roles_by_name:
                    raise WorkflowError("参考组中同一素材被重复映射")
                expected_roles_by_name[name] = role
        for name in normalized["动作参考视频"]:
            if name in expected_roles_by_name:
                raise WorkflowError("参考组中同一素材被重复映射")
            expected_roles_by_name[name] = "动作参考视频"

        for name, role in expected_roles_by_name.items():
            if materials is None:
                path = self.paths.materials / sku / name
                _reject_reparse_components(path)
                self._require_within(path, self.paths.materials / sku)
                self._assert_regular_file(path)
            elif role == "动作参考视频":
                path = video_by_name.get(name)
                if path is None:
                    raise WorkflowError(f"参考组动作视频不在正式映射中：{name}")
            elif role == "动作参考图":
                image = action_image_by_name.get(name)
                if image is None:
                    raise WorkflowError(f"参考组动作参考图不在正式映射中：{name}")
                path = image.path
            elif role == "场景参考" and name in scene_video_by_name:
                path = scene_video_by_name[name]
            else:
                image = image_by_name.get(name)
                if image is None:
                    raise WorkflowError(f"参考组图片不在正式映射中：{name}")
                if role != image.role:
                    raise WorkflowError(f"参考组素材角色与需求映射不一致：{name}")
                path = image.path
            expected_digest = expected_digests.get(name)
            if expected_digest is None or _sha256_file_stable(path) != expected_digest:
                raise WorkflowError(f"参考组素材 SHA-256 不匹配：{name}")
        return {**record, "reference_files": normalized}, hashlib.sha256(raw).hexdigest()

    def _prepared_record(self, prepared: PreparedAssets) -> dict[str, object]:
        def item(image):
            return {
                "source": self._relative_project_path(image.source),
                "api_path": self._relative_run_path(image.api_path, image.api_path.parents[1]),
                "role": image.role,
                "source_sha256": image.source_sha256,
                "derived_sha256": image.derived_sha256,
                "width": image.width,
                "height": image.height,
            }

        return {
            "frontal": item(prepared.frontal),
            "references": [item(image) for image in prepared.references],
            "first_frame": prepared.first_frame.name,
        }

    @staticmethod
    def _source_dimensions(metadata: Mapping[str, object]) -> tuple[int, int] | None:
        prepared = metadata.get("prepared")
        frontal = prepared.get("frontal") if isinstance(prepared, Mapping) else None
        if not isinstance(frontal, Mapping):
            return None
        width, height = frontal.get("width"), frontal.get("height")
        if (
            isinstance(width, int)
            and not isinstance(width, bool)
            and width > 0
            and isinstance(height, int)
            and not isinstance(height, bool)
            and height > 0
        ):
            return width, height
        return None

    @staticmethod
    def _reference_mapping(metadata: Mapping[str, object]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}

        def _add(role_name: str, file_name: str) -> None:
            """One file name per role, first occurrence wins.

            The same material can arrive through material_roles,
            prepared.references and video_manifest; the paid prompt must not
            list one file twice under a single role.
            """
            bucket = result.setdefault(role_name, [])
            if file_name not in bucket:
                bucket.append(file_name)

        roles = metadata.get("material_roles")
        if isinstance(roles, Mapping):
            for role, names in roles.items():
                if not isinstance(role, str) or not isinstance(names, (list, tuple)):
                    continue
                for name in names:
                    if isinstance(name, str) and name and Path(name).name == name:
                        _add(role, name)

        prepared = metadata.get("prepared")
        if "主参考图" not in result and isinstance(prepared, Mapping):
            frontal = prepared.get("frontal")
            source = frontal.get("source") if isinstance(frontal, Mapping) else None
            if isinstance(source, str) and source:
                _add("主参考图", Path(source).name)
            references = prepared.get("references")
            if isinstance(references, list):
                for image in references:
                    if not isinstance(image, Mapping):
                        continue
                    role = image.get("role")
                    source = image.get("source")
                    if isinstance(role, str) and isinstance(source, str) and source:
                        _add(role, Path(source).name)

        manifest = metadata.get("video_manifest")
        rows = manifest.get("videos") if isinstance(manifest, Mapping) else None
        if isinstance(rows, list):
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                path = row.get("path")
                role = row.get("role")
                if not isinstance(path, str) or not path:
                    continue
                mapped_role = (
                    "动作参考视频" if role == "action_reference"
                    else "场景参考视频" if role == "scene_reference"
                    else None
                )
                if mapped_role:
                    _add(mapped_role, Path(path).name)
        return result

    def _source_aspect_ratio(self, run_dir: Path) -> float | None:
        try:
            metadata = self._read_metadata(run_dir)
        except Exception:
            return None
        dimensions = self._source_dimensions(metadata)
        return dimensions[0] / dimensions[1] if dimensions else None

    @staticmethod
    def _reference_audio_policy(metadata: Mapping[str, object]) -> str:
        """Choose original-track retention or music generation from action evidence."""
        manifest = metadata.get("video_manifest")
        videos = manifest.get("videos") if isinstance(manifest, Mapping) else None
        if not isinstance(videos, list):
            raise WorkflowError("参考视频音频证据缺失，无法确定音频策略")
        action_rows = [
            row for row in videos
            if isinstance(row, Mapping) and row.get("role", "action_reference") == "action_reference"
        ]
        if not action_rows:
            # Preparation-only projects without a reference video remain
            # compatible; they will be stopped later by the normal action
            # reference/shot validation gate before paid submission.
            return "generate_music"
        statuses = {row.get("audio_status") for row in action_rows}
        statuses.discard(None)
        if not statuses:
            # Compatibility for injected analyzers and older cached manifests:
            # missing audio evidence is treated as silent and requests music.
            return "generate_music"
        if "unknown" in statuses:
            raise WorkflowError("动作参考视频音轨无法确定，禁止提交生成")
        if statuses - {"present", "absent"}:
            raise WorkflowError("动作参考视频音轨状态无效，禁止提交生成")
        if len(statuses) > 1:
            raise WorkflowError("多个动作参考视频音轨状态冲突，需先明确单一参考")
        if statuses == {"absent"}:
            return "generate_music"
        # A ``soun`` track can still be digitally silent: the silent
        # YF95402010 reference carried a complete mp4a track that decodes to
        # -91 dB.  A silent reference is never rejected here; it switches the
        # saved prompt to the music-only policy instead.
        for row in action_rows:
            if Workflow._row_audio_is_silent(row):
                return "generate_music"
        return "preserve_original"

    @staticmethod
    def _row_audio_is_silent(row: Mapping[str, object]) -> bool:
        """Return True only with positive evidence that the track is silent."""
        flag = row.get("audio_silent")
        if isinstance(flag, bool):
            return flag
        path_value = row.get("absolute_path") or row.get("path")
        if isinstance(path_value, str) and path_value:
            try:
                measurement = measure_audio(path_value)
            except (OSError, ValueError, TypeError):
                return False
            if measurement.silent is not None:
                return measurement.silent
        return False

    @staticmethod
    def _reference_duration(metadata: Mapping[str, object]) -> float | None:
        manifest = metadata.get("video_manifest")
        videos = manifest.get("videos") if isinstance(manifest, Mapping) else None
        if not isinstance(videos, list):
            return None
        durations = [
            float(video["duration_seconds"])
            for video in videos
            if isinstance(video, Mapping)
            and video.get("role", "action_reference") == "action_reference"
            and isinstance(video.get("duration_seconds"), (int, float))
            and not isinstance(video.get("duration_seconds"), bool)
            and math.isfinite(float(video["duration_seconds"]))
            and video["duration_seconds"] > 0
        ]
        if not durations:
            return None
        if any(
            Decimal(str(duration)) != Decimal(str(durations[0]))
            for duration in durations[1:]
        ):
            raise WorkflowError(
                "多个动作参考视频时长冲突，当前版本进入待处理；请先明确选择单一动作参考"
            )
        override = metadata.get("reference_duration_override")
        if override is None:
            return durations[0]
        if not isinstance(override, Mapping):
            raise WorkflowError("时长例外记录结构无效")
        if override.get("user_authorized") is not True:
            raise WorkflowError("时长例外必须有当前用户授权")
        source_seconds = override.get("source_seconds")
        target_seconds = override.get("target_seconds")
        if (
            not isinstance(source_seconds, (int, float))
            or isinstance(source_seconds, bool)
            or not math.isfinite(float(source_seconds))
            or not isinstance(target_seconds, (int, float))
            or isinstance(target_seconds, bool)
            or not math.isfinite(float(target_seconds))
            or target_seconds <= 0
        ):
            raise WorkflowError("时长例外记录与动作参考证据不匹配")
        evidence_duration = Decimal(str(durations[0]))
        if (
            Decimal(str(source_seconds)) != evidence_duration
            or Decimal(str(target_seconds)) != evidence_duration
        ):
            raise WorkflowError("时长例外不得改变动作参考的真实时长")
        return durations[0]

    @staticmethod
    def _report_matches_aspect_ratio(report: Mapping[str, object], expected: float) -> bool:
        width, height = report.get("width"), report.get("height")
        if (
            isinstance(width, (int, float))
            and not isinstance(width, bool)
            and isinstance(height, (int, float))
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        ):
            return abs(width / height - expected) / expected <= 0.01
        raw = report.get("aspect_ratio")
        if isinstance(raw, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*:\s*\d+(?:\.\d+)?\s*", raw):
            numerator, denominator = (float(part.strip()) for part in raw.split(":"))
            return denominator > 0 and abs(numerator / denominator - expected) / expected <= 0.01
        return False

    @staticmethod
    def _report_is_portrait(report: Mapping[str, object]) -> bool:
        width, height = report.get("width"), report.get("height")
        if (
            isinstance(width, (int, float))
            and not isinstance(width, bool)
            and isinstance(height, (int, float))
            and not isinstance(height, bool)
            and width > 0
            and height > 0
        ):
            return width < height
        return False

    def _relative_project_path(self, path: Path) -> str:
        resolved_root = self.paths.root.resolve(strict=True)
        resolved_path = Path(path).resolve(strict=True)
        try:
            return resolved_path.relative_to(resolved_root).as_posix()
        except ValueError:
            raise WorkflowError("源素材路径超出项目目录") from None

    @staticmethod
    def _relative_run_path(path: Path, run_dir: Path) -> str:
        try:
            return path.resolve(strict=True).relative_to(run_dir.resolve(strict=True)).as_posix()
        except (OSError, ValueError):
            raise WorkflowError("衍生素材路径超出当前任务目录") from None

    def _render_analysis_markdown(self, analysis: Mapping[str, object]) -> str:
        return _render_analysis_markdown(analysis)

    def _reference_videos(self, directory: Path) -> list[Path]:
        try:
            paths = sorted(
                (
                    entry
                    for entry in directory.iterdir()
                    if entry.is_file()
                    and (
                        entry.suffix.lower() in {".mp4", ".mov"}
                        # Conventional suffixes keep the historical offline
                        # fixture path; arbitrary extensions are classified
                        # by their decodable content.
                        or detect_media_kind(entry) == "video"
                    )
                ),
                key=lambda path: (path.name.casefold(), path.name),
            )
        except OSError:
            raise WorkflowError("无法读取效果参考视频目录") from None
        for path in paths:
            _reject_reparse_components(path)
            if not path.is_file():
                raise WorkflowError("效果参考视频必须是普通本地文件")
            self._require_within(path, directory)
        return paths

    def _check_project_paths(self) -> None:
        _reject_reparse_components(self.paths.root)
        _reject_reparse_components(self.paths.results)
        try:
            resolved_root = self.paths.root.resolve(strict=True)
            resolved_results = self.paths.results.resolve(strict=False)
            resolved_results.relative_to(resolved_root)
        except (OSError, ValueError):
            raise WorkflowError("项目结果目录超出项目根目录或无法安全解析") from None

    def _check_output_parent(self, path: Path) -> None:
        _reject_reparse_components(path)
        try:
            root = self.paths.root.resolve(strict=True)
            resolved = path.resolve(strict=False)
            resolved.relative_to(root)
        except (OSError, ValueError):
            raise WorkflowError("输出路径超出项目根目录") from None

    def _require_safe_source_directory(self, path: Path, label: str) -> None:
        _reject_reparse_components(path)
        try:
            resolved_root = self.paths.root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
            resolved_path.relative_to(resolved_root)
        except (OSError, ValueError):
            raise WorkflowError(f"{label}目录不存在或超出项目根目录") from None
        if not path.is_dir():
            raise WorkflowError(f"{label}路径不是文件夹")

    def _check_source_children(self, directory: Path) -> None:
        _reject_reparse_components(directory)
        for child in directory.iterdir():
            _reject_reparse_components(child)
            try:
                resolved_child = child.resolve(strict=True)
                resolved_parent = directory.resolve(strict=True)
                resolved_child.relative_to(resolved_parent)
            except (OSError, ValueError):
                raise WorkflowError("源素材路径链接或逃逸出原始素材目录") from None
            if child.is_dir():
                for nested in child.iterdir():
                    _reject_reparse_components(nested)
                    try:
                        nested.resolve(strict=True).relative_to(resolved_parent)
                    except (OSError, ValueError):
                        raise WorkflowError("源素材路径链接或逃逸出原始素材目录") from None

    @staticmethod
    def _require_within(path: Path, root: Path) -> None:
        try:
            Path(path).resolve(strict=True).relative_to(Path(root).resolve(strict=True))
        except (OSError, ValueError):
            raise WorkflowError("源素材路径超出允许目录") from None

    def _find_unique_waiting_run(self, sku: str) -> Path:
        latest = self._find_latest_run(sku)
        if latest is None:
            raise ApprovalError("该款号没有等待确认的任务")
        if self._load_run(latest).status is not TaskStatus.WAITING_APPROVAL:
            raise ApprovalError("该任务不是等待确认状态")
        waiting: list[Path] = []
        sku_dir = self.paths.results_for(sku) / sku
        for candidate in self._run_directories(sku_dir):
            run = self._load_run(candidate)
            if run.status is TaskStatus.WAITING_APPROVAL:
                waiting.append(candidate)
        if len(waiting) != 1 or waiting[0] != latest:
            raise ApprovalError("该款号必须只有一份最新的等待确认任务")
        return latest

    def _find_waiting_run(self, sku: str, *, version: str | None = None) -> Path:
        """Resolve one waiting run, optionally by its explicit version.

        Batch generation must target the prepared version belonging to each
        numbered reference group.  The old unique-run rule remains the default
        for legacy callers, while an explicit version avoids cross-group
        ambiguity without weakening the waiting-state gate.
        """

        if version is None:
            return self._find_unique_waiting_run(sku)
        if not isinstance(version, str) or not version or Path(version).name != version:
            raise ApprovalError("指定任务版本无效")
        if re.fullmatch(r"v\d{3,}", version):
            number = int(version[1:])
            if version != f"v{number:03d}":
                raise ApprovalError("指定任务版本无效")
            run_dir = self._version_run_dir(sku, version)
        elif _RUN_NAME.fullmatch(version):
            # Legacy timestamp runs use their directory name as the version
            # argument in existing role-step callers.  Keep that explicit
            # targeting path while still resolving it beneath this SKU only.
            run_dir = self.paths.results_for(sku) / sku / version
        else:
            raise ApprovalError("指定任务版本无效")
        try:
            run_dir = self._assert_safe_run_dir(run_dir, sku)
            run = self._load_run(run_dir)
        except WorkflowError:
            raise ApprovalError("指定参考组没有可用的待确认任务") from None
        if run.status is not TaskStatus.WAITING_APPROVAL:
            raise ApprovalError("指定参考组任务不是等待确认状态")
        return run_dir

    def _find_latest_run(self, sku: str) -> Path | None:
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        self._check_project_paths()
        sku_dir = self.paths.results_for(sku) / sku
        _reject_reparse_components(sku_dir)
        if not sku_dir.exists():
            return None
        if not sku_dir.is_dir():
            raise WorkflowError("款号任务路径不是文件夹")
        runs = self._run_directories(sku_dir)
        return runs[-1] if runs else None

    def _run_directories(self, sku_dir: Path) -> list[Path]:
        try:
            candidates = [
                path
                for path in sku_dir.iterdir()
                if _RUN_NAME.fullmatch(path.name) and path.is_dir()
            ]
            workflow_dir = sku_dir / ".workflow"
            if workflow_dir.exists():
                _reject_reparse_components(workflow_dir)
                if not workflow_dir.is_dir():
                    raise WorkflowError("历史版本工作目录不是文件夹")
                candidates.extend(
                    path
                    for path in workflow_dir.iterdir()
                    if _RUN_NAME.fullmatch(path.name) and path.is_dir()
                )
            candidates.sort(key=self._run_sort_key)
        except OSError:
            raise WorkflowError("无法读取款号任务目录") from None
        result: list[Path] = []
        for path in candidates:
            self._assert_safe_run_dir(path, sku_dir.name)
            result.append(path)
        return result

    @staticmethod
    def _run_sort_key(path: Path) -> tuple[int, int, str]:
        version = re.fullmatch(r"v(\d{3,})", path.name)
        if version:
            return (1, int(version.group(1)), path.name)
        return (0, int(path.name.replace("-", "")), path.name)

    def _assert_safe_run_dir(self, run_dir: Path, sku: str | None = None) -> Path:
        path = Path(run_dir)
        if ".." in path.parts:
            raise WorkflowError("任务路径包含父目录跳转")
        _reject_reparse_components(path)
        inferred_sku = path.parent.parent.name if path.parent.name == ".workflow" else path.parent.name
        result_root = self.paths.results_for(sku or inferred_sku)
        _reject_reparse_components(result_root)
        try:
            results = result_root.resolve(strict=True)
            resolved = path.resolve(strict=True)
            relative = resolved.relative_to(results)
        except (OSError, ValueError):
            raise WorkflowError("任务路径超出项目 03生成结果 目录或不存在") from None
        valid_direct = len(relative.parts) == 2 and bool(_RUN_NAME.fullmatch(relative.parts[1]))
        valid_sidecar = (
            len(relative.parts) == 3
            and relative.parts[1] == ".workflow"
            and bool(_RUN_NAME.fullmatch(relative.parts[2]))
        )
        if not (valid_direct or valid_sidecar):
            raise WorkflowError("任务路径必须是款号下的时间戳或兼容版本侧车目录")
        if sku is not None and relative.parts[0] != sku:
            raise WorkflowError("任务路径与款号不匹配")
        self._assert_regular_file(_record_path(path))
        return path

    def _load_run(self, run_dir: Path) -> RunRecord:
        path = self._assert_safe_run_dir(run_dir)
        try:
            payload = json.loads(_record_path(path).read_text(encoding="utf-8"))
            record = self.store.load(path)
        except Exception:
            raise WorkflowError("任务记录损坏或无法读取；未执行网络操作") from None
        if not isinstance(payload, dict) or payload.get("run_dir") is None:
            raise WorkflowError("任务记录缺少本地运行目录")
        try:
            recorded_dir = Path(payload["run_dir"]).resolve(strict=True)
            current_dir = path.resolve(strict=True)
        except (OSError, TypeError, ValueError):
            raise WorkflowError("任务记录目录路径无效") from None
        if os.path.normcase(str(recorded_dir)) != os.path.normcase(str(current_dir)):
            raise WorkflowError("任务记录目录与当前任务不一致")
        if payload.get("sku") != record.sku or record.run_dir.resolve() != path.resolve():
            raise WorkflowError("任务记录款号或路径不一致")
        return record

    def _update_run(self, run_dir: Path, **changes: object) -> RunRecord:
        path = self._assert_safe_run_dir(run_dir)
        self._check_task_store_temp(path)
        try:
            return self.store.update(path, **changes)
        except Exception as exc:
            raise WorkflowError(f"任务状态无法安全保存（{type(exc).__name__}）") from None

    @staticmethod
    def _check_task_store_temp(run_dir: Path) -> None:
        record_path = _record_path(run_dir)
        temporary = record_path.with_suffix(record_path.suffix + ".tmp")
        _reject_reparse_components(temporary)
        if temporary.exists():
            raise WorkflowError("任务记录临时文件已存在；为避免覆盖，已停止")

    def _read_metadata(self, run_dir: Path) -> dict[str, object]:
        path = run_dir / _METADATA_NAME
        _reject_reparse_components(path)
        self._assert_regular_file(path)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise WorkflowError("工作流本地状态损坏或无法读取") from None
        if not isinstance(value, dict):
            raise WorkflowError("工作流本地状态结构无效")
        return value

    def _load_scene_image_manifest(
        self, run_dir: Path, metadata: Mapping[str, object]
    ) -> dict[str, object] | None:
        manifest_path_value = metadata.get("scene_image_manifest_path")
        if manifest_path_value is None:
            if any(
                key in metadata
                for key in (
                    "scene_image_manifest_version",
                    "scene_image_manifest_sha256",
                    "scene_image_reference_group_id",
                )
            ):
                raise WorkflowError("场景参考图分析绑定记录不完整")
            return None
        if manifest_path_value != "scene_image_manifest.json":
            raise WorkflowError("场景参考图清单路径无效")
        path = run_dir / manifest_path_value
        try:
            self._assert_regular_file(path)
            raw = path.read_bytes()
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError, WorkflowError):
            raise WorkflowError("当前任务场景参考图清单损坏或缺失") from None
        if not isinstance(value, dict):
            raise WorkflowError("当前任务场景参考图清单结构无效")
        expected_digest = metadata.get("scene_image_manifest_sha256")
        expected_group = metadata.get("scene_image_reference_group_id")
        if (
            not isinstance(expected_digest, str)
            or scene_image_manifest_sha256(value) != expected_digest
            or value.get("manifest_sha256") != expected_digest
            or value.get("reference_group_id") != expected_group
            or metadata.get("scene_image_manifest_version") != value.get("analyzer_version")
        ):
            raise WorkflowError("场景参考图清单摘要或参考组绑定不匹配")
        return value

    def _verify_scene_image_manifest(
        self,
        metadata: Mapping[str, object],
        materials: object,
        *,
        run_dir: Path | None,
    ) -> None:
        scene_images = tuple(
            image for image in getattr(materials, "images", ())
            if getattr(image, "role", None) == "场景参考"
        )
        manifest_path_value = metadata.get("scene_image_manifest_path")
        if not scene_images and manifest_path_value is None:
            return
        if manifest_path_value is None:
            raise WorkflowError("当前参考组场景图已变化，请重新离线分析并审核提示词")
        if run_dir is None:
            raise WorkflowError("当前任务目录缺失，无法核对场景参考图证据")
        manifest = self._load_scene_image_manifest(run_dir, metadata)
        rows = manifest.get("images") if isinstance(manifest, Mapping) else None
        if not isinstance(rows, list):
            raise WorkflowError("当前任务缺少场景参考图 SHA 证据，请重新离线分析")
        expected_group = metadata.get("reference_group_id", "flat-layout")
        if metadata.get("scene_image_reference_group_id") != expected_group:
            raise WorkflowError("场景参考图证据与当前参考组不匹配")
        by_name = {image.path.name: image for image in scene_images}
        if set(by_name) != {
            str(row.get("filename")) for row in rows if isinstance(row, Mapping)
        }:
            raise WorkflowError("当前参考组场景参考图名单已变化，请重新离线分析并审核提示词")
        for row in rows:
            if not isinstance(row, Mapping):
                raise WorkflowError("场景参考图证据行结构无效")
            image = by_name.get(str(row.get("filename")))
            if image is None:
                raise WorkflowError("当前参考组场景参考图缺失，请重新离线分析")
            try:
                image.path.resolve(strict=True).relative_to(materials.folder.resolve(strict=True))
                digest = _sha256_file_stable(image.path)
                metadata_now = image.path.stat()
            except (OSError, ValueError, WorkflowError):
                raise WorkflowError("当前参考组场景参考图路径或内容已变化，请重新离线分析") from None
            if (
                digest != row.get("sha256")
                or metadata_now.st_size != row.get("file_size_bytes")
                or str(image.path.resolve(strict=True)) != row.get("absolute_path")
            ):
                raise WorkflowError("场景参考图在提示词审核后发生变化，请重新分析并审核新提示词")

    def _write_metadata(
        self,
        run_dir: Path,
        value: dict[str, object],
        *,
        create: bool = False,
    ) -> None:
        _assert_no_sensitive_content(value)
        path = run_dir / _METADATA_NAME
        encoded = _json_bytes(value)
        if create:
            self._write_exclusive(path, encoded)
        else:
            self._atomic_replace(path, encoded)

    def _write_exclusive(self, path: Path, payload: bytes) -> None:
        self._write_exclusive_group(((Path(path), payload),))

    def _assert_exclusive_compatible(self, path: Path, payload: bytes) -> None:
        path = Path(path)
        self._check_output_parent(path.parent)
        _reject_reparse_components(path)
        if path.exists():
            self._assert_regular_file(path)
            if not _same_file_bytes(path, payload):
                raise WorkflowError(f"现有输出已存在且内容不同，已保留原文件：{path.name}")

    def _write_exclusive_group(self, outputs: Sequence[tuple[Path, bytes]]) -> None:
        reusable: set[Path] = set()
        for path, payload in outputs:
            path = Path(path)
            self._check_output_parent(path.parent)
            _reject_reparse_components(path)
            if path.exists():
                self._assert_regular_file(path)
                if _same_file_bytes(path, payload):
                    reusable.add(path)
                else:
                    raise WorkflowError(f"现有输出已存在且内容不同，已保留原文件：{path.name}")

        staged: list[tuple[Path, Path, bytes, tuple[int, int]]] = []
        published: list[tuple[Path, tuple[int, int]]] = []
        try:
            for path, payload in outputs:
                if path in reusable:
                    continue
                temp_path, identity = self._write_temp(path, payload)
                staged.append((temp_path, path, payload, identity))
            for temp_path, path, payload, identity in staged:
                _reject_reparse_components(path)
                published.append((path, identity))
                try:
                    if os.name == "nt":
                        os.rename(temp_path, path)
                    else:
                        os.link(temp_path, path)
                        temp_path.unlink()
                except FileExistsError:
                    published.pop()
                    if not _same_file_bytes(path, payload):
                        raise WorkflowError(
                            f"现有输出已存在且内容不同，已保留原文件：{path.name}"
                        ) from None
        except WorkflowError:
            for target, identity in reversed(published):
                _unlink_owned(target, identity)
            raise
        except OSError as exc:
            for target, identity in reversed(published):
                _unlink_owned(target, identity)
            raise WorkflowError(f"无法安全发布输出文件（{type(exc).__name__}）") from None
        finally:
            for temp_path, _, _, identity in staged:
                _unlink_owned(temp_path, identity)

    def _atomic_replace(self, path: Path, payload: bytes) -> None:
        path = Path(path)
        self._check_output_parent(path.parent)
        _reject_reparse_components(path)
        if path.exists():
            self._assert_regular_file(path)
        temp_path, identity = self._write_temp(path, payload)
        try:
            _reject_reparse_components(path)
            if path.exists():
                self._assert_regular_file(path)
            os.replace(temp_path, path)
        except OSError as exc:
            raise WorkflowError(f"无法原子更新本地工作流状态（{type(exc).__name__}）") from None
        finally:
            _unlink_owned(temp_path, identity)

    def _write_temp(self, target: Path, payload: bytes) -> tuple[Path, tuple[int, int]]:
        descriptor: int | None = None
        temp_path: Path | None = None
        identity: tuple[int, int] | None = None
        succeeded = False
        try:
            descriptor, raw_path = tempfile.mkstemp(
                prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
            )
            temp_path = Path(raw_path)
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = None
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            succeeded = True
            return temp_path, identity
        except OSError as exc:
            raise WorkflowError(f"无法创建本地临时文件（{type(exc).__name__}）") from None
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if not succeeded and temp_path is not None and identity is not None:
                _unlink_owned(temp_path, identity)

    @staticmethod
    def _assert_regular_file(path: Path) -> None:
        _reject_reparse_components(path)
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise WorkflowError(f"本地文件不存在：{path.name}") from None
        except OSError:
            raise WorkflowError(f"无法检查本地文件：{path.name}") from None
        if not stat.S_ISREG(metadata.st_mode):
            raise WorkflowError(f"本地路径不是普通文件：{path.name}")

    def _assert_regular_output(self, path: Path, run_dir: Path) -> None:
        path = Path(path)
        _reject_reparse_components(path)
        self._assert_download_path(path, run_dir)
        self._assert_regular_file(path)
        try:
            if path.stat().st_size <= 0:
                raise WorkflowError("下载的视频文件为空")
        except OSError:
            raise WorkflowError("无法检查已验证视频文件") from None

    def _client(self):
        if self.client is None:
            raise WorkflowError("可灵客户端未配置")
        return self.client

    def _new_external_id(self, kind: str, sku: str) -> str:
        new = getattr(self.ids, "new", None)
        if not callable(new):
            raise WorkflowError("外部任务编号生成器无效")
        value = new(kind, sku)
        if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
            raise WorkflowError("外部任务编号无效，未提交请求")
        if not re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$", value):
            raise WorkflowError("外部任务编号必须包含 UUID，未提交请求")
        return value

    def _record_processing_step(
        self,
        *,
        run: RunRecord,
        role: str,
        reason: str,
        result: str,
        evidence: str,
        exception: str = "",
    ) -> None:
        recorder = getattr(self.processing_log, "record_step", None)
        if not callable(recorder):
            return
        try:
            recorder(
                model="local",
                role=role,
                reason=reason,
                result=result,
                evidence=evidence,
                exception=exception,
                sku=run.sku,
                version=run.run_dir.name if re.fullmatch(r"v\d{3,6}", run.run_dir.name) else None,
            )
        except Exception:
            raise WorkflowError("处理日志写入失败；本地任务已停止并保留当前状态") from None

    def _safe_remote_fields(self, remote: RemoteTask) -> dict[str, object]:
        result: dict[str, object] = {}
        task_id = self._safe_remote_identifier(getattr(remote, "task_id", None))
        request_id = self._safe_remote_identifier(getattr(remote, "request_id", None))
        message = self._redact_known(_safe_text(getattr(remote, "message", "")))
        if task_id:
            result["task_id"] = task_id
        if request_id:
            result["request_id"] = request_id
        if message:
            result["message"] = message[:512]
        deduction = self._redact_known(_safe_text(getattr(remote, "final_unit_deduction", None)))
        if deduction:
            result["final_unit_deduction"] = deduction[:64]
        balance = getattr(remote, "final_balance_deduction", None)
        if isinstance(balance, Mapping):
            clean = {}
            for key, value in balance.items():
                if not isinstance(key, str) or self._safe_remote_identifier(key) is None:
                    continue
                if not isinstance(value, (str, int, float)):
                    continue
                safe_value = self._redact_known(_safe_text(str(value)))[:64]
                if safe_value:
                    clean[key] = safe_value
            if clean:
                result["final_balance_deduction"] = clean
        return result

    def _is_not_found(self, exc: Exception) -> bool:
        message = self._redact_known(_safe_text(str(exc))).casefold()
        return any(token in message for token in ("not found", "不存在", "未找到", "找不到"))

    def _safe_remote_identifier(self, value: object) -> str | None:
        identifier = _safe_identifier(value)
        if identifier is None:
            return None
        if any(secret in identifier for secret in self._sensitive_values):
            return None
        return identifier

    def _redact_known(self, value: str) -> str:
        cleaned = value
        for secret in sorted(self._sensitive_values, key=len, reverse=True):
            cleaned = cleaned.replace(secret, "[敏感信息已隐藏]")
        return cleaned

    @staticmethod
    def _as_element(remote: RemoteTask | RemoteElement) -> RemoteElement:
        if isinstance(remote, RemoteElement):
            return remote
        if not isinstance(remote, RemoteTask):
            raise WorkflowError("可灵元素接口返回了无效任务对象")
        return RemoteElement(**remote.__dict__, element_id=None)

    @staticmethod
    def _as_video(remote: RemoteTask | RemoteVideo) -> RemoteVideo:
        if isinstance(remote, RemoteVideo):
            return remote
        if not isinstance(remote, RemoteTask):
            raise WorkflowError("可灵视频接口返回了无效任务对象")
        return RemoteVideo(**remote.__dict__)

    @staticmethod
    def _valid_element_id(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _now(self) -> datetime:
        method = getattr(self.clock, "now", None)
        if callable(method):
            value = method()
            if isinstance(value, datetime):
                return value
        return datetime.now(timezone.utc)

    def _now_iso(self) -> str:
        method = getattr(self.clock, "now_iso", None)
        if callable(method):
            value = method()
            if isinstance(value, str):
                return _safe_text(value)
        return self._now().isoformat()

    def _monotonic(self) -> float:
        method = getattr(self.clock, "monotonic", None)
        return float(method()) if callable(method) else time.monotonic()

    def _sleep(self, seconds: float) -> None:
        method = getattr(self.clock, "sleep", None)
        if callable(method):
            method(max(0.0, seconds))
        else:
            time.sleep(max(0.0, seconds))


def _locked(path: Path) -> Iterator[None]:
    @contextmanager
    def acquire() -> Iterator[None]:
        _reject_reparse_components(path)
        key = os.path.normcase(str(path.absolute()))
        with _GLOBAL_LOCK_GUARD:
            lock = _THREAD_LOCKS.setdefault(key, threading.RLock())
        with lock:
            descriptor: int | None = None
            try:
                existed = path.exists()
                descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise WorkflowError("工作流锁不是普通文件")
                _reject_reparse_components(path)
                if not existed and os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            except WorkflowError:
                raise
            except Exception as exc:
                raise WorkflowError(f"无法取得本地任务锁（{type(exc).__name__}）") from None
            finally:
                if descriptor is not None:
                    try:
                        if os.name == "nt":
                            import msvcrt

                            os.lseek(descriptor, 0, os.SEEK_SET)
                            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                        else:
                            import fcntl

                            fcntl.flock(descriptor, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(descriptor)

    return acquire()


def _reject_reparse_components(path: Path) -> None:
    path = Path(path)
    if ".." in path.parts:
        raise WorkflowError("路径包含父目录跳转，拒绝访问")
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            raise WorkflowError("无法检查路径组件，已停止访问") from None
        attributes = getattr(metadata, "st_file_attributes", 0)
        if stat.S_ISLNK(metadata.st_mode) or attributes & 0x400:
            raise WorkflowError("路径经过符号链接或 Windows 重解析点，已停止访问")


def _json_bytes(value: object) -> bytes:
    _validate_json_shape(value)
    _assert_no_sensitive_content(value)
    try:
        rendered = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        raise WorkflowError("结构化分析或本地记录包含无效数据，无法安全序列化") from None
    sensitive = _find_sensitive(rendered)
    if sensitive:
        raise WorkflowError("分析或任务记录包含禁止保存的凭据、令牌、Base64 或下载链接")
    return (rendered + "\n").encode("utf-8")


def _json_safe_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise ValueError("non-finite value")
        return value
    if isinstance(value, str):
        if _find_sensitive(value):
            return _safe_text(value)
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_value(item) for item in value]
    if hasattr(value, "__dict__"):
        return _json_safe_value(vars(value))
    raise TypeError("unsupported value")


def _validate_json_shape(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in {float("inf"), float("-inf")}:
            raise WorkflowError("结构化分析或本地记录包含无效数字")
        return
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise WorkflowError("结构化分析字段名必须是文本")
        for item in value.values():
            _validate_json_shape(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_shape(item)
        return
    raise WorkflowError("结构化分析包含不支持的数据类型")


def _render_analysis_markdown(analysis: Mapping[str, object]) -> str:
    _assert_no_sensitive_content(analysis)
    try:
        encoded = json.dumps(
            analysis,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
    except (TypeError, ValueError, OverflowError):
        raise WorkflowError("动作与镜头分析必须是有效结构化数据") from None
    if _find_sensitive(encoded):
        raise WorkflowError("动作与镜头分析包含禁止保存的敏感内容")
    return "# 动作与镜头分析\n\n```json\n" + encoded + "\n```\n"


def _safe_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = value
    text = _BEARER_PATTERN.sub("[敏感信息已隐藏]", text)
    text = _JWT_PATTERN.sub("[敏感信息已隐藏]", text)
    text = _URL_PATTERN.sub("[下载链接已隐藏]", text)
    text = _BASE64_PATTERN.sub("[Base64已隐藏]", text)
    text = _KEY_PATTERN.sub("[凭据已隐藏]", text)
    return text[:2048]


def _find_sensitive(value: str) -> bool:
    return bool(
        _BEARER_PATTERN.search(value)
        or _JWT_PATTERN.search(value)
        or _URL_PATTERN.search(value)
        or _BASE64_PATTERN.search(value)
        or _KEY_PATTERN.search(value)
        or _AK_SK_VALUE_PATTERN.search(value)
    )


def _validate_semantic_review(
    analysis: Mapping[str, object], metadata: Mapping[str, object]
) -> None:
    source = metadata.get("video_manifest")
    if not isinstance(source, Mapping):
        raise WorkflowError("视频逐项语义复核缺少程序生成的视频清单")
    source_videos = source.get("videos")
    if not isinstance(source_videos, list):
        raise WorkflowError("视频逐项语义复核的视频清单结构无效")
    if not source_videos:
        return
    candidate = analysis.get("video_manifest")
    if not isinstance(candidate, Mapping):
        raise WorkflowError("视频逐项语义复核必须保留完整 video_manifest")
    candidate_videos = candidate.get("videos")
    if not isinstance(candidate_videos, list) or len(candidate_videos) != len(source_videos):
        raise WorkflowError("视频逐项语义复核必须保留全部程序生成视频行")

    expected = json.loads(_json_bytes(source).decode("utf-8"))
    for index, (source_row, candidate_row) in enumerate(
        zip(source_videos, candidate_videos, strict=True)
    ):
        if not isinstance(source_row, Mapping) or not isinstance(candidate_row, Mapping):
            raise WorkflowError("视频逐项语义复核的视频行结构无效")
        status = source_row.get("status")
        if status != "success":
            raise WorkflowError("视频逐项语义复核发现失败视频；请先修复并重新分析")
        for key, value in source_row.items():
            if key != "semantic_review" and candidate_row.get(key) != value:
                raise WorkflowError("视频逐项语义复核不得改写程序生成的清单元数据")
        review = candidate_row.get("semantic_review")
        if not isinstance(review, Mapping) or set(review) != set(SEMANTIC_FIELDS):
            raise WorkflowError("视频逐项语义复核必须逐视频填写全部 16 个字段")
        for field_name in SEMANTIC_FIELDS:
            value = review.get(field_name)
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError("视频逐项语义复核不允许空白字段；无法判断请显式写未知")
            normalized = re.sub(r"\s+", "", value).casefold()
            if normalized == "未知":
                continue
            generic = {
                "待填写",
                "请填写",
                "待补充",
                "待确认",
                "待分析",
                "同上",
                "同参考",
                "与参考一致",
                "保持不变",
                "正常",
                "正常展示即可",
                "见联系表",
                "按联系表",
                "按参考",
                "参考为准",
                "整体符合参考",
                "按0.25秒证据填写全部可见动作和时间码;未知动作明确写未知",
            }
            if normalized in generic or "或未知" in normalized or "待填写" in normalized:
                raise WorkflowError("视频逐项语义复核包含空白或泛化描述；请按证据填写或显式写未知")
        expected["videos"][index]["semantic_review"] = dict(review)
    if _json_bytes(expected) != _json_bytes(candidate):
        raise WorkflowError("视频逐项语义复核必须完整保留程序生成的 video_manifest")


def _assert_no_sensitive_content(value: object) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise WorkflowError("结构化内容字段名必须是文本")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if any(fragment in normalized for fragment in _SENSITIVE_FIELD_FRAGMENTS):
                raise WorkflowError("内容包含禁止保存的敏感字段")
            _assert_no_sensitive_content(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _assert_no_sensitive_content(item)
        return
    if isinstance(value, str) and _find_sensitive(value):
        raise WorkflowError("内容包含禁止保存的凭据、令牌、Base64 或下载链接")
    if hasattr(value, "__dict__") and not isinstance(value, type):
        _assert_no_sensitive_content(vars(value))


def _safe_identifier(value: object) -> str | None:
    if not isinstance(value, str) or not _SAFE_IDENTIFIER.fullmatch(value):
        return None
    if _find_sensitive(value) or value in {".", ".."}:
        return None
    return value


def _same_file_bytes(path: Path, payload: bytes) -> bool:
    _reject_reparse_components(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return False
    if not stat.S_ISREG(metadata.st_mode):
        raise WorkflowError(f"現有輸出不是普通文件：{path.name}")
    try:
        return path.read_bytes() == payload
    except OSError:
        raise WorkflowError(f"無法核对现有输出：{path.name}") from None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
    )


def _sha256_file_stable(path: Path) -> str:
    digest = hashlib.sha256()
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise WorkflowError("参考视频必须是普通文件")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if _stat_identity(before) != _stat_identity(opened):
            raise WorkflowError("参考视频在打开期间发生变化")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after_open = os.fstat(stream.fileno())
    after_path = path.lstat()
    if _stat_identity(before) != _stat_identity(after_open) or _stat_identity(before) != _stat_identity(after_path):
        raise WorkflowError("参考视频在读取期间发生变化")
    return digest.hexdigest()


def _unlink_owned(path: Path, identity: tuple[int, int]) -> None:
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) == identity and stat.S_ISREG(metadata.st_mode):
            path.unlink()
    except OSError:
        pass
