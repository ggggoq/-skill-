import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from kling_workflow.models import ProjectPaths, RunRecord, TaskStatus


_RECORD_NAME = "任务记录.json"
_VERSION_NAME = re.compile(r"^v(\d{3,})$")
_WINDOWS_INVALID_FILENAME_CHARACTERS = frozenset('<>:"/\\|?*')
_WINDOWS_RESERVED_DEVICE_NAMES = frozenset(
    (
        "CON",
        "PRN",
        "AUX",
        "NUL",
        "CONIN$",
        "CONOUT$",
        *(f"COM{digit}" for digit in range(1, 10)),
        *(f"LPT{digit}" for digit in range(1, 10)),
        "COM¹",
        "COM²",
        "COM³",
        "LPT¹",
        "LPT²",
        "LPT³",
    )
)


def _validate_sku_segment(sku: str) -> None:
    if not isinstance(sku, str) or not sku:
        raise ValueError("款号必须是有效的 Windows 单级目录名")

    reserved_stem = sku.split(".", maxsplit=1)[0].rstrip(" .").upper()
    if (
        sku in {".", ".."}
        or sku.endswith((" ", "."))
        or any(
            character in _WINDOWS_INVALID_FILENAME_CHARACTERS
            or ord(character) < 32
            for character in sku
        )
        or reserved_stem in _WINDOWS_RESERVED_DEVICE_NAMES
    ):
        raise ValueError("款号必须是有效的 Windows 单级目录名")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        _replace_write_through(temp, path)
        if os.name != "nt":
            with path.open("rb") as committed:
                os.fsync(committed.fileno())
            descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _replace_write_through(source: Path, target: Path) -> None:
    if os.name != "nt":
        os.replace(source, target)
        return

    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file = kernel32.MoveFileExW
    move_file.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move_file.restype = wintypes.BOOL
    replace_existing = 0x1
    write_through = 0x8
    if not move_file(str(source), str(target), replace_existing | write_through):
        error = ctypes.get_last_error()
        raise OSError(error, ctypes.FormatError(error), str(target))


def _record_path(run_dir: Path) -> Path:
    run_dir = Path(run_dir)
    if run_dir.parent.name == ".workflow" and _VERSION_NAME.fullmatch(run_dir.name):
        return run_dir.parent.parent / f"{run_dir.name}.json"
    return run_dir / _RECORD_NAME


def _write_exclusive_json(path: Path, payload: dict[str, object]) -> None:
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


class TaskStore:
    def __init__(self, paths: ProjectPaths) -> None:
        self.paths = paths

    def create_run(self, sku: str, *, now: datetime | None = None) -> RunRecord:
        _validate_sku_segment(sku)

        sku_dir = self.paths.results_for(sku) / sku
        sku_dir.mkdir(parents=True, exist_ok=True)
        new_layout = (self.paths.materials / sku).is_dir()
        if new_layout:
            run_dir = self._create_version_run(sku, sku_dir)
        else:
            timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
            run_dir = sku_dir / timestamp
            run_dir.mkdir(exist_ok=False)

        record = RunRecord(sku=sku, run_dir=run_dir, status=TaskStatus.ANALYZING)
        if new_layout:
            _write_exclusive_json(_record_path(run_dir), self._payload(record))
        else:
            _atomic_json(run_dir / _RECORD_NAME, self._payload(record))
        return record

    def _create_version_run(self, sku: str, history_sku_dir: Path) -> Path:
        bases = (
            self.paths.history / sku,
            self.paths.pending / sku,
            self.paths.approved / sku,
        )
        for _ in range(1000):
            used: set[int] = set()
            for base in bases:
                if not base.exists():
                    continue
                for path in base.rglob("*"):
                    for part in path.relative_to(base).parts:
                        match = _VERSION_NAME.fullmatch(part)
                        if match:
                            used.add(int(match.group(1)))
                    direct_file = re.fullmatch(r"v(\d+)\.(?:json|mp4)$", path.name, re.IGNORECASE)
                    approved_file = re.fullmatch(
                        re.escape(sku) + r"_v(\d+)\.mp4", path.name, re.IGNORECASE
                    )
                    for match in (direct_file, approved_file):
                        if match:
                            used.add(int(match.group(1)))
            version = f"v{max(used, default=0) + 1:03d}"
            workflow_dir = history_sku_dir / ".workflow"
            workflow_dir.mkdir(exist_ok=True)
            run_dir = workflow_dir / version
            try:
                run_dir.mkdir(exist_ok=False)
            except FileExistsError:
                continue
            return run_dir
        raise FileExistsError("无法排他式分配新版本号")

    def load(self, run_dir: Path) -> RunRecord:
        with _record_path(run_dir).open("r", encoding="utf-8") as stream:
            payload = json.load(stream)

        return RunRecord(
            sku=payload["sku"],
            run_dir=run_dir,
            status=TaskStatus(payload["status"]),
            prompt_sha256=payload.get("prompt_sha256"),
            element_external_id=payload.get("element_external_id"),
            element_task_id=payload.get("element_task_id"),
            video_external_id=payload.get("video_external_id"),
            video_task_id=payload.get("video_task_id"),
        )

    def update(
        self,
        run_dir: Path,
        *,
        status: TaskStatus | None = None,
        prompt_sha256: str | None = None,
        element_external_id: str | None = None,
        element_task_id: str | None = None,
        video_external_id: str | None = None,
        video_task_id: str | None = None,
    ) -> RunRecord:
        record = self.load(run_dir)
        for name, value in (
            ("status", status),
            ("prompt_sha256", prompt_sha256),
            ("element_external_id", element_external_id),
            ("element_task_id", element_task_id),
            ("video_external_id", video_external_id),
            ("video_task_id", video_task_id),
        ):
            if value is not None:
                setattr(record, name, value)

        expected = self._payload(record)
        _atomic_json(_record_path(run_dir), expected)
        committed = self.load(run_dir)
        if self._payload(committed) != expected:
            raise RuntimeError("任务记录写入后校验失败")
        return committed

    @staticmethod
    def _payload(record: RunRecord) -> dict[str, object]:
        return {
            "sku": record.sku,
            "run_dir": str(record.run_dir),
            "status": record.status.value,
            "prompt_sha256": record.prompt_sha256,
            "element_external_id": record.element_external_id,
            "element_task_id": record.element_task_id,
            "video_external_id": record.video_external_id,
            "video_task_id": record.video_task_id,
        }
