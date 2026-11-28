from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import gc
import hashlib
import json
import math
import os
import re
import stat
import sys
import threading
import tomllib
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from pathlib import PureWindowsPath
from typing import Mapping

from .client import BASE_URL, ConnectionResult, KlingClient, KlingError
from .connector_prompt import build_connector_prompt
from .prompting import PromptError
from .credentials import CredentialStore
from .importer import ImportError as FolderImportError
from .importer import (
    import_and_prepare,
    infer_sku,
    prepare_existing_reference_group,
    preflight_current_sku_folder,
    suggest_standard_names,
)
from .models import ProjectPaths, RunRecord, ShotPlan
from .processing_log import ProcessingLog, ProcessingLogError
from .reference_groups import (
    find_prepared_reference_group_runs,
    select_numbered_reference_groups,
)
from .task_store import TaskStore
from .validation import validate_video
from .workflow import Workflow, WorkflowError
from .provider_config import (
    load_provider_config, ProviderContractError, PROVIDER_SAFE_MESSAGES,
    CONNECTION_UNDOCUMENTED, EXTERNAL_TASK_BLOCKED, require_generation_contract,
)


_PROMPT_SHA = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_JSON_BYTES = 1024 * 1024
_OUTPUT_DIRS = (
    "01_素材库",
    "02_待审核",
    "03_审核通过",
    "04_历史版本",
    "05_系统配置",
    "06_处理日志",
    "成片",
    "04系统配置",
)
_REPARSE_ATTRIBUTE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_PROJECT_ROOT_ENV = "KLING_WORKFLOW_PROJECT_ROOT"
_PROJECT_NAME = "kling-image-to-video-workflow"
_KNOWN_COMMANDS = frozenset(
    {
        "init",
        "prepare",
        "suggest-names",
        "classify-sku",
        "select-reference-groups",
        "import-and-prepare",
        "reprepare-reference-group",
        "register-external-task",
        "complete-external-task",
        "resume-local-validation",
        "save-prompt",
        "record-role-step",
        "verify-no-watermark",
        "credential-status",
        "credential-set",
        "check-connection",
        "provider-status",
        "resolve-creation-unknown",
        "generate",
        "batch-generate",
        "approve",
        "reject",
        "resume",
    }
)
_DEPENDENCY_ERROR = "命令依赖异常退出，操作未完成"
_GENERIC_ERROR = "命令执行失败，请检查本地任务状态和输入文件"
_CANCELLED_ERROR = "操作已取消"
_PROCESSING_LOG_START_ERROR = "无法安全写入处理日志，操作未执行"
_PROCESSING_LOG_FINISH_ERROR = "处理可能已经执行，但结束日志写入失败；请核对本地任务记录，禁止自动重试"
_USAGE_ERROR = "命令参数无效，请按固定命令格式重新输入"
_PAID_CONFIRMATION_ERROR = "开始生成必须提供 --confirm-paid-request"
_BATCH_STOPPED_ERROR = "当前参考组未取得可安全续查的任务编号；批量已停止，后续参考组未提交"
_CONNECTION_FAILURE_BY_KIND = {
    "credential": "无法读取可灵凭据，请重新设置凭据",
    "auth": "可灵鉴权失败，请检查凭据和接口权限",
    "endpoint": "可灵北京接口不支持当前连接检查路径",
    "network": "无法连接可灵北京 API，请检查网络后重试",
    "invalid_response": "可灵连接检查返回的数据结构无效",
    "api_rejected": "可灵接口拒绝了连接检查请求",
    "http": "可灵连接检查收到异常 HTTP 响应",
}
_CLI_USAGE_MESSAGES = frozenset({_USAGE_ERROR, _PAID_CONFIRMATION_ERROR})
_CLI_FAILURE_MESSAGES = frozenset(
    {
        "配置的项目目录无效或包含链接/重解析点",
        "无法定位有效的可灵工作流项目目录",
        _DEPENDENCY_ERROR,
        _GENERIC_ERROR,
        "无法安全读取可灵凭据状态",
        "可灵连接检查返回的数据结构无效",
        _PAID_CONFIRMATION_ERROR,
        "提示词 SHA 必须是 64 位十六进制字符串",
        "不支持的固定命令",
        "本地任务返回的数据结构无效",
        "Seedance 本地深度预检不可用",
        *_CONNECTION_FAILURE_BY_KIND.values(),
        "项目目录不存在或包含链接/重解析点",
        "固定项目目录必须是普通目录",
        "固定项目目录创建时发生冲突",
        "无法创建固定项目目录",
        "固定项目目录创建后校验失败",
        "提示词 JSON 必须是当前任务目录内的普通本地文件",
        "动作分析 JSON 必须是非空对象",
        "分镜 JSON 必须包含 1 到 6 个镜头",
        "分镜 JSON 字段必须为 index、duration、prompt",
        "分镜 JSON 的编号、时长或提示词无效",
        "提示词 JSON 必须是普通文件",
        "提示词 JSON 超过本地读取上限",
        "提示词 JSON 在读取前发生变化",
        "无法安全读取提示词 JSON",
        "提示词 JSON 在读取过程中发生变化",
        "提示词 JSON 格式无效",
        "动作分析 JSON 包含无效数字",
        "动作分析 JSON 结构无效",
        "命令返回的数据结构无法安全序列化",
        "命令结果无法安全序列化",
        _PROCESSING_LOG_START_ERROR,
        _PROCESSING_LOG_FINISH_ERROR,
    }
)


class _CliUsageError(Exception):
    pass


class _CliFailure(Exception):
    pass


class _BatchSubmissionStopped(Exception):
    def __init__(self, result: dict[str, object]) -> None:
        super().__init__(_BATCH_STOPPED_ERROR)
        self.result = result


class _FixedArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        if "--confirm-paid-request" in message:
            raise _CliUsageError("开始生成必须提供 --confirm-paid-request")
        raise _CliUsageError("命令参数无效，请按固定命令格式重新输入")

    def exit(self, status: int = 0, message: str | None = None) -> None:
        raise _CliUsageError("命令参数无效，请按固定命令格式重新输入")


def _project_root() -> Path:
    configured = os.environ.get(_PROJECT_ROOT_ENV)
    if configured is not None:
        root = _validated_project_root(configured)
        if root is None:
            raise _CliFailure("配置的项目目录无效或包含链接/重解析点")
        return root

    source_file = Path(os.path.abspath(__file__))
    if len(source_file.parents) >= 3:
        source_root = _validated_project_root(source_file.parents[2])
        if source_root is not None:
            return source_root

    cwd_root = _validated_project_root(Path.cwd())
    if cwd_root is not None:
        return cwd_root
    raise _CliFailure("无法定位有效的可灵工作流项目目录")


def _validated_project_root(candidate: str | os.PathLike[str]) -> Path | None:
    try:
        raw = os.fspath(candidate)
        if not isinstance(raw, str) or not raw.strip():
            return None
        windows_path = PureWindowsPath(raw)
        if windows_path.drive.startswith("\\\\"):
            return None
        path = Path(raw)
        if not path.is_absolute() or ".." in path.parts:
            return None
        if os.name == "nt":
            drive, _tail = os.path.splitdrive(os.fspath(path))
            if not drive or drive.startswith("\\\\") or not _is_local_project_path(path):
                return None
        _reject_reparse_components(path)
        metadata = path.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or _is_reparse(metadata):
            return None
        root = path.resolve(strict=True)

        pyproject = root / "pyproject.toml"
        _reject_reparse_components(pyproject)
        pyproject_metadata = pyproject.lstat()
        if _is_reparse(pyproject_metadata) or not stat.S_ISREG(pyproject_metadata.st_mode):
            return None
        if pyproject_metadata.st_size > 256 * 1024:
            return None
        project = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        if not isinstance(project, dict) or project.get("project", {}).get("name") != _PROJECT_NAME:
            return None
        if not _safe_existing_directory(root / "src" / "kling_workflow"):
            return None
        if not _safe_existing_directory(root / "04系统配置"):
            return None
        return root
    except Exception:
        return None


def _is_local_project_path(path: Path) -> bool:
    if os.name != "nt":
        return True
    try:
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(path.anchor))
        return drive_type not in {0, 1, 4}
    except Exception:
        return False


def _make_credential_store() -> CredentialStore:
    return CredentialStore()


def _make_client(store: CredentialStore) -> KlingClient:
    return KlingClient(BASE_URL, api_key_provider=store.read)


def _make_workflow(
    root: Path,
    store: CredentialStore | None = None,
    client: KlingClient | None = None,
    *,
    online: bool = False,
    local_validation: bool = False,
    processing_log: ProcessingLog | None = None,
) -> Workflow:
    paths = ProjectPaths(Path(root))
    def _validate_candidate(path, expected_aspect_ratio=9 / 16, require_audio=False):
        kwargs = {
            "expected_aspect_ratio": expected_aspect_ratio,
            "write_report": False,
        }
        if require_audio:
            kwargs["require_audio"] = True
        return validate_video(path, **kwargs)

    provider_config = load_provider_config(root)
    workflow_class = Workflow
    provider_kwargs = {}
    if provider_config.provider == "aimaxhug":
        from .seedance_workflow import SeedanceWorkflow
        workflow_class = SeedanceWorkflow
        provider_kwargs["provider_config"] = provider_config
    return workflow_class(
        paths,
        store=TaskStore(paths),
        credential_store=store,
        client=client,
        validator=(
            _validate_candidate
        ) if online or local_validation else None,
        processing_log=processing_log,
        **provider_kwargs,
    )


@contextmanager
def _generation_workflow(root, provider_config, processing_log):
    if provider_config.provider == "aimaxhug":
        # The Seedance coordinator owns its lazy client and uploader. Even after
        # the contract gate is opened, no legacy credential factory is reached.
        yield _make_workflow(root, online=True, processing_log=processing_log)
        return
    store = _make_credential_store()
    client = _make_client(store)
    try:
        yield _make_workflow(root, store, client, online=True, processing_log=processing_log)
    finally:
        _close_client(client)


def _build_parser() -> argparse.ArgumentParser:
    parser = _FixedArgumentParser(prog="kling-workflow", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True, parser_class=_FixedArgumentParser)

    sub.add_parser("init", add_help=False)

    prepare = sub.add_parser("prepare", add_help=False)
    prepare.add_argument("--sku", required=True)
    prepare.add_argument("--from", dest="from_version")

    suggest = sub.add_parser("suggest-names", add_help=False)
    suggest.add_argument("--source", type=Path, required=True)
    suggest.add_argument("--sku")
    suggest.add_argument("--roles-json", required=True)

    classify = sub.add_parser("classify-sku", add_help=False)
    classify.add_argument("--sku", required=True)

    select_groups = sub.add_parser("select-reference-groups", add_help=False)
    select_groups.add_argument("--sku", required=True)
    select_groups.add_argument("--number")

    import_prepare = sub.add_parser("import-and-prepare", add_help=False)
    import_prepare.add_argument("--source", type=Path, required=True)
    import_prepare.add_argument("--sku", required=True)
    import_prepare.add_argument("--roles-json")

    reprepare_group = sub.add_parser("reprepare-reference-group", add_help=False)
    reprepare_group.add_argument("--manifest", type=Path, required=True)
    reprepare_group.add_argument("--sku", required=True)

    external = sub.add_parser("register-external-task", add_help=False)
    external.add_argument("--sku", required=True)
    external.add_argument("--version", required=True)
    external.add_argument("--prompt-sha", required=True)
    external.add_argument("--task-id", required=True)
    external.add_argument("--external-task-id", required=True)
    external.add_argument("--provider", choices=("mcp", "api"), required=True)
    external.add_argument("--confirm-paid-request", action="store_true", required=True)

    external_result = sub.add_parser("complete-external-task", add_help=False)
    external_result.add_argument("--sku", required=True)
    external_result.add_argument("--version", required=True)
    external_result.add_argument("--task-id", required=True)
    external_result.add_argument("--video-path", type=Path, required=True)

    local_validation = sub.add_parser("resume-local-validation", add_help=False)
    local_validation.add_argument("--sku", required=True)
    local_validation.add_argument("--version", required=True)
    local_validation.add_argument("--task-id")

    save = sub.add_parser("save-prompt", add_help=False)
    save.add_argument("--run", type=Path, required=True)
    save.add_argument("--analysis", type=Path, required=True)
    save.add_argument("--shots", type=Path, required=True)

    role_step = sub.add_parser("record-role-step", add_help=False)
    role_step.add_argument("--sku", required=True)
    role_step.add_argument("--version", required=True)
    role_step.add_argument("--model", choices=("Luna", "Sol", "Web", "local"), required=True)
    role_step.add_argument("--role", choices=("initial_draft", "review", "source_lookup", "query", "download"), required=True)
    role_step.add_argument("--reason", required=True)
    role_step.add_argument("--result", required=True)
    role_step.add_argument("--evidence", default="")

    watermark_review = sub.add_parser("verify-no-watermark", add_help=False)
    watermark_review.add_argument("--run", type=Path, required=True)
    watermark_review.add_argument("--evidence", required=True)

    sub.add_parser("credential-status", add_help=False)
    sub.add_parser("credential-set", add_help=False)
    sub.add_parser("check-connection", add_help=False)
    sub.add_parser("provider-status", add_help=False)

    resolve_unknown = sub.add_parser("resolve-creation-unknown", add_help=False)
    resolve_unknown.add_argument("--sku", required=True)
    resolve_unknown.add_argument("--version", required=True)
    resolve_unknown.add_argument(
        "--confirm-no-task-no-charge", action="store_true", required=True
    )

    generate = sub.add_parser("generate", add_help=False)
    generate.add_argument("--sku", required=True)
    generate.add_argument("--prompt-sha", required=True)
    generate.add_argument("--confirm-paid-request", action="store_true", required=True)

    batch_generate = sub.add_parser("batch-generate", add_help=False)
    batch_generate.add_argument("--sku", required=True)
    batch_generate.add_argument("--number")
    batch_generate.add_argument("--confirm-paid-request", action="store_true", required=True)

    refresh_prompt = sub.add_parser("refresh-prompt", add_help=False)
    refresh_prompt.add_argument("--sku", required=True)
    refresh_prompt.add_argument("--version", required=True)

    audio_gate = sub.add_parser("audio-gate", add_help=False)
    audio_gate.add_argument("--sku", required=True)
    audio_gate.add_argument("--version", required=True)
    audio_gate.add_argument("--video-path", type=Path)
    audio_gate.add_argument("--ffmpeg")

    connector_prompt = sub.add_parser("connector-prompt", add_help=False)
    connector_prompt.add_argument("--sku", required=True)
    connector_prompt.add_argument("--version", required=True)
    connector_prompt.add_argument("--element-id", required=True)

    record_review = sub.add_parser("record-review", add_help=False)
    record_review.add_argument("--sku", required=True)
    record_review.add_argument("--version", required=True)
    record_review.add_argument("--record", type=Path, required=True)

    approve = sub.add_parser("approve", add_help=False)
    approve.add_argument("--sku", required=True)
    approve.add_argument("--version", required=True)

    reject = sub.add_parser("reject", add_help=False)
    reject.add_argument("--sku", required=True)
    reject.add_argument("--version", required=True)
    reject.add_argument("--reason", required=True)

    resume = sub.add_parser("resume", add_help=False)
    resume.add_argument("--sku", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    exit_code, output_line, error_line = _execute_with_unraisable_guard(argv)
    if output_line is not None:
        _write_line(sys.stdout, output_line)
    if error_line is not None:
        _write_error(error_line)
    return exit_code


def _execute_with_unraisable_guard(
    argv: list[str] | None,
) -> tuple[int, str | None, str | None]:
    previous_hook = sys.unraisablehook
    cli_thread_id = threading.get_ident()
    observed = [False]

    def scoped_unraisable_hook(details: object) -> None:
        if threading.get_ident() == cli_thread_id:
            observed[0] = True
            return
        previous_hook(details)

    outcome: tuple[int, str | None, str | None] = (2, None, _GENERIC_ERROR)
    parser: argparse.ArgumentParser | None = None
    args: argparse.Namespace | None = None
    result: object | None = None
    output_line: str | None = None
    processing_log: ProcessingLog | None = None
    command_argv = tuple(sys.argv[1:] if argv is None else argv)
    try:
        sys.unraisablehook = scoped_unraisable_hook
        try:
            try:
                root = _project_root()
                processing_log = _start_processing_log(root, command_argv)
                parser = _build_parser()
                args = parser.parse_args(command_argv)
                result = _dispatch(args, root=root, processing_log=processing_log)
                output_line = _json_line(result)
                outcome = (0, output_line, None)
            except _CliUsageError as error:
                message = _safe_cli_message(error, _CLI_USAGE_MESSAGES, _USAGE_ERROR)
                outcome = (2, None, message)
            except _CliFailure as error:
                message = _safe_cli_message(error, _CLI_FAILURE_MESSAGES, _GENERIC_ERROR)
                outcome = (2, None, message)
            except ProviderContractError as error:
                message = _safe_cli_message(error, PROVIDER_SAFE_MESSAGES, _GENERIC_ERROR)
                outcome = (2, None, message)
            except _BatchSubmissionStopped as error:
                outcome = (2, _json_line(error.result), _BATCH_STOPPED_ERROR)
            except FolderImportError as error:
                message = str(error).strip()
                if not message or len(message) > 512 or "\\" in message or ":\\" in message:
                    message = _GENERIC_ERROR
                outcome = (2, None, message)
            except SystemExit:
                outcome = (2, None, _DEPENDENCY_ERROR)
            except KeyboardInterrupt:
                outcome = (2, None, _CANCELLED_ERROR)
            except ProcessingLogError:
                outcome = (2, None, _PROCESSING_LOG_START_ERROR)
            except BaseException:
                outcome = (2, None, _GENERIC_ERROR)
        finally:
            parser = None
            args = None
            result = None
            output_line = None
            argv = None
            command_argv = ()
            try:
                gc.collect()
            except BaseException:
                outcome = (2, None, _GENERIC_ERROR)
            if processing_log is not None:
                try:
                    processing_log.finish(
                        status=_processing_status(outcome),
                        exit_code=outcome[0],
                    )
                except ProcessingLogError:
                    outcome = (2, None, _PROCESSING_LOG_FINISH_ERROR)
    finally:
        sys.unraisablehook = previous_hook

    if observed[0]:
        return (2, None, _DEPENDENCY_ERROR)
    return outcome


def _safe_arg_value(argv: tuple[str, ...], flag: str, pattern: re.Pattern[str]) -> str | None:
    try:
        index = argv.index(flag)
        value = argv[index + 1]
    except (ValueError, IndexError):
        return None
    if type(value) is not str or not pattern.fullmatch(value):
        return None
    return value


def _start_processing_log(root: Path, argv: tuple[str, ...]) -> ProcessingLog:
    requested_command = argv[0] if argv else "unknown"
    command = requested_command if requested_command in _KNOWN_COMMANDS else "unknown"
    paid_commands = {"generate", "batch-generate", "register-external-task"}
    read_only_commands = {"check-connection", "resume"}
    if command in paid_commands:
        network_mode = "paid"
    elif command in read_only_commands:
        network_mode = "read_only"
    else:
        network_mode = "offline"
    return ProcessingLog.start(
        root,
        command=command,
        sku=_safe_arg_value(argv, "--sku", re.compile(r"^[A-Za-z0-9_-]{1,64}$")),
        version=_safe_arg_value(
            argv, "--version", re.compile(r"^(?:v[0-9]{3,6}|[0-9]{8}-[0-9]{6})$")
        ),
        network_mode=network_mode,
        paid_authorization_present=(
            command in paid_commands and "--confirm-paid-request" in argv
        ),
    )


def _processing_status(outcome: tuple[int, str | None, str | None]) -> str:
    exit_code, _output, error = outcome
    if exit_code == 0:
        return "success"
    if error == _CANCELLED_ERROR:
        return "cancelled"
    if error in _CLI_USAGE_MESSAGES:
        return "refused"
    return "failure"


def _safe_cli_message(
    error: BaseException,
    allowed_messages: frozenset[str],
    fallback: str,
) -> str:
    arguments = error.args
    if type(arguments) is tuple and len(arguments) == 1 and type(arguments[0]) is str:
        message = arguments[0]
        if message in allowed_messages:
            return message
    return fallback


def _preflight_generation(
    root: Path, sku: str, requested_number: str | None
) -> dict[str, object]:
    """Resolve numbered groups and their waiting runs before reading credentials."""

    result = find_prepared_reference_group_runs(
        root / "01_素材库",
        root / "04_历史版本",
        sku,
        requested_number=requested_number,
    )
    if not result.get("legacy"):
        return result

    # Keep the pre-existing flat-SKU layout working without weakening the
    # numbered-group isolation rule.  This local lookup performs no network IO.
    try:
        workflow = _make_workflow(root)
        run_dir = workflow._find_unique_waiting_run(sku)
        run = workflow._load_run(run_dir)
    except Exception:
        result["missing_preparation"] = [sku]
        return result
    prompt_sha = getattr(run, "prompt_sha256", None)
    if not isinstance(prompt_sha, str) or not _PROMPT_SHA.fullmatch(prompt_sha):
        result["missing_preparation"] = [sku]
        return result
    result.update(
        {
            "ready": True,
            "candidates": [
                {
                    "number": None,
                    "name": sku,
                    "path": str((root / "01_素材库" / sku).resolve()),
                    "group_id": "legacy",
                    "manifest": None,
                    "version": run_dir.name,
                    "run_dir": str(run_dir),
                    "prompt_sha": prompt_sha,
                }
            ],
            "missing_preparation": [],
            "legacy": True,
        }
    )
    return result


def _stop_batch_submission(
    preflight: dict[str, object], candidate: Mapping[str, object]
) -> None:
    preflight["ok"] = False
    preflight["stopped_at"] = {
        key: candidate[key]
        for key in ("number", "name", "group_id", "version")
    }
    preflight["stop_reason"] = _BATCH_STOPPED_ERROR
    raise _BatchSubmissionStopped(preflight)


def _batch_candidate_context(
    preflight: Mapping[str, object], candidate: Mapping[str, object]
) -> tuple[str, tuple[str, ...]]:
    number = candidate.get("number")
    group_id = candidate.get("group_id")
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number < 1
        or not isinstance(group_id, str)
        or not re.fullmatch(r"group-\d{4,}", group_id)
    ):
        raise WorkflowError("批量参考组顺序或身份无效")

    raw_order = preflight.get("group_order")
    if not isinstance(raw_order, list) or not raw_order:
        raw_order = preflight.get("candidates")
    if not isinstance(raw_order, list):
        raise WorkflowError("批量参考组顺序缺失")
    mapped: dict[int, str] = {}
    for item in raw_order:
        if not isinstance(item, Mapping):
            raise WorkflowError("批量参考组顺序结构无效")
        item_number = item.get("number")
        item_group = item.get("group_id")
        if (
            not isinstance(item_number, int)
            or isinstance(item_number, bool)
            or item_number < 1
            or not isinstance(item_group, str)
            or not re.fullmatch(r"group-\d{4,}", item_group)
            or item_number in mapped
        ):
            raise WorkflowError("批量参考组顺序结构无效")
        mapped[item_number] = item_group
    if mapped.get(number) != group_id:
        raise WorkflowError("当前参考组与批次顺序记录不匹配")

    raw_numbers = preflight.get("group_numbers")
    if isinstance(raw_numbers, list) and raw_numbers:
        if any(
            not isinstance(item, int) or isinstance(item, bool) or item < 1
            for item in raw_numbers
        ):
            raise WorkflowError("全部参考组编号记录无效")
        lower_numbers = sorted({item for item in raw_numbers if item < number})
    else:
        lower_numbers = sorted(item for item in mapped if item < number)
    if any(item not in mapped for item in lower_numbers):
        raise WorkflowError("前序参考组身份记录缺失，禁止跳组提交")
    return group_id, tuple(mapped[item] for item in lower_numbers)


def _record_preflight_failure(
    preflight: dict[str, object], candidate: Mapping[str, object]
) -> dict[str, object]:
    preflight["ok"] = False
    preflight["ready"] = False
    preflight["preflight_failed_at"] = {
        key: candidate[key]
        for key in ("number", "name", "group_id", "version")
    }
    preflight["preflight_error"] = "本地深度预检失败；未读取凭据、未上传、未提交任务"
    return preflight


def _dispatch(
    args: argparse.Namespace,
    *,
    root: Path | None = None,
    processing_log: ProcessingLog | None = None,
) -> dict[str, object]:
    root = _project_root() if root is None else root
    command = args.command

    provider_config = load_provider_config(root)
    if command == "provider-status":
        return provider_config.status()
    if provider_config.provider == "aimaxhug":
        # Paid transport gaps are reported before selecting a client/credential.
        if command in {"generate", "batch-generate"}:
            if args.confirm_paid_request is not True:
                raise _CliFailure(_PAID_CONFIRMATION_ERROR)
            require_generation_contract()
        if command == "check-connection":
            raise ProviderContractError(CONNECTION_UNDOCUMENTED)
        if command == "register-external-task":
            raise ProviderContractError(EXTERNAL_TASK_BLOCKED)
        if command in {"credential-status", "credential-set"}:
            from .seedance_credentials import SeedanceCredentialStore
            store = SeedanceCredentialStore()
            if command == "credential-set":
                store.set_interactive()
                return {"ok": True, "message": "凭据设置完成"}
            status = store.status()
            if set(status) != {"api-key"} or status["api-key"] not in {"PRESENT", "ABSENT"}:
                raise _CliFailure("无法安全读取可灵凭据状态")
            return status
        if command == "resolve-creation-unknown":
            workflow = _make_workflow(root, processing_log=processing_log)
            record = workflow.resolve_creation_unknown(
                args.sku,
                args.version,
                confirmed_no_task_no_charge=args.confirm_no_task_no_charge,
            )
            return _run_record_result(record)
        if command == "resume":
            workflow = _make_workflow(root, online=True, processing_log=processing_log)
            resume_batch = getattr(workflow, "resume_batch", None)
            if callable(resume_batch):
                return resume_batch(args.sku)
            return _run_record_result(workflow.resume(args.sku))

    if command == "init":
        return {"ok": True, "directories": [str(path) for path in _initialize_directories(root)]}

    if command == "suggest-names":
        try:
            roles = json.loads(args.roles_json)
        except (TypeError, ValueError):
            raise FolderImportError("素材角色映射 JSON 格式无效") from None
        if not isinstance(roles, Mapping) or any(
            not isinstance(name, str) or not isinstance(role, str)
            for name, role in roles.items()
        ):
            raise FolderImportError("素材角色映射必须是文件名到角色的对象")
        sku = args.sku or infer_sku(args.source)
        suggestions = suggest_standard_names(
            args.source,
            sku,
            roles,
            materials_root=root / "01_素材库",
        )
        copyable = [
            f"{name} -> {suggestions[name]}"
            for name in sorted(suggestions, key=lambda item: (item.casefold(), item))
        ]
        pending = sum(value == "待确认" for value in suggestions.values())
        return {
            "ok": True,
            "sku": sku,
            "total": len(suggestions),
            "identified": len(suggestions) - pending,
            "pending": pending,
            "suggestions": suggestions,
            "copyable_names": copyable,
        }

    if command == "classify-sku":
        categories = preflight_current_sku_folder(root / "01_素材库", args.sku)
        serialized: dict[str, list[object]] = {}
        for category, entries in categories.items():
            serialized[category] = [
                {
                    "name": Path(entry["path"]).name,
                    "media_type": entry["media_type"],
                }
                if isinstance(entry, Mapping)
                else Path(entry).name
                for entry in entries
            ]
        return {"ok": True, "sku": args.sku, "classifications": serialized}

    if command == "select-reference-groups":
        return select_numbered_reference_groups(
            root / "01_素材库", args.sku, requested_number=args.number
        )

    if command == "import-and-prepare":
        workflow = _make_workflow(root, processing_log=processing_log)
        try:
            roles = None
            if args.roles_json is not None:
                try:
                    roles = json.loads(args.roles_json)
                except (TypeError, ValueError):
                    raise FolderImportError("素材角色映射 JSON 格式无效") from None
                if not isinstance(roles, Mapping) or any(
                    not isinstance(name, str) or not isinstance(role, str)
                    for name, role in roles.items()
                ):
                    raise FolderImportError("素材角色映射必须是文件名到角色的对象")
            return import_and_prepare(
                args.source,
                args.sku,
                workflow,
                role_assignments=roles,
            )
        except FolderImportError:
            raise
        except Exception as error:
            raise _CliFailure("素材导入或离线分析失败；素材已保留，请检查任务记录") from error

    if command == "reprepare-reference-group":
        workflow = _make_workflow(root, processing_log=processing_log)
        try:
            return prepare_existing_reference_group(args.manifest, args.sku, workflow)
        except FolderImportError:
            raise
        except Exception as error:
            raise _CliFailure("参考组重新分析失败；原素材与历史版本已保留") from error

    if command == "register-external-task":
        if args.confirm_paid_request is not True:
            raise _CliFailure("开始生成必须提供 --confirm-paid-request")
        if not isinstance(args.prompt_sha, str) or not _PROMPT_SHA.fullmatch(args.prompt_sha):
            raise _CliFailure("提示词 SHA 必须是 64 位十六进制字符串")
        workflow = _make_workflow(root, processing_log=processing_log)
        record = workflow.register_external_task(
            sku=args.sku,
            version=args.version,
            prompt_sha256=args.prompt_sha.lower(),
            task_id=args.task_id,
            external_task_id=args.external_task_id,
            provider=args.provider,
            paid_confirmation=True,
        )
        return _run_record_result(record)

    if command == "complete-external-task":
        workflow = _make_workflow(root, local_validation=True, processing_log=processing_log)
        record = workflow.complete_external_task(
            sku=args.sku,
            version=args.version,
            task_id=args.task_id,
            video_path=args.video_path,
        )
        return _run_record_result(record)

    if command == "resume-local-validation":
        workflow = _make_workflow(root, local_validation=True, processing_log=processing_log)
        return _run_record_result(
            workflow.resume_local_validation(args.sku, args.version, task_id=args.task_id)
        )

    if command == "credential-status":
        store = _make_credential_store()
        status = store.status()
        if set(status) != {"api-key"} or any(
            value not in {"PRESENT", "ABSENT"} for value in status.values()
        ):
            raise _CliFailure("无法安全读取可灵凭据状态")
        return {"api-key": status["api-key"]}

    if command == "credential-set":
        _make_credential_store().set_interactive()
        return {"ok": True, "message": "凭据设置完成"}

    if command == "prepare":
        workflow = _make_workflow(root, processing_log=processing_log)
        run_dir = (
            workflow.prepare(args.sku, from_version=args.from_version)
            if args.from_version is not None
            else workflow.prepare(args.sku)
        )
        return {"ok": True, "run_dir": str(run_dir)}

    if command == "save-prompt":
        analysis, shots = _load_prompt_inputs(root, args.run, args.analysis, args.shots)
        workflow = _make_workflow(root, processing_log=processing_log)
        record = workflow.save_prompt(args.run, analysis, shots)
        return _run_record_result(record)

    if command == "record-role-step":
        workflow = _make_workflow(root, processing_log=processing_log)
        event = workflow.record_role_step(
            sku=args.sku, version=args.version, model=args.model, role=args.role,
            reason=args.reason, result=args.result, evidence=args.evidence,
        )
        return {"ok": True, "role_event": event}

    if command == "verify-no-watermark":
        workflow = _make_workflow(root, processing_log=processing_log)
        review = workflow.verify_no_watermark(args.run, evidence=args.evidence)
        return {"ok": True, "watermark_review": review}

    if command == "check-connection":
        store = _make_credential_store()
        client = _make_client(store)
        try:
            result = client.check_connection()
        except KlingError as error:
            kind = error.kind if type(error.kind) is str else None
            message = _CONNECTION_FAILURE_BY_KIND.get(kind, _GENERIC_ERROR)
            try:
                _close_client(client)
            except BaseException:
                pass
            raise _CliFailure(message) from None
        except BaseException:
            try:
                _close_client(client)
            except BaseException:
                pass
            raise
        else:
            _close_client(client)
        if type(result) is not ConnectionResult:
            raise _CliFailure("可灵连接检查返回的数据结构无效")
        return {
            "ok": True,
            "connected": True,
            "element_count": result.element_count,
        }

    if command == "generate":
        if args.confirm_paid_request is not True:
            raise _CliFailure("开始生成必须提供 --confirm-paid-request")
        if not isinstance(args.prompt_sha, str) or not _PROMPT_SHA.fullmatch(args.prompt_sha):
            raise _CliFailure("提示词 SHA 必须是 64 位十六进制字符串")
        with _generation_workflow(root, provider_config, processing_log) as workflow:
            record = workflow.generate(args.sku, args.prompt_sha.lower(), paid_confirmation=True)
            return _run_record_result(record)

    if command == "batch-generate":
        if args.confirm_paid_request is not True:
            raise _CliFailure("开始生成必须提供 --confirm-paid-request")
        preflight = _preflight_generation(root, args.sku, args.number)
        preflight["submitted"] = []
        if not preflight.get("ready"):
            return preflight
        candidates = preflight.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise _CliFailure("没有可提交的待确认参考组任务")
        with _generation_workflow(root, provider_config, processing_log) as workflow:
            contexts: list[tuple[str, tuple[str, ...]]] = []
            if provider_config.provider == "aimaxhug":
                deep_preflight = getattr(workflow, "preflight_generate", None)
                if not callable(deep_preflight):
                    raise _CliFailure("Seedance 本地深度预检不可用")
                for candidate in candidates:
                    if not isinstance(candidate, Mapping):
                        raise _CliFailure("批量参考组任务结构无效")
                    try:
                        group_id, predecessors = _batch_candidate_context(
                            preflight, candidate
                        )
                        deep_preflight(
                            args.sku,
                            str(candidate["prompt_sha"]).lower(),
                            version=str(candidate["version"]),
                            batch_group_id=group_id,
                            predecessor_group_ids=predecessors,
                        )
                    except WorkflowError:
                        return _record_preflight_failure(preflight, candidate)
                    contexts.append((group_id, predecessors))
            else:
                contexts = [("", ()) for _ in candidates]

            for candidate, (group_id, predecessors) in zip(
                candidates, contexts, strict=True
            ):
                if not isinstance(candidate, Mapping):
                    raise _CliFailure("批量参考组任务结构无效")
                options: dict[str, object] = {"version": str(candidate["version"])}
                if provider_config.provider == "aimaxhug":
                    options["submit_only"] = True
                    options["batch_group_id"] = group_id
                    options["predecessor_group_ids"] = predecessors
                try:
                    record = workflow.generate(
                        args.sku,
                        str(candidate["prompt_sha"]).lower(),
                        paid_confirmation=True,
                        **options,
                    )
                except WorkflowError:
                    _stop_batch_submission(preflight, candidate)
                task_id = getattr(record, "video_task_id", None)
                if (
                    provider_config.provider == "aimaxhug"
                    and (
                        not isinstance(task_id, str)
                        or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}", task_id)
                    )
                ):
                    _stop_batch_submission(preflight, candidate)
                preflight["submitted"].append(
                    {
                        "number": candidate["number"],
                        "name": candidate["name"],
                        "group_id": candidate["group_id"],
                        "version": candidate["version"],
                        "run_dir": str(getattr(record, "run_dir", candidate["run_dir"])),
                        "status": getattr(getattr(record, "status", None), "value", None),
                        "task_id": task_id,
                    }
                )
            preflight["mode"] = "batch" if not preflight.get("legacy") else "legacy"
            preflight["ok"] = True
            return preflight
    if command == "refresh-prompt":
        workflow = _make_workflow(root, processing_log=processing_log)
        return workflow.refresh_prompt_version(sku=args.sku, version=args.version)

    if command == "audio-gate":
        workflow = _make_workflow(root, processing_log=processing_log)
        return workflow.check_audio_gate(
            sku=args.sku,
            version=args.version,
            path=args.video_path,
            ffmpeg=args.ffmpeg,
        )

    if command == "connector-prompt":
        run_dir = root / "04_历史版本" / args.sku / ".workflow" / args.version
        prompt_path = run_dir / "可灵MCP提交提示词.txt"
        try:
            markdown = prompt_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            raise _CliFailure("找不到已审核的提交提示词") from None
        try:
            text = build_connector_prompt(markdown, args.element_id)
        except PromptError as exc:
            raise _CliFailure(str(exc)) from None
        target = run_dir / "可灵MCP提交提示词-连接器精简版.txt"
        target.write_text(text, encoding="utf-8", newline="\n")
        return {
            "ok": True,
            "sku": args.sku,
            "version": args.version,
            "element_id": args.element_id,
            "prompt_chars": len(text),
            "prompt_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "file": target.name,
        }

    if command == "record-review":
        try:
            review = json.loads(args.record.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _CliUsageError("复审记录文件无法读取或 JSON 格式无效") from None
        if not isinstance(review, Mapping):
            raise _CliUsageError("复审记录必须是 JSON 对象")
        workflow = _make_workflow(root, processing_log=processing_log)
        return workflow.record_ai_review(sku=args.sku, version=args.version, review=review)

    if command == "approve":
        workflow = _make_workflow(root, processing_log=processing_log)
        return workflow.approve(args.sku, args.version)

    if command == "reject":
        workflow = _make_workflow(root, processing_log=processing_log)
        return workflow.reject(args.sku, args.version, args.reason)

    if command == "resume":
        store = _make_credential_store()
        client = _make_client(store)
        try:
            workflow = _make_workflow(root, store, client, online=True, processing_log=processing_log)
            return _run_record_result(workflow.resume(args.sku))
        finally:
            _close_client(client)

    raise _CliFailure("不支持的固定命令")


def _run_record_result(record: RunRecord | object) -> dict[str, object]:
    if isinstance(record, RunRecord):
        run_dir = record.run_dir
        status = record.status.value
    else:
        run_dir = getattr(record, "run_dir", None)
        status_value = getattr(record, "status", None)
        status = status_value.value if isinstance(status_value, Enum) else status_value
    if not isinstance(run_dir, (str, Path)):
        raise _CliFailure("本地任务返回的数据结构无效")
    result: dict[str, object] = {"ok": True, "run_dir": str(run_dir)}
    if isinstance(status, str):
        result["status"] = status
    return result


def _initialize_directories(root: Path) -> tuple[Path, ...]:
    root = Path(root)
    if not _safe_existing_directory(root):
        raise _CliFailure("项目目录不存在或包含链接/重解析点")
    directories: list[Path] = []
    for name in _OUTPUT_DIRS:
        target = root / name
        if target.exists() or target.is_symlink():
            if not _safe_existing_directory(target):
                raise _CliFailure("固定项目目录必须是普通目录")
        else:
            try:
                target.mkdir()
            except FileExistsError:
                if not _safe_existing_directory(target):
                    raise _CliFailure("固定项目目录创建时发生冲突") from None
            except OSError:
                raise _CliFailure("无法创建固定项目目录") from None
            if not _safe_existing_directory(target):
                raise _CliFailure("固定项目目录创建后校验失败")
        directories.append(target)
    return tuple(directories)


def _safe_existing_directory(path: Path) -> bool:
    try:
        _reject_reparse_components(path)
        metadata = path.lstat()
        return stat.S_ISDIR(metadata.st_mode) and not _is_reparse(metadata)
    except OSError:
        return False


def _load_prompt_inputs(
    root: Path,
    run_dir: Path,
    analysis_path: Path,
    shots_path: Path,
) -> tuple[dict[str, object], list[ShotPlan]]:
    try:
        _reject_reparse_components(run_dir)
        run_real = run_dir.resolve(strict=True)
        project_root = Path(root)
        sku = run_real.parent.parent.name if run_real.parent.name == ".workflow" else run_real.parent.name
        results = ProjectPaths(project_root).results_for(sku).resolve(strict=True)
        relative = run_real.relative_to(results)
        run_name = relative.parts[-1] if relative.parts else ""
        valid_run_name = bool(re.fullmatch(r"(?:\d{8}-\d{6}|v\d{3,})", run_name))
        valid_run_path = (
            len(relative.parts) == 2
                or (
                len(relative.parts) == 3
                and relative.parts[1] == ".workflow"
                and run_name.startswith("v")
            )
        )
        if (
            not valid_run_path
            or not valid_run_name
                or relative.parts[0] != sku
            or not _safe_existing_directory(run_real)
        ):
            raise ValueError
        analysis_value = _read_bounded_json(analysis_path, run_real)
        shots_value = _read_bounded_json(shots_path, run_real)
    except _CliFailure:
        raise
    except Exception:
        raise _CliFailure("提示词 JSON 必须是当前任务目录内的普通本地文件") from None

    if not isinstance(analysis_value, dict) or not analysis_value:
        raise _CliFailure("动作分析 JSON 必须是非空对象")
    _validate_json_tree(analysis_value)
    if not isinstance(shots_value, list) or not 1 <= len(shots_value) <= 6:
        raise _CliFailure("分镜 JSON 必须包含 1 到 6 个镜头")
    shots: list[ShotPlan] = []
    for shot in shots_value:
        if not isinstance(shot, dict) or set(shot) != {"index", "duration", "prompt"}:
            raise _CliFailure("分镜 JSON 字段必须为 index、duration、prompt")
        index = shot["index"]
        duration = shot["duration"]
        prompt = shot["prompt"]
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or index < 1
            or not isinstance(duration, (int, float))
            or isinstance(duration, bool)
            or not math.isfinite(float(duration))
            or duration <= 0
            or not isinstance(prompt, str)
            or not prompt.strip()
        ):
            raise _CliFailure("分镜 JSON 的编号、时长或提示词无效")
        shots.append(ShotPlan(index=index, duration=duration, prompt=prompt))
    return analysis_value, shots


def _read_bounded_json(path: Path, run_dir: Path) -> object:
    path = Path(path)
    _reject_reparse_components(path)
    resolved = path.resolve(strict=True)
    resolved.relative_to(run_dir)
    metadata = path.lstat()
    if _is_reparse(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise _CliFailure("提示词 JSON 必须是普通文件")
    if metadata.st_size > _MAX_JSON_BYTES:
        raise _CliFailure("提示词 JSON 超过本地读取上限")
    before = _file_identity(metadata)
    try:
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if _file_identity(opened) != before:
                raise _CliFailure("提示词 JSON 在读取前发生变化")
            payload = stream.read(_MAX_JSON_BYTES + 1)
            after_open = os.fstat(stream.fileno())
        after_path = path.lstat()
    except _CliFailure:
        raise
    except Exception:
        raise _CliFailure("无法安全读取提示词 JSON") from None
    if (
        len(payload) > _MAX_JSON_BYTES
        or _file_identity(after_open) != before
        or _file_identity(after_path) != before
        or len(payload) != int(after_path.st_size)
    ):
        raise _CliFailure("提示词 JSON 在读取过程中发生变化")
    try:
        text = payload.decode("utf-8", errors="strict")
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
    except Exception:
        raise _CliFailure("提示词 JSON 格式无效") from None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def _validate_json_tree(value: object) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if math.isfinite(value):
            return
        raise _CliFailure("动作分析 JSON 包含无效数字")
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            _validate_json_tree(item)
        return
    raise _CliFailure("动作分析 JSON 结构无效")


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
    )


def _is_reparse(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(attributes & _REPARSE_ATTRIBUTE)


def _reject_reparse_components(path: Path) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    for component in reversed((absolute, *absolute.parents)):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if _is_reparse(metadata):
            raise OSError("reparse component")


def _json_value(value: object) -> object:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _json_value(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise _CliFailure("命令返回的数据结构无法安全序列化")


def _json_line(value: object) -> str:
    try:
        return json.dumps(_json_value(value), ensure_ascii=False, separators=(",", ":")) + "\n"
    except _CliFailure:
        raise
    except Exception:
        raise _CliFailure("命令结果无法安全序列化") from None


def _close_client(client: object) -> None:
    close = getattr(client, "close", None)
    if callable(close):
        close()


def _write_line(stream: object, value: str) -> None:
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="strict")
        except (OSError, ValueError):
            pass
    stream.write(value)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


def _write_error(message: str) -> None:
    _write_line(sys.stderr, message.replace("\r", " ").replace("\n", " ") + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
