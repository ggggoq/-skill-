from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path


class TaskStatus(StrEnum):
    ANALYZING = "分析中"
    WAITING_APPROVAL = "等待用户确认"
    APPROVED = "已确认待提交"
    ELEMENT_SUBMITTING = "元素提交中"
    ELEMENT_PROCESSING = "元素生成中"
    VIDEO_SUBMITTING = "视频提交中"
    VIDEO_PROCESSING = "视频生成中"
    DOWNLOADING = "下载中"
    COMPLETED = "已完成"
    FAILED = "失败"
    TIMED_OUT = "超时待续查"


@dataclass(frozen=True)
class ShotPlan:
    index: int
    duration: int | float
    prompt: str

    def __post_init__(self) -> None:
        if (
            isinstance(self.duration, bool)
            or not isinstance(self.duration, (int, float))
            or not math.isfinite(float(self.duration))
            or self.duration <= 0
        ):
            raise ValueError("每段分镜至少 1 秒")


@dataclass(frozen=True)
class ProjectPaths:
    root: Path

    @property
    def materials(self) -> Path:
        return self.root / "01_素材库"

    @property
    def pending(self) -> Path:
        return self.root / "02_待审核"

    @property
    def approved(self) -> Path:
        return self.root / "03_审核通过"

    @property
    def history(self) -> Path:
        return self.root / "04_历史版本"

    @property
    def config(self) -> Path:
        return self.root / "05_系统配置"

    @property
    def final(self) -> Path:
        return self.root / "成片"

    @property
    def results(self) -> Path:
        return self.history if self.materials.is_dir() else self.root / "03生成结果"

    def results_for(self, sku: str) -> Path:
        if (self.materials / sku).is_dir():
            return self.history
        return self.root / "03生成结果"


@dataclass
class RunRecord:
    sku: str
    run_dir: Path
    status: TaskStatus
    prompt_sha256: str | None = None
    element_external_id: str | None = None
    element_task_id: str | None = None
    video_external_id: str | None = None
    video_task_id: str | None = None
