"""Project provider selection; contains no credentials or private media URLs."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import stat

from .workflow import WorkflowError, _reject_reparse_components
from .seedance_models import SEEDANCE_MODELS, supports_resolution


CONTRACT_BLOCKED = (
    "Seedance 完成结果合同尚未验证：下载字段及无水印证明仍待核对；"
    "已停止交付，请运行“检查接口”查看缺项"
)
CONFIG_INVALID = "视频接口配置无效；请检查供应商、模型和固定的视频要求，配置内不得保存凭据"
CONNECTION_UNDOCUMENTED = "Seedance 尚未提供独立只读连接检查合同；不会用付费生成测试连接"
LEGACY_TASK_BLOCKED = "旧任务供应商不是 Aimaxhug；禁止用新接口查询或重新提交旧任务"
MODEL_BINDING_ERROR = "Seedance 任务模型记录缺失或与查询客户端不一致；禁止猜测模型"
NO_TASK_ID = "Seedance 创建结果不明或任务编号缺失；只能人工核查，不会自动重新创建"
EXTERNAL_TASK_BLOCKED = "Aimaxhug 模式禁止登记旧外部任务；Seedance 任务必须由安全创建入口原生登记"
PROVIDER_SAFE_MESSAGES = frozenset({CONTRACT_BLOCKED, CONFIG_INVALID, CONNECTION_UNDOCUMENTED,
                                    LEGACY_TASK_BLOCKED, MODEL_BINDING_ERROR, NO_TASK_ID,
                                    EXTERNAL_TASK_BLOCKED})
VIDEO_CREATE_ENDPOINT = "https://apis.aimaxhug.cloud/v1/videos"
VIDEO_QUERY_ENDPOINT_TEMPLATE = "https://apis.aimaxhug.cloud/v1/videos/{model}/{task_id}"


class ProviderContractError(WorkflowError):
    """Safe static messages for unavailable provider contracts."""


@dataclass(frozen=True)
class ProviderConfig:
    provider: str = "aimaxhug"
    model: str = "Seedance-2.0-pro"
    resolution: str = "720p"
    aspect_ratio: str = "9:16"
    poll_interval_seconds: int = 10

    def status(self) -> dict[str, object]:
        if self.provider == "kling":
            return {"provider": "kling", "migration_configured": False}
        blockers = [
            "安全串行提交已启用；创建结果无 task_id 时整批停止且禁止自动重提",
            "已确认没有音频控制参数；原音或纯音乐仅可通过提示词提出，实际效果待验证",
            "当前标准通道文档使用 queued/processing/SUCCESS/failed；真实完整响应仍待核对",
            "服务端无水印证明字段或平台无水印保证仍待确认",
        ]
        return {
            "provider": self.provider, "model": self.model,
            "video_create_endpoint": VIDEO_CREATE_ENDPOINT,
            "video_query_endpoint_template": VIDEO_QUERY_ENDPOINT_TEMPLATE,
            "available_models": list(SEEDANCE_MODELS),
            "resolution": self.resolution, "aspect_ratio": self.aspect_ratio,
            "poll_interval_seconds": self.poll_interval_seconds,
            "model_25_compatible": True, "audio_parameter_supported": False,
            "ready_for_generation": False,
            "ready_for_batch_submission": True,
            "ready_for_delivery": False,
            "blockers": blockers,
        }


def _unique_json(pairs):
    data = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("duplicate configuration key")
        data[key] = value
    return data


def load_provider_config(root: Path) -> ProviderConfig:
    path = Path(root) / "04系统配置" / "视频接口.json"
    try:
        _reject_reparse_components(path)
        if not path.exists():
            # Deleting/misplacing the new configuration must never reactivate
            # the old paid transport or select the old credential namespace.
            return ProviderConfig()
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 8192:
            raise ValueError("invalid configuration file")
        raw = path.read_bytes()
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("configuration changed during read")
        data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_json)
        expected = {"provider", "model", "resolution", "aspect_ratio", "poll_interval_seconds"}
        if not isinstance(data, dict) or set(data) != expected:
            raise ValueError("unsupported configuration")
        if (data["provider"] != "aimaxhug"
                or data["model"] not in SEEDANCE_MODELS
                or not supports_resolution(data["model"], data["resolution"])
                or data["resolution"] != "720p"
                or data["aspect_ratio"] != "9:16"
                or type(data["poll_interval_seconds"]) is not int or data["poll_interval_seconds"] != 10):
            raise ValueError("invalid provider policy")
        return ProviderConfig(**data)
    except (OSError, ValueError, TypeError, UnicodeError, WorkflowError):
        raise ProviderContractError(CONFIG_INVALID) from None


def require_generation_contract() -> None:
    """Allow the verified create/query contract, not unverified delivery.

    The workflow still fails closed when creation has no task ID and when a
    completed response lacks an explicit, verified no-watermark result.
    """
    return None
