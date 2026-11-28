"""Offline model-test QA; technical evidence never constitutes human acceptance.

All media is opened read-only. PNGs are lossless decoded-frame evidence (not
re-encoded source media), at native resolution, in a fresh output subdirectory.
Report persistence deliberately accepts only the documented small scalar schema.
"""
from __future__ import annotations

import csv
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile

import cv2
from PIL import Image, ImageDraw

from .media_audio import probe_audio
from .validation import (
    _is_network_path,
    _positive_float,
    _positive_integer,
    _regular_file_identity,
    _reject_reparse_components,
    _sha256_file,
    _valid_frame,
)


def _local(path: Path) -> Path:
    path = Path(path)
    if '..' in path.parts or _is_network_path(path):
        raise ValueError('unsafe local path')
    # Reject device paths and Windows alternate data streams as well.
    if any(':' in part for part in path.parts if part != path.anchor):
        raise ValueError('unsafe local path')
    _reject_reparse_components(path)
    return Path(os.path.abspath(path))


def _within(path: Path, root: Path) -> Path:
    path = _local(path if path.is_absolute() else root / path)
    if not path.is_relative_to(root):
        raise ValueError('path outside root')
    return path


def _fresh_dir(output: Path, prefix: str) -> Path:
    output = _local(output)
    output.mkdir(parents=True, exist_ok=True)
    _local(output)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=output))


def _publish(path: Path, payload: bytes) -> None:
    """Atomic publication without replacing any existing name, including links."""
    _local(path)
    fd, name = tempfile.mkstemp(prefix='.writing-', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        _local(path)
        if os.name == 'nt':
            os.rename(temporary, path)  # Windows rename fails if target exists.
        else:
            os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _png(path: Path, image: Image.Image) -> None:
    buffer = io.BytesIO()
    image.save(buffer, format='PNG')
    _publish(path, buffer.getvalue())


def _number(value: object, *, zero: bool = False) -> bool:
    return (type(value) in (int, float) and math.isfinite(value)
            and (value >= 0 if zero else value > 0))


def _policy(raw: dict | None) -> dict:
    values = {'min_width': 720, 'require_audio': False}
    if raw is not None:
        if not isinstance(raw, dict) or set(raw) - {
            'min_width', 'require_audio', 'expected_duration_seconds',
            'expected_aspect_ratio', 'sample_times',
        }:
            raise ValueError('invalid policy')
        values.update(raw)
    if type(values['min_width']) is not int or values['min_width'] <= 0:
        raise ValueError('invalid policy')
    if type(values['require_audio']) is not bool:
        raise ValueError('invalid policy')
    for key in ('expected_duration_seconds', 'expected_aspect_ratio'):
        if values.get(key) is not None and not _number(values[key]):
            raise ValueError('invalid policy')
    times = values.get('sample_times')
    if times is not None and (not isinstance(times, (list, tuple))
                             or not 1 <= len(times) <= 12
                             or not all(_number(t, zero=True) for t in times)):
        raise ValueError('invalid policy')
    return values


def inspect_candidate(project_root: Path, candidate: Path, output_dir: Path,
                      *, policy: dict | None = None) -> dict:
    """Decode every frame and extract native-size evidence, without network I/O.

    Relative candidate/output paths are project-relative. Output must be a child
    directory of the project. Expected aspect ratio is numeric width/height;
    duration tolerance is 0.001 seconds and ratio tolerance is one pixel.
    Missing/unsafe paths return unavailable; unreadable media fails technically.
    Required audio checks track presence and matching duration, not audibility.
    """
    result = dict(status='unavailable', human_review='pending', width=None,
                  height=None, duration_seconds=None, fps=None, decoded_frames=0,
                  sha256=None, checks={}, contact_sheet=None, frames=[])
    checks = result['checks']
    try:
        root = _local(project_root)
        source = _within(Path(candidate), root)
        output = _within(Path(output_dir), root)
        if not root.is_dir() or output == root or output == source:
            raise ValueError('unsafe output')
        identity, _ = _regular_file_identity(source)
        if source.suffix.lower() not in {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.m4v'}:
            raise ValueError('unsupported local video')
        checks['paths'] = 'pass'
    except (OSError, ValueError, TypeError):
        checks['paths'] = 'fail'
        return result
    result['status'] = 'technical_fail'
    try:
        settings = _policy(policy)
    except (ValueError, TypeError, OverflowError):
        checks['policy'] = 'fail'
        return result
    capture = None
    samples: dict[int, Image.Image] = {}
    try:
        result['sha256'] = _sha256_file(source, identity)
        capture = cv2.VideoCapture(os.fspath(source))
        checks['full_decode'] = 'fail'
        if not capture.isOpened():
            return result
        width = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_WIDTH))
        height = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fps = _positive_float(capture.get(cv2.CAP_PROP_FPS))
        declared = _positive_integer(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        result.update(width=width, height=height, fps=fps)
        checks['metadata'] = 'pass' if all((width, height, fps, declared)) else 'fail'
        if checks['metadata'] != 'pass':
            return result
        times = settings.get('sample_times')
        if times is None:
            targets = {0, (declared - 1) // 2, declared - 1}
            checks['sample_times'] = 'pass'
        else:
            checks['sample_times'] = 'pass' if all(t < declared / fps for t in times) else 'fail'
            targets = {int(t * fps + 1e-9) for t in times if t < declared / fps}
        valid = True
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            index = result['decoded_frames']
            result['decoded_frames'] += 1
            if not _valid_frame(frame, width, height):
                valid = False
                continue
            if index in targets:
                samples[index] = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        count = result['decoded_frames']
        checks['full_decode'] = 'pass' if valid and count == declared else 'fail'
        duration = count / fps
        result['duration_seconds'] = duration
        checks['min_width'] = 'pass' if width >= settings['min_width'] else 'fail'
        expected = settings.get('expected_duration_seconds')
        if expected is not None:
            checks['duration'] = 'pass' if abs(duration - expected) <= .001 else 'fail'
        ratio = settings.get('expected_aspect_ratio')
        if ratio is not None:
            checks['aspect_ratio'] = 'pass' if abs(width - height * ratio) <= 1 else 'fail'
        if settings['require_audio']:
            audio = probe_audio(source)
            checks['audio'] = 'unverified'
            if audio.status == 'absent':
                checks['audio'] = 'fail'
            elif audio.status == 'present' and _number(audio.duration_seconds):
                checks['audio'] = 'pass' if abs(audio.duration_seconds - duration) <= .001 else 'fail'
        checks['source_unchanged'] = 'pass' if (
            _regular_file_identity(source)[0] == identity
        ) else 'fail'
        if samples and checks['source_unchanged'] == 'pass':
            destination = _fresh_dir(output, 'review-')
            sheet = Image.new('RGB', (width, (height + 24) * len(samples)), 'white')
            draw = ImageDraw.Draw(sheet)
            for row, (index, picture) in enumerate(sorted(samples.items())):
                path = destination / f'frame-{index:08d}.png'
                _png(path, picture)
                result['frames'].append(dict(path=str(path), frame_index=index, time_seconds=index / fps))
                y = row * (height + 24)
                draw.text((4, y + 4), f'frame {index} / {index / fps:.3f}s', fill='black')
                sheet.paste(picture, (0, y + 24))
            path = destination / 'contact-sheet.png'
            _png(path, sheet)
            result['contact_sheet'] = str(path)
            checks['evidence'] = 'pass'
        result['status'] = 'technical_pass' if all(v == 'pass' for v in checks.values()) else 'technical_fail'
    except Exception:
        # Never expose decoder messages, paths from exceptions or arbitrary data.
        checks['inspection'] = 'fail'
        result['status'] = 'technical_fail'
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                checks['decoder_release'] = 'fail'
                result['status'] = 'technical_fail'
        if result['sha256'] is not None:
            try:
                unchanged = (_regular_file_identity(source)[0] == identity
                             and _sha256_file(source, identity) == result['sha256'])
            except (OSError, ValueError):
                unchanged = False
            checks['source_unchanged'] = 'pass' if unchanged else 'fail'
            if not unchanged:
                result['status'] = 'technical_fail'
    return result


_IDENTIFIER = re.compile(r'[A-Za-z0-9\u4e00-\u9fff][A-Za-z0-9\u4e00-\u9fff_.:/ -]{0,159}\Z')
_SENSITIVE = re.compile(r'://|bearer|authorization|api[_ -]?key|password|secret|token|cookie|(?<![A-Za-z0-9])sk-', re.I)
_TIMINGS = {'preflight', 'upload', 'submit', 'create', 'poll', 'download', 'review', 'total',
            'queue', 'generation', 'inspect', 'elapsed', 'preparation', 'submission',
            'query_download', 'polling_wait', 'technical_review'}


def _label(value: object) -> str | None:
    if isinstance(value, str) and _IDENTIFIER.fullmatch(value) and not _SENSITIVE.search(value):
        return value
    return None


def _params(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    safe = {}
    for key in ('duration_seconds', 'duration', 'width', 'height', 'fps', 'seed'):
        if _number(raw.get(key), zero=True):
            safe[key] = raw[key]
    for key in ('enable_audio', 'keepOriginalSound', 'mute_voice'):
        if type(raw.get(key)) is bool:
            safe[key] = raw[key]
    for key in ('aspect_ratio', 'resolution'):
        value = raw.get(key)
        if isinstance(value, str) and re.fullmatch(r'[0-9]{1,5}(?:[:x][0-9]{1,5}|p)?', value):
            safe[key] = value
    return safe


def _report_candidate(value: object, run: Path) -> str | None:
    if not isinstance(value, (str, Path)) or _SENSITIVE.search(str(value)):
        return None
    try:
        path = _within(Path(value), run)
        _regular_file_identity(path)
        relative = path.relative_to(run)
        if not all(_label(part) is not None for part in relative.parts):
            return None
        return relative.as_posix()
    except (OSError, ValueError, TypeError):
        return None


def write_batch_report(run_dir: Path, state: dict) -> dict:
    """Publish JSON/CSV/Markdown in a new report-* directory; return their paths.

    Candidate references are only emitted for existing regular files inside
    run_dir. Unknown fields and nested provider payloads are omitted. Human
    review is always pending, including when the input claims approval.
    Parameters are read from each job's optional ``params`` dictionary.
    """
    try:
        run = _local(run_dir)
        if not isinstance(state, dict):
            raise ValueError('invalid state')
        raw_jobs = state.get('jobs', [])
        if not isinstance(raw_jobs, list) or not all(isinstance(job, dict) for job in raw_jobs):
            raise ValueError('invalid jobs')
        jobs = []
        for raw in raw_jobs:
            job = {key: _label(raw.get(key)) for key in (
                'key', 'provider', 'model', 'sku', 'version', 'state', 'task_id', 'error_category')}
            group = raw.get('group_number')
            job['group_number'] = group if type(group) is int and group >= 0 else None
            job['candidate_path'] = _report_candidate(raw.get('candidate_path'), run)
            job['params'] = _params(raw.get('params'))
            timings = raw.get('timings_seconds', {})
            job['timings_seconds'] = {
                key: value for key, value in timings.items()
                if key in _TIMINGS and _number(value, zero=True)
            } if isinstance(timings, dict) else {}
            raw_review = raw.get('review', {})
            status = raw_review.get('status') if isinstance(raw_review, dict) else None
            job['review'] = {'status': status if status in (
                'technical_pass', 'technical_fail', 'unavailable') else None, 'human_review': 'pending'}
            preflight = raw.get('preflight')
            if isinstance(preflight, dict):
                safe_preflight = {'status': _label(preflight.get('status', 'prepared'))}
                for key in ('cache_hits', 'cache_checks'):
                    if _number(preflight.get(key), zero=True):
                        safe_preflight[key] = preflight[key]
                if type(preflight.get('paid_ready')) is bool:
                    safe_preflight['paid_ready'] = preflight['paid_ready']
                job['preflight'] = safe_preflight
            jobs.append(job)
        report = {'run_id': _label(state.get('run_id')), 'status': _label(state.get('status')), 'jobs': jobs}
        columns = ['key', 'provider', 'model', 'sku', 'version', 'group_number', 'params',
                   'state', 'timings_seconds', 'task_id', 'candidate_path', 'technical_review', 'human_review']
        rows = []
        for job in jobs:
            row = {key: job.get(key) for key in columns}
            for key in ('params', 'timings_seconds'):
                row[key] = json.dumps(job[key], ensure_ascii=False, sort_keys=True)
            row.update(technical_review=job['review']['status'], human_review='pending')
            rows.append(row)
        csv_buffer = io.StringIO(newline='')
        writer = csv.DictWriter(csv_buffer, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        markdown = ['# Model test review', '', 'Technical checks only. Human acceptance: pending.', '',
                    '| ' + ' | '.join(columns) + ' |', '| ' + ' | '.join(['---'] * len(columns)) + ' |']
        for row in rows:
            markdown.append('| ' + ' | '.join(str(row[key] if row[key] is not None else '').replace('|', '\\|').replace('\n', ' ') for key in columns) + ' |')
        destination = _fresh_dir(run, 'report-')
        payloads = {'json': json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n',
                    'csv': csv_buffer.getvalue(), 'markdown': '\n'.join(markdown) + '\n'}
        paths = {}
        for kind, payload in payloads.items():
            path = destination / ('summary.' + ('md' if kind == 'markdown' else kind))
            _publish(path, payload.encode('utf-8'))
            paths[kind] = str(path)
        return paths
    except (OSError, ValueError, TypeError):
        raise ValueError('cannot safely write batch report') from None
