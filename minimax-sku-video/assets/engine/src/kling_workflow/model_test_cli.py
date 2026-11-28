"""Audited standalone model-test entry point; prepare is always offline."""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr, redirect_stdout
import json
import math
import os
from pathlib import Path
import re
import stat
import sys

from .processing_log import ProcessingLog


_COMMANDS = {'prepare', 'run', 'resume', 'status', 'providers'}
_RUN = re.compile(r'r\d{8}T\d{6}-[0-9a-f]{16}\Z')
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.-]{1,128}\Z')
_TASK_ID = re.compile(r'[A-Za-z0-9_.-]{1,180}\Z')
_SENSITIVE = re.compile(r'https?://|data:|bearer|authorization|api.?key|secret|token|[0-9a-f]{64}', re.I)
_STATES = {'preparing', 'prepared', 'preflight_failed', 'blocked_before_submission',
           'running', 'resuming', 'needs_attention', 'completed_with_failures',
           'awaiting_review', 'remote_failed', 'technical_fail', 'creation_unknown',
           'submitting', 'not_started', 'task_known', 'processing', 'poll_timeout'}
_FAILURES = {'preflight_failed', 'blocked_before_submission', 'needs_attention',
             'completed_with_failures', 'remote_failed', 'technical_fail',
             'creation_unknown', 'poll_timeout', 'unknown'}
_ARG_ERROR = '参数无效；请使用 prepare --plan 文件、run --run-id 编号 --confirm-paid-request、resume --run-id 编号、status --run-id 编号或 providers。'
_PAID_ERROR = '未提交；run 需要本轮明确付费授权后添加 --confirm-paid-request。'
_OP_ERROR = '操作未完成；请检查本地计划、批次编号及审批记录，使用 status 查看状态，勿盲目重复提交。'
_LOG_ERROR = '无法写入处理日志；请检查项目根目录和 06_处理日志 的权限，修复后再执行。'


class _Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's original error includes raw arguments. Never display it.
        raise _Refused()


def _parser():
    parser = _Parser(add_help=False, allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    for command in sorted(_COMMANDS):
        child = sub.add_parser(command, add_help=False, allow_abbrev=False)
        if command == 'prepare':
            child.add_argument('--plan', required=True)
        elif command in {'run', 'resume', 'status'}:
            child.add_argument('--run-id', required=True)
        if command == 'run':
            child.add_argument('--confirm-paid-request', action='store_true')
    return parser


def _root(value):
    path = Path(value).absolute()
    if '..' in path.parts:
        raise _Refused()
    for component in (path, *path.parents):
        info = component.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise _Refused()
    if not path.is_dir():
        raise _Refused()
    return path.resolve(strict=True)


def _extract_root(argv):
    remaining, explicit = [], []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--project-root':
            index += 1
            if index >= len(argv) or argv[index].startswith('--'):
                raise _Refused()
            explicit.append(argv[index])
        elif arg.startswith('--project-root='):
            explicit.append(arg.split('=', 1)[1])
        else:
            remaining.append(arg)
        index += 1
    if len(explicit) > 1 or explicit == ['']:
        raise _Refused()
    return _root(explicit[0] if explicit else os.environ.get('KLING_WORKFLOW_PROJECT_ROOT') or Path.cwd()), remaining


def _identifier(value, pattern=_IDENTIFIER):
    return value if (isinstance(value, str) and pattern.fullmatch(value)
                     and value not in {'.', '..'} and not _SENSITIVE.search(value)) else None


def _local_output(value, root, run_id):
    if not isinstance(value, str) or not run_id or _SENSITIVE.search(value):
        return None
    if any(ord(c) < 32 for c in value) or any(c in value for c in '?<>|'):
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            path = root / path
        base = root / '.model_tests' / 'runs' / run_id
        relative = path.relative_to(base)
        if '..' in path.parts or any(':' in part for part in relative.parts):
            return None
        for component in (path, *path.parents):
            if component.exists() or component.is_symlink():
                info = component.lstat()
                if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                    return None
        path.resolve().relative_to(base.resolve())
        return str(path)
    except (OSError, ValueError, RuntimeError):
        return None


def _summary(state, root):
    if not isinstance(state, dict):
        raise ValueError()
    run_id = state.get('run_id')
    run_id = run_id if isinstance(run_id, str) and _RUN.fullmatch(run_id) else None
    status = state.get('status')
    status = status if isinstance(status, str) and status in _STATES else 'unknown'
    jobs = []
    for raw in state.get('jobs', []):
        if not isinstance(raw, dict):
            continue
        item = {key: _identifier(raw.get(key)) for key in ('key', 'model')}
        item['task_id'] = _identifier(raw.get('task_id'), _TASK_ID)
        value = raw.get('state')
        item['state'] = value if isinstance(value, str) and value in _STATES else 'unknown'
        item['candidate_path'] = _local_output(raw.get('candidate_path'), root, run_id)
        preflight = raw.get('preflight')
        preflight = preflight if isinstance(preflight, dict) else {}
        item['preflight'] = {'paid_ready': preflight.get('paid_ready') is True}
        for key in ('cache_hits', 'cache_checks'):
            count = preflight.get(key)
            item['preflight'][key] = count if type(count) is int and count >= 0 else 0
        timings = raw.get('timings_seconds')
        preparation = timings.get('preparation') if isinstance(timings, dict) else None
        if (type(preparation) in (int, float) and preparation >= 0
                and (type(preparation) is int or math.isfinite(preparation))):
            item['preparation_seconds'] = preparation
        jobs.append(item)
    reports = state.get('reports', {})
    reports = reports if isinstance(reports, dict) else {}
    return {'run_id': run_id, 'status': status, 'jobs': jobs,
            'reports': {key: path for key in ('json', 'csv', 'markdown')
                        if (path := _local_output(reports.get(key), root, run_id))}}


def _runner(root):
    from .model_test_runner import BatchRunner
    return BatchRunner(root)


def _providers():
    from .model_test_adapters import supported_providers
    return supported_providers()


def main(argv=None, *, runner_factory=None, provider_registry=None):
    """Return a process exit code. Injection hooks support offline CLI tests.

    Invalid/unavailable roots are audited in cwd when safely writable. If no
    safe audit destination exists, refuse all runner work with a fixed error.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    root, parsed, rejection = None, None, None
    try:
        root, args = _extract_root(args)
        if not args or args[0].startswith('--'):
            args.insert(0, 'prepare')
        parsed = _parser().parse_args(args)
        if hasattr(parsed, 'run_id') and not _RUN.fullmatch(parsed.run_id):
            raise _Refused()
        if parsed.command == 'run' and not parsed.confirm_paid_request:
            rejection = _PAID_ERROR
    except Exception:
        rejection = _ARG_ERROR
    command = parsed.command if parsed is not None else 'invalid'
    paid = rejection is None and command == 'run'
    mode = 'paid' if paid else 'read_only' if rejection is None and command == 'resume' else 'offline'
    try:
        root = root if root is not None else _root(Path.cwd())
        log = ProcessingLog.start(root, command='model-test-' + command,
                                  network_mode=mode, paid_authorization_present=paid)
    except Exception:
        print(_LOG_ERROR, file=sys.stderr)
        return 1
    code, outcome, summary, message = 1, 'failure', None, None
    try:
        if rejection:
            code, outcome, message = 2, 'refused', rejection
        else:
            # Do not allow incidental native/provider prints to escape the
            # summary boundary. No raw output is buffered or persisted.
            with open(os.devnull, 'w', encoding='utf-8') as sink, redirect_stdout(sink), redirect_stderr(sink):
                if command == 'providers':
                    available = (provider_registry or _providers)()
                    summary = {'providers': [p for p in available if p in ('minimax', 'muse', 'omni')]}
                else:
                    runner = (runner_factory or _runner)(root)
                    if command == 'prepare':
                        state = runner.prepare(Path(parsed.plan))
                    elif command == 'run':
                        state = runner.run(parsed.run_id, confirm_paid_request=True)
                    elif command == 'resume':
                        state = runner.resume(parsed.run_id)
                    else:
                        state = runner.status(parsed.run_id)
                    summary = _summary(state, root)
            code = 1 if summary.get('status') in _FAILURES else 0
            outcome = 'failure' if code else 'success'
            if code:
                message = _OP_ERROR
    except KeyboardInterrupt:
        code, outcome, message = 130, 'cancelled', _OP_ERROR
    except Exception:
        code, outcome, message = 1, 'failure', _OP_ERROR
    finally:
        try:
            log.finish(status=outcome, exit_code=code)
        except Exception:
            code, summary, message = 1, None, _LOG_ERROR
    if summary is not None:
        print(json.dumps(summary, ensure_ascii=False))
    if message:
        print(message, file=sys.stderr)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
