"""Offline product coordination; upload/result contracts still require live verification.

Only the reviewed full prompt is submitted. Images use immutable local data-URI
snapshots; uploaded video URLs live only in memory. There is no force switch.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from decimal import Decimal, ROUND_FLOOR
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .assets import prepare_api_images, element_fingerprint, scan_sku_images
from .models import TaskStatus
from .media_audio import resolve_ffmpeg
from .prompting import GARMENT_PROTECTION_SUFFIX
from .provider_config import (
    ProviderConfig, ProviderContractError, LEGACY_TASK_BLOCKED, MODEL_BINDING_ERROR,
    NO_TASK_ID, SEEDANCE_MODELS, CONTRACT_BLOCKED, require_generation_contract,
)
from .seedance_models import supports_resolution
from .reference_groups import resolve_reference_group_order
from .seedance_client import validate_payload, _public_url
from .seedance_upload import _read_snapshot
from .task_store import _validate_sku_segment
from .video_analysis import AnalysisManifest
from .workflow import (
    Workflow,
    WorkflowError,
    ApprovalError,
    _INPUT_NAME,
    _json_bytes,
    _locked,
)


class SeedanceWorkflow(Workflow):
    def __init__(self, *args, provider_config: ProviderConfig, uploader=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.provider_config = provider_config
        self.uploader = uploader
        self.poll_interval_seconds = 10.0

    def _preparation_images(self, materials):
        return (*materials.images, *materials.action_reference_images)

    def _select_preparation_assets(self, assets, project_materials, video_paths):
        if not 1 <= len(assets) <= 9 or not 1 <= len(video_paths) <= 3:
            raise WorkflowError("Seedance 当前组需要 1–9 张图片及 1–3 个实际参考视频")
        return assets, ()

    def _prepare_images(self, assets, run_dir):
        return prepare_api_images(tuple(assets), run_dir, max_images=9, allow_action_images=True)

    def _snapshot_reference_videos(self, directory, source_paths=None):
        return sorted(super()._snapshot_reference_videos(directory, source_paths),
                      key=lambda row: (row["name"].casefold(), row["name"]))

    @staticmethod
    def _is_standard_model(model: object) -> bool:
        return isinstance(model, str) and model.startswith("Seedance-")

    def _seedance_derivative_root(self, metadata):
        version = metadata.get("seedance_run_version")
        sku = metadata.get("sku")
        if (
            not isinstance(version, str)
            or not re.fullmatch(r"v\d{3,}", version)
            or not isinstance(sku, str)
        ):
            raise WorkflowError("Seedance 裁剪版本记录缺失，请重新分析")
        return (
            self.paths.history
            / sku
            / ".seedance-video-derivatives"
            / version
        )

    @staticmethod
    def _publish_copy(source: Path, destination: Path) -> None:
        try:
            with source.open("rb") as source_stream, destination.open("xb") as target_stream:
                shutil.copyfileobj(source_stream, target_stream)
                target_stream.flush()
                os.fsync(target_stream.fileno())
        except OSError:
            raise WorkflowError("无法创建 Seedance 参考视频快照") from None

    @staticmethod
    def _trim_video(source: Path, destination: Path, seconds: int) -> None:
        binary = resolve_ffmpeg()
        if not binary:
            raise WorkflowError("Seedance 自动裁剪需要本地 ffmpeg，尚未生成或提交")
        temp_path = destination.with_name(f".{destination.stem}.trim.tmp.mp4")
        if destination.exists() or temp_path.exists():
            raise WorkflowError("Seedance 裁剪目标发生冲突，请重新分析")
        command = [
            binary,
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-n",
            "-i",
            str(source),
            "-t",
            str(seconds),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(temp_path),
        ]
        try:
            completed = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=300,
            )
            if completed.returncode != 0 or not temp_path.is_file():
                raise WorkflowError("Seedance 动作视频尾部裁剪失败；原视频未修改")
            os.replace(temp_path, destination)
        except subprocess.TimeoutExpired:
            raise WorkflowError("Seedance 动作视频裁剪超时；原视频未修改") from None
        except WorkflowError:
            raise
        except OSError:
            raise WorkflowError("Seedance 动作视频裁剪失败；原视频未修改") from None
        finally:
            try:
                if temp_path.exists():
                    temp_path.unlink()
            except OSError:
                pass

    @staticmethod
    def _rewrite_analysis_input(run_dir: Path, manifest, audio_policy) -> None:
        input_path = run_dir / _INPUT_NAME
        try:
            payload = json.loads(input_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("invalid input")
            payload["video_manifest"] = manifest
            payload["audio_policy"] = audio_policy
            descriptor, temp_name = tempfile.mkstemp(
                prefix=".seedance-analysis-", suffix=".tmp", dir=run_dir
            )
            temp_path = Path(temp_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(_json_bytes(payload))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temp_path, input_path)
            finally:
                if temp_path.exists():
                    temp_path.unlink()
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise WorkflowError("无法保存 Seedance 裁剪后分析输入") from None

    def _prepare_seedance_video_inputs(self, metadata, run_dir):
        source_manifest = metadata.get("video_manifest")
        source_rows = (
            source_manifest.get("videos")
            if isinstance(source_manifest, dict)
            else None
        )
        if not isinstance(source_rows, list) or not source_rows:
            raise WorkflowError("Seedance 原始参考视频分析记录缺失")
        derivative_root = (
            self.paths.history
            / str(metadata.get("sku", ""))
            / ".seedance-video-derivatives"
            / run_dir.name
        )
        try:
            derivative_root.mkdir(parents=True)
        except OSError:
            raise WorkflowError("无法创建 Seedance 参考视频派生目录") from None
        source_root = (
            self.paths.materials / str(metadata.get("sku", ""))
            if metadata.get("source_layout") == "gemini"
            else self.paths.root / "02款号参考图" / str(metadata.get("sku", ""))
        )
        action_paths = []
        scene_paths = []
        evidence = []
        seen_names = set()
        for row in source_rows:
            if not isinstance(row, dict) or row.get("role") not in {
                "action_reference",
                "scene_reference",
            }:
                raise WorkflowError("Seedance 原始参考视频角色无效")
            source = Path(str(row.get("path", "")))
            try:
                source = source.resolve(strict=True)
                self._require_within(source, source_root)
                self._assert_regular_file(source)
            except (OSError, WorkflowError):
                raise WorkflowError("Seedance 原始参考视频路径无效") from None
            if source.name in seen_names:
                raise WorkflowError("Seedance 参考视频文件名冲突")
            seen_names.add(source.name)
            destination = derivative_root / source.name
            role = row["role"]
            if role == "scene_reference":
                self._publish_copy(source, destination)
                scene_paths.append(destination)
                continue
            raw_duration = row.get("duration_seconds")
            if (
                not isinstance(raw_duration, (int, float))
                or isinstance(raw_duration, bool)
            ):
                raise WorkflowError("Seedance 动作参考时长无效")
            source_duration = Decimal(str(raw_duration))
            target_duration = int(source_duration.to_integral_value(rounding=ROUND_FLOOR))
            if not 4 <= target_duration <= 15:
                raise WorkflowError("Seedance 裁剪后动作时长必须为 4–15 秒整数")
            if source_duration == Decimal(target_duration):
                self._publish_copy(source, destination)
                method = "snapshot_copy"
            else:
                self._trim_video(source, destination, target_duration)
                method = "tail_trim_floor"
            action_paths.append(destination)
            evidence.append(
                {
                    "source": self._relative_project_path(source),
                    "derived": self._relative_project_path(destination),
                    "source_sha256": str(row.get("sha256", "")),
                    "source_duration_seconds": float(source_duration),
                    "target_duration_seconds": target_duration,
                    "method": method,
                }
            )
        video_paths = [*action_paths, *scene_paths]
        if self._uses_default_video_analyzer:
            analyzed = self.video_analyzer(
                video_paths, run_dir, cache=self.video_cache
            )
        else:
            analyzed = self.video_analyzer(video_paths, run_dir)
        if not isinstance(analyzed, AnalysisManifest):
            raise WorkflowError("Seedance 裁剪后参考视频分析结构无效")
        manifest = self._video_manifest_with_roles(
            analyzed, action_paths, scene_paths
        )
        snapshot = self._snapshot_reference_videos(derivative_root, video_paths)
        self._verify_video_manifest(manifest, snapshot, derivative_root)
        action_by_name = {
            Path(str(row["path"])).name: row
            for row in manifest["videos"]
            if row.get("role") == "action_reference"
        }
        for item in evidence:
            analyzed_row = action_by_name.get(Path(item["derived"]).name)
            if (
                analyzed_row is None
                or Decimal(str(analyzed_row.get("duration_seconds")))
                != Decimal(item["target_duration_seconds"])
            ):
                raise WorkflowError("Seedance 裁剪后视频时长未精确达到目标整数")
            item["derived_sha256"] = analyzed_row["sha256"]
        metadata["seedance_source_video_manifest"] = json.loads(
            json.dumps(source_manifest, ensure_ascii=False)
        )
        metadata["video_manifest"] = manifest
        metadata["seedance_video_preprocessing"] = evidence
        metadata["audio_policy"] = self._reference_audio_policy(metadata)
        self._rewrite_analysis_input(run_dir, manifest, metadata["audio_policy"])

    def _finalize_preparation_metadata(self, metadata, run_dir):
        config = self.provider_config
        if (config.provider != "aimaxhug" or config.model not in SEEDANCE_MODELS
                or config.aspect_ratio != "9:16"
                or config.resolution != "720p"
                or not supports_resolution(config.model, config.resolution)):
            raise ProviderContractError(MODEL_BINDING_ERROR)
        metadata.update(generation_provider="aimaxhug", generation_model=config.model,
                        generation_resolution=config.resolution, generation_aspect_ratio=config.aspect_ratio)
        metadata["seedance_run_version"] = run_dir.name
        self._prepare_seedance_video_inputs(metadata, run_dir)
        metadata["seedance_media"] = self._media_manifest(metadata)
        self._reference_timing(metadata)

    def _media_manifest(self, metadata):
        prepared = metadata["prepared"]
        images = [prepared["frontal"], *prepared["references"]]
        videos = metadata["video_manifest"]["videos"]
        if not 1 <= len(images) <= 9 or not 1 <= len(videos) <= 3:
            raise WorkflowError("Seedance 素材清单数量无效")
        result = {"images": [], "videos": []}
        for index, row in enumerate(images, 1):
            result["images"].append({"index": index, **row})
        derivative_root = self._seedance_derivative_root(metadata)
        for row in videos:
            role = row.get("role")
            if role not in {"action_reference", "scene_reference"}:
                raise WorkflowError("Seedance 视频角色缺失")
            path = Path(row["path"])
            source = derivative_root
            if not path.is_absolute():
                path = source / path
            self._require_within(path, source)
            result["videos"].append({"role": role, "source": self._relative_project_path(path),
                                     "sha256": row["sha256"], "storage": "run_derivative"})
        result["videos"].sort(key=lambda row: (row["role"] != "action_reference", row["source"].casefold(), row["source"]))
        for index, row in enumerate(result["videos"], 1):
            row["index"] = index
        return result

    def _require_binding(self, metadata):
        if metadata.get("generation_provider") != "aimaxhug":
            raise ProviderContractError(LEGACY_TASK_BLOCKED)
        if (metadata.get("generation_model") not in SEEDANCE_MODELS
                or metadata.get("generation_model") != self.provider_config.model
                or metadata.get("generation_resolution") != self.provider_config.resolution
                or not supports_resolution(
                    self.provider_config.model, self.provider_config.resolution
                )
                or self.provider_config.resolution != "720p"
                or metadata.get("generation_aspect_ratio") != "9:16"
                or self.provider_config.aspect_ratio != "9:16"
                or self.provider_config.provider != "aimaxhug"):
            raise ProviderContractError(MODEL_BINDING_ERROR)
        media = metadata.get("seedance_media")
        if not isinstance(media, dict) or media != self._media_manifest(metadata):
            raise WorkflowError("Seedance 素材顺序或角色清单已变化，请重新分析审核")

    def _reference_timing(self, metadata, shots=None):
        reference = self._reference_duration(metadata)
        if reference is None:
            raise WorkflowError("缺少动作参考总时长")
        duration = Decimal(str(reference))
        if not duration.is_finite() or duration <= 0:
            raise WorkflowError("动作参考时长必须是有限的正数")
        if shots is not None and sum((Decimal(str(s.duration)) for s in shots), Decimal(0)) != duration:
            raise WorkflowError("分镜总时长必须与动作参考精确一致")
        return duration

    def _duration(self, metadata, shots=None):
        duration = self._reference_timing(metadata, shots)
        if duration != duration.to_integral_value() or not 4 <= duration <= 15:
            raise WorkflowError("Seedance 动作参考总时长必须精确为 4–15 秒整数；禁止取整或改速")
        return int(duration)

    def _render_prompt_document(self, metadata, *args, **kwargs):
        self._require_binding(metadata)
        self._reference_timing(metadata, args[0])
        media = metadata["seedance_media"]
        mapping = {}
        standard = self._is_standard_model(metadata.get("generation_model"))
        for kind, label in (("images", "@image" if standard else "图片"),
                            ("videos", "@video" if standard else "视频")):
            for row in media[kind]:
                mapping[f"{label}{row['index']}（{row['role']}）"] = [Path(row["source"]).name]
        kwargs["reference_mapping"] = mapping
        # Keep the existing ShotPlan protection/continuity validation intact.
        # Conversion is part of rendering, BEFORE review, file persistence and SHA.
        draft = super()._render_prompt_document(metadata, *args, **kwargs)
        lines = []
        for line in draft.splitlines():
            if line.startswith(("- 模式：", "- 速度策略：", "- 原生音频：", "- 可灵音频参数：")):
                continue
            if line == "- 使用款号服装元素作为角色与服装参考。":
                line = "- 按上述图片与视频数组顺序提交；视频仅提供动作、节奏和镜头。"
            line = line.replace("# 可灵图生视频提示词", "# Seedance 图生视频提示词")
            if line == "- 模型：kling-v3":
                line = f"- 模型：{metadata['generation_model']}"
            if line == "- 元素优先最高优先级：服装以元素为准，忽略参考视频里那个人穿的衣服；参考视频只提供动作、步态、节奏与镜头。":
                line = "- 参考图片最高优先级：服装以参考图片为准，忽略参考视频里那个人穿的衣服；参考视频只提供动作、步态、节奏与镜头。"
            line = line.replace(GARMENT_PROTECTION_SUFFIX,
                                GARMENT_PROTECTION_SUFFIX.replace("服装以元素为准", "服装以参考图片为准"))
            line = line.replace("<<<element_1>>>", "@image1" if standard else "图片1")
            lines.append(line)
        digest = hashlib.sha256(json.dumps(media, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        lines[3:3] = [f"- 分辨率：{metadata['generation_resolution']}", f"- 素材清单指纹：{digest}"]
        prompt = "\n".join(lines) + "\n"
        if any(token in prompt for token in ("<<<element_", "kling-", "enable_audio", "keepOriginalSound")):
            raise WorkflowError("Seedance 提示词包含旧平台引用或参数，请重新审核")
        return prompt

    def _prepare_current_assets(self, run, metadata):
        try:
            # Reject a changed derivative instead of silently healing reviewed inputs.
            for row in metadata["seedance_media"]["images"]:
                _read_snapshot(run.run_dir / row["api_path"], run.run_dir, row["derived_sha256"])
            assets = (self._preparation_images(self._scan_current_project_materials(run.sku, metadata))
                      if metadata["source_layout"] == "gemini" else scan_sku_images(self.paths, run.sku))
            prepared = self._prepare_images(assets, run.run_dir)
            if (element_fingerprint(prepared) != metadata.get("element_fingerprint")
                    or self._prepared_record(prepared) != metadata.get("prepared")):
                raise ValueError("changed")
            return prepared
        except Exception:
            raise WorkflowError("Seedance 图片或派生文件在审核后发生变化，请重新分析审核") from None

    def _local_payload(self, run, metadata):
        images = []
        for row in metadata["seedance_media"]["images"]:
            try:
                raw, _, _, mime = _read_snapshot(run.run_dir / row["api_path"], run.run_dir, row["derived_sha256"])
            except Exception:
                raise WorkflowError("Seedance 图片快照校验失败") from None
            images.append(f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"))
        payload = {"model": metadata["generation_model"], "prompt": self._render_saved_prompt(run, metadata),
                   "duration": self._duration(metadata, self._saved_shots(metadata)), "aspect_ratio": "9:16",
                   "resolution": metadata["generation_resolution"]}
        if self._is_standard_model(metadata["generation_model"]):
            payload.update(
                image_url=images[0],
                reference_image_urls=images[1:],
                reference_videos=[],
            )
        else:
            payload.update(images=images, referenceVideos=[])
        validate_payload(payload)
        return payload

    def _verify_reference_video_sources(self, metadata, run_dir=None):
        source_manifest = metadata.get("seedance_source_video_manifest")
        if not isinstance(source_manifest, dict):
            raise WorkflowError("Seedance 原始参考视频证据缺失，请重新分析")
        source_metadata = dict(metadata)
        source_metadata["video_manifest"] = source_manifest
        super()._verify_reference_video_sources(source_metadata, run_dir)
        derivative_root = self._seedance_derivative_root(metadata)
        manifest = metadata.get("video_manifest")
        rows = manifest.get("videos") if isinstance(manifest, dict) else None
        if not isinstance(rows, list) or not rows:
            raise WorkflowError("Seedance 裁剪后参考视频证据缺失")
        paths = [Path(str(row.get("path", ""))) for row in rows]
        snapshot = self._snapshot_reference_videos(derivative_root, paths)
        self._verify_video_manifest(manifest, snapshot, derivative_root)

    @contextmanager
    def _remote_resources(self, model, *, upload=False):
        owned_client, owned_uploader = self.client is None, upload and self.uploader is None
        try:
            if owned_client or owned_uploader:
                from .seedance_credentials import SeedanceCredentialStore, _valid_key
                store = self.credential_store or SeedanceCredentialStore()
                provider = store.read
                if upload:
                    # Resolve once before writing upload intent. Both owned HTTP
                    # clients use the same in-memory key for this operation.
                    try:
                        api_key = provider()
                        if not _valid_key(api_key):
                            raise ValueError("invalid credential")
                    except Exception:
                        raise WorkflowError("Aimaxhug 凭据缺失或不可用；尚未上传或提交") from None
                    provider = lambda: api_key
                if owned_client:
                    from .seedance_client import SeedanceClient
                    self.client = SeedanceClient(model=model, api_key_provider=provider)
                if owned_uploader:
                    from .seedance_upload import SeedanceUploader
                    self.uploader = SeedanceUploader(api_key_provider=provider)
            if getattr(self.client, "model", None) != model:
                raise ProviderContractError(MODEL_BINDING_ERROR)
            yield
        finally:
            try:
                if owned_uploader and self.uploader is not None:
                    self.uploader.close()
            finally:
                if owned_uploader:
                    self.uploader = None
                try:
                    if owned_client and self.client is not None:
                        self.client.close()
                finally:
                    if owned_client:
                        self.client = None

    def _client(self):
        if self.client is None:
            raise WorkflowError("Seedance 客户端尚未初始化；禁止使用旧供应商")
        return self.client

    def _submission_lock_path(self, sku):
        return self.paths.results_for(sku) / sku / ".seedance-submit.lock"

    def _existing_submission_lock_path(self, sku, version):
        path = self._submission_lock_path(sku)
        if not path.parent.is_dir():
            # Preserve the existing explicit waiting-task error without
            # creating folders for an invalid or stale request.
            self._find_waiting_run(sku, version=version)
        return path

    @staticmethod
    def _group_identity(metadata):
        value = metadata.get("reference_group_id")
        return value if isinstance(value, str) else "flat-layout"

    @staticmethod
    def _batch_context(batch_group_id, predecessor_group_ids):
        if batch_group_id is not None and (
            not isinstance(batch_group_id, str)
            or not re.fullmatch(r"(?:group-\d{4,}|flat-layout)", batch_group_id)
        ):
            raise WorkflowError("Seedance 批次参考组身份无效")
        if not isinstance(predecessor_group_ids, (tuple, list)) or any(
            not isinstance(item, str)
            or not re.fullmatch(r"group-\d{4,}", item)
            for item in predecessor_group_ids
        ):
            raise WorkflowError("Seedance 前序参考组清单无效")
        predecessors = tuple(predecessor_group_ids)
        if len(set(predecessors)) != len(predecessors):
            raise WorkflowError("Seedance 前序参考组清单重复")
        return batch_group_id, predecessors

    def _assert_submission_guard(
        self,
        run,
        metadata,
        *,
        batch_group_id=None,
        predecessor_group_ids=(),
        require_persisted_predecessors=True,
    ):
        current_group = self._group_identity(metadata)
        if current_group != "flat-layout" and not re.fullmatch(
            r"group-\d{4,}", current_group
        ):
            raise WorkflowError("Seedance 当前参考组身份无效")
        if batch_group_id is None and current_group != "flat-layout":
            raise WorkflowError(
                "Seedance 编号参考组必须通过 batch-generate 执行，以核对完整批量顺序"
            )
        if batch_group_id is not None and current_group != batch_group_id:
            raise WorkflowError("Seedance 当前任务与批次参考组身份不匹配")
        if current_group != "flat-layout":
            order_result = resolve_reference_group_order(
                self.paths.materials, self.paths.history, run.sku
            )
            order = order_result.get("group_order")
            if (
                order_result.get("error")
                or order_result.get("conflicts")
                or not isinstance(order, list)
            ):
                raise WorkflowError("无法安全核对该款号完整参考组顺序")
            positions = [
                index
                for index, item in enumerate(order)
                if isinstance(item, dict) and item.get("group_id") == current_group
            ]
            if len(positions) != 1:
                raise WorkflowError("当前编号参考组不在本地完整顺序清单中")
            expected_predecessors = tuple(
                str(item["group_id"]) for item in order[: positions[0]]
            )
            if tuple(predecessor_group_ids) != expected_predecessors:
                raise WorkflowError("Seedance 前序参考组清单不完整或顺序不一致")

        sku_dir = self.paths.results_for(run.sku) / run.sku
        known_groups = set()
        unknown_groups = set()
        active_groups = set()
        invalid_known_groups = set()
        for other_dir in self._run_directories(sku_dir):
            try:
                other = self._load_run(other_dir)
                other_metadata = self._read_metadata(other_dir)
            except Exception:
                raise WorkflowError("无法安全核对该款号既有 Seedance 提交记录") from None
            group_id = self._group_identity(other_metadata)
            task_id = (
                self._safe_remote_identifier(other.video_task_id)
                if other.video_task_id
                else None
            )
            if (
                not task_id
                and other_metadata.get("stage")
                in {"creation_unknown", "upload_unknown"}
            ):
                unknown_groups.add(group_id)
                continue
            if other_metadata.get("generation_provider") != "aimaxhug":
                continue
            if other.video_task_id and task_id != other.video_task_id:
                raise WorkflowError("既有 Seedance 任务编号无效，禁止继续批量提交")
            native_seedance = (
                other_metadata.get("generation_model") in SEEDANCE_MODELS
                and not other_metadata.get("external_provider")
                and not other.video_external_id
            )
            if task_id and native_seedance:
                known_groups.add(group_id)
                if other.status not in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                    active_groups.add(group_id)
            elif task_id:
                invalid_known_groups.add(group_id)

        if unknown_groups:
            raise WorkflowError("该款号存在结果未知的 Seedance 提交，全部新建任务已冻结")
        if current_group in invalid_known_groups:
            raise WorkflowError("同一参考组已有来源或模型无效的任务编号，禁止重复创建")
        if current_group in active_groups:
            raise WorkflowError("同一参考组已有未结束的 Seedance 任务，禁止重复创建")
        for group_id in predecessor_group_ids:
            if group_id in invalid_known_groups:
                raise WorkflowError("前序参考组不是可安全续查的原生 Seedance 模型任务")
            if require_persisted_predecessors and group_id not in known_groups:
                raise WorkflowError("前序参考组尚无可安全续查的持久任务编号，禁止提交后续组")

    def preflight_generate(
        self,
        sku,
        prompt_sha,
        *,
        version=None,
        batch_group_id=None,
        predecessor_group_ids=(),
    ):
        batch_group_id, predecessor_group_ids = self._batch_context(
            batch_group_id, predecessor_group_ids
        )
        with _locked(self._existing_submission_lock_path(sku, version)):
            run_dir = self._find_waiting_run(sku, version=version)
            with _locked(run_dir / ".workflow.lock"):
                if self._find_waiting_run(sku, version=version) != run_dir:
                    raise ApprovalError("待确认任务已变化")
                run = self._load_run(run_dir)
                metadata = self._read_metadata(run_dir)
                self._require_binding(metadata)
                self._assert_submission_guard(
                    run,
                    metadata,
                    batch_group_id=batch_group_id,
                    predecessor_group_ids=predecessor_group_ids,
                    require_persisted_predecessors=False,
                )
                if run.video_task_id or run.video_external_id or metadata.get("external_provider"):
                    raise ProviderContractError(NO_TASK_ID)
                if (
                    run.status is not TaskStatus.WAITING_APPROVAL
                    or not isinstance(prompt_sha, str)
                    or not isinstance(run.prompt_sha256, str)
                    or not hmac.compare_digest(prompt_sha, run.prompt_sha256)
                ):
                    raise ApprovalError("提示词版本或等待确认状态不一致")
                self._verify_saved_prompt(run, metadata)
                self._require_persisted_sol_review(run, metadata)
                self._prepare_current_assets(run, metadata)
                self._verify_reference_video_sources(metadata, run_dir)
                validate_payload(self._local_payload(run, metadata))
                return {
                    "ok": True,
                    "sku": sku,
                    "version": run_dir.name,
                    "group_id": self._group_identity(metadata),
                }

    def resolve_creation_unknown(
        self,
        sku,
        version,
        *,
        confirmed_no_task_no_charge=False,
    ):
        """Reopen one ambiguous create only after an explicit provider-side check.

        This is deliberately offline and narrower than a retry: it accepts only
        an Aimaxhug ``creation_unknown`` run with no persisted remote identity.
        The paid ``generate`` command remains a separate, explicit action.
        """

        if confirmed_no_task_no_charge is not True:
            raise ApprovalError("恢复创建未知状态必须明确确认供应商未创建任务且未扣费")
        try:
            _validate_sku_segment(sku)
        except (TypeError, ValueError):
            raise WorkflowError("款号必须是有效的 Windows 单级目录名") from None
        if not isinstance(version, str) or not re.fullmatch(r"v\d{3,}", version):
            raise ApprovalError("指定任务版本无效")
        number = int(version[1:])
        if number < 1 or version != f"v{number:03d}":
            raise ApprovalError("指定任务版本无效")

        with _locked(self._submission_lock_path(sku)):
            try:
                run_dir = self._assert_safe_run_dir(
                    self._version_run_dir(sku, version), sku
                )
            except WorkflowError:
                raise ApprovalError("指定任务版本不存在或无法安全读取") from None
            with _locked(run_dir / ".workflow.lock"):
                run = self._load_run(run_dir)
                metadata = self._read_metadata(run_dir)
                self._require_binding(metadata)
                if metadata.get("stage") != "creation_unknown":
                    raise WorkflowError("仅可恢复创建结果未知的 Seedance 任务")
                if (
                    run.video_task_id
                    or run.video_external_id
                    or run.element_task_id
                    or run.element_external_id
                    or metadata.get("external_provider")
                ):
                    raise ProviderContractError(NO_TASK_ID)
                if run.status not in {
                    TaskStatus.APPROVED,
                    TaskStatus.WAITING_APPROVAL,
                }:
                    raise WorkflowError("创建未知任务的本地状态不允许恢复")
                verification_metadata = dict(metadata)
                verification_metadata["stage"] = "waiting_approval"
                self._verify_saved_prompt(run, verification_metadata)
                self._require_persisted_sol_review(run, metadata)
                self._prepare_current_assets(run, metadata)
                self._verify_reference_video_sources(metadata, run_dir)

                history = metadata.get("creation_unknown_resolutions", [])
                if not isinstance(history, list) or any(
                    not isinstance(item, dict) for item in history
                ):
                    raise WorkflowError("创建未知状态的人工核查记录无效")
                resolution = {
                    "decision": "confirmed_no_task_no_charge",
                    "source": "user_manual_provider_check",
                    "previous_stage": "creation_unknown",
                    "recorded_at": self._now_iso(),
                }

                # Status-first is fail-closed: if metadata persistence fails,
                # ``creation_unknown`` remains and every POST is still frozen.
                recovered = self._update_run(
                    run_dir, status=TaskStatus.WAITING_APPROVAL
                )
                updated = dict(metadata)
                updated["creation_unknown_resolutions"] = [*history, resolution]
                updated["stage"] = "waiting_approval"
                self._write_metadata(run_dir, updated)
                return recovered

    def generate(
        self,
        sku,
        prompt_sha,
        paid_confirmation,
        *,
        version=None,
        submit_only=False,
        batch_group_id=None,
        predecessor_group_ids=(),
    ):
        if paid_confirmation is not True:
            raise ApprovalError("缺少用户当前明确的开始生成确认")
        if type(submit_only) is not bool:
            raise WorkflowError("Seedance 提交模式无效")
        batch_group_id, predecessor_group_ids = self._batch_context(
            batch_group_id, predecessor_group_ids
        )
        with _locked(self._existing_submission_lock_path(sku, version)):
            return self._generate_locked(
                sku,
                prompt_sha,
                version=version,
                submit_only=submit_only,
                batch_group_id=batch_group_id,
                predecessor_group_ids=predecessor_group_ids,
            )

    def _generate_locked(
        self,
        sku,
        prompt_sha,
        *,
        version,
        submit_only,
        batch_group_id,
        predecessor_group_ids,
    ):
        run_dir = self._find_waiting_run(sku, version=version)
        with _locked(run_dir / ".workflow.lock"):
            if self._find_waiting_run(sku, version=version) != run_dir:
                raise ApprovalError("待确认任务已变化")
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            self._require_binding(metadata)
            self._assert_submission_guard(
                run,
                metadata,
                batch_group_id=batch_group_id,
                predecessor_group_ids=predecessor_group_ids,
            )
            if run.video_task_id or run.video_external_id or metadata.get("external_provider"):
                raise ProviderContractError(NO_TASK_ID)
            if (run.status is not TaskStatus.WAITING_APPROVAL or not isinstance(prompt_sha, str)
                    or not isinstance(run.prompt_sha256, str) or not hmac.compare_digest(prompt_sha, run.prompt_sha256)):
                raise ApprovalError("提示词版本或等待确认状态不一致")
            self._verify_saved_prompt(run, metadata)
            self._require_persisted_sol_review(run, metadata)
            self._prepare_current_assets(run, metadata)
            self._verify_reference_video_sources(metadata, run_dir)
            if self.client is not None and getattr(self.client, "model", None) != metadata["generation_model"]:
                raise ProviderContractError(MODEL_BINDING_ERROR)
            payload = self._local_payload(run, metadata)
            require_generation_contract()
            with self._remote_resources(metadata["generation_model"], upload=True):
                # A crash during upload is kept distinct from a paid creation.
                intent = {**metadata, "stage": "upload_unknown"}
                self._write_metadata(run_dir, intent)
                approved = self._update_run(run_dir, status=TaskStatus.APPROVED)
                try:
                    video_field = (
                        "reference_videos"
                        if self._is_standard_model(metadata["generation_model"])
                        else "referenceVideos"
                    )
                    for row in metadata["seedance_media"]["videos"]:
                        path = self.paths.root / row["source"]
                        source_root = self._seedance_derivative_root(metadata)
                        result = self.uploader.upload_file(path, allowed_root=source_root, expected_sha256=row["sha256"])
                        if result.sha256 != row["sha256"] or not _public_url(result.url):
                            raise ValueError("invalid upload result")
                        payload[video_field].append(result.url)
                except Exception:
                    raise WorkflowError("Seedance 上传结果未知；未提交视频创建，不会自动重传") from None
                # Upload can take time: recheck approval and sources before paid intent.
                if self._read_metadata(run_dir) != intent:
                    raise WorkflowError("上传期间审核记录发生变化，已停止创建")
                self._require_binding(metadata)
                self._verify_saved_prompt(run, metadata)
                self._prepare_current_assets(run, metadata)
                self._verify_reference_video_sources(metadata, run_dir)
                validate_payload(payload)
                intent["stage"] = "creation_unknown"
                self._write_metadata(run_dir, intent)
                try:
                    remote = self.client.create_video(payload)
                    task_id = self._safe_remote_identifier(remote.task_id)
                    if not task_id or remote.external_task_id:
                        raise ValueError("invalid creation identity")
                except Exception:
                    raise WorkflowError("Seedance 创建结果未知；没有可确认任务编号，只能人工核查，禁止自动重提") from None
                # Persist ID before any GET, even when create already says completed.
                approved = self._update_run(run_dir, status=TaskStatus.VIDEO_PROCESSING, video_task_id=task_id)
                intent["stage"] = "video_processing"
                self._write_metadata(run_dir, intent)
                if submit_only:
                    return approved
                remote = self._query_video_task(approved, task_id)
                return self._poll_download_and_validate(approved, remote)

    def _query_video_task(self, run, task_id):
        try:
            remote = self._as_video(self._client().query_video(task_id=task_id))
        except Exception:
            raise WorkflowError("Seedance 任务查询未完成；保留任务编号，不会重新创建") from None
        if remote.task_id != task_id:
            raise WorkflowError("Seedance 查询返回的任务编号不匹配，已停止")
        self._record_processing_step(
            run=run, role="query", reason="查询已登记的 Seedance 任务",
            result="completed" if remote.status == "succeed" else remote.status,
            evidence="任务供应商和模型已绑定",
        )
        if remote.status == "succeed":
            # The adapter's compatibility aliases are not a verified vendor
            # watermark contract. Do not persist authenticated watermark proof
            # or publish/download on the strength of those guesses.
            metadata = self._read_metadata(run.run_dir)
            metadata.update({"stage": "seedance_result_contract_pending", "remote_status": "completed"})
            self._write_metadata(run.run_dir, metadata)
            raise ProviderContractError(CONTRACT_BLOCKED)
        return remote

    def resume_batch(self, sku):
        """Resume every persisted Aimaxhug task for one SKU without creating.

        Each run remains independently locked and bound to its saved model and
        task ID. Unknown creation records are reported but never retried.
        """
        latest = self._find_latest_run(sku)
        if latest is None:
            raise WorkflowError("该款号没有可续查的本地任务")
        sku_dir = self.paths.results_for(sku) / sku
        tasks = []
        for run_dir in self._run_directories(sku_dir):
            with _locked(run_dir / ".workflow.lock"):
                run = self._load_run(run_dir)
                metadata = self._read_metadata(run_dir)
                if metadata.get("generation_provider") != "aimaxhug":
                    continue
                stage = metadata.get("stage")
                if not run.video_task_id:
                    if stage in {"creation_unknown", "upload_unknown"}:
                        tasks.append({
                            "version": run_dir.name,
                            "task_id": None,
                            "status": run.status.value,
                            "state": "unknown_without_task_id",
                        })
                    continue
                task_id = self._safe_remote_identifier(run.video_task_id)
                model = metadata.get("generation_model")
                if task_id != run.video_task_id:
                    tasks.append({
                        "version": run_dir.name,
                        "task_id": None,
                        "status": run.status.value,
                        "state": "invalid_binding",
                    })
                    continue
                if model not in SEEDANCE_MODELS:
                    tasks.append({
                        "version": run_dir.name,
                        "task_id": task_id,
                        "status": run.status.value,
                        "state": "invalid_binding",
                    })
                    continue
                if run.status in {TaskStatus.COMPLETED, TaskStatus.FAILED}:
                    tasks.append({
                        "version": run_dir.name,
                        "task_id": task_id,
                        "status": run.status.value,
                        "state": "finished",
                    })
                    continue
                try:
                    with self._remote_resources(model):
                        remote = self._query_video_task(run, task_id)
                        current = self._poll_download_and_validate(run, remote)
                    state = "finished" if current.status in {
                        TaskStatus.COMPLETED, TaskStatus.FAILED
                    } else "pending"
                except ProviderContractError as error:
                    if error.args != (CONTRACT_BLOCKED,):
                        raise
                    current = self.store.load(run_dir)
                    state = "delivery_blocked"
                except WorkflowError:
                    current = self.store.load(run_dir)
                    state = "query_pending"
                tasks.append({
                    "version": run_dir.name,
                    "task_id": task_id,
                    "status": current.status.value,
                    "state": state,
                })
        if not tasks:
            raise WorkflowError("该款号没有已登记的 Aimaxhug 任务可续查")
        return {
            "ok": all(item["state"] not in {
                "unknown_without_task_id", "invalid_binding", "query_pending"
            } for item in tasks),
            "sku": sku,
            "tasks": tasks,
        }

    def resume(self, sku):
        run_dir = self._find_latest_run(sku)
        if run_dir is None:
            raise WorkflowError("该款号没有可续查的本地任务")
        with _locked(run_dir / ".workflow.lock"):
            if self._find_latest_run(sku) != run_dir:
                raise WorkflowError("续查期间任务发生变化，请重新选择")
            run = self._load_run(run_dir)
            metadata = self._read_metadata(run_dir)
            if metadata.get("generation_provider") != "aimaxhug":
                raise ProviderContractError(LEGACY_TASK_BLOCKED)
            model = metadata.get("generation_model")
            if model not in SEEDANCE_MODELS:
                raise ProviderContractError(MODEL_BINDING_ERROR)
            if metadata.get("stage") in {"creation_unknown", "upload_unknown"} and not run.video_task_id:
                raise ProviderContractError(NO_TASK_ID)
            if run.status in {TaskStatus.COMPLETED, TaskStatus.FAILED,
                              TaskStatus.ANALYZING, TaskStatus.WAITING_APPROVAL}:
                return run
            if (not run.video_task_id
                    or self._safe_remote_identifier(run.video_task_id) != run.video_task_id):
                raise ProviderContractError(NO_TASK_ID)
            with self._remote_resources(model):
                remote = self._query_video_task(run, run.video_task_id)
                return self._poll_download_and_validate(run, remote)
