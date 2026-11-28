"""Local metadata cache only; cache hits never constitute human approval.

Callers own scope (SKU/group/model/role) and analysis version discipline.
Custom analysers must be read-only and return JSON metadata without secrets or
approval decisions. This module has no provider/runner dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Callable

from PIL import Image


def _json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('utf-8')


def _components(path: Path) -> None:
    if '..' in path.parts:
        raise ValueError('Parent path escapes are forbidden')
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError('Symlink or reparse path is forbidden')


def _signature(info: os.stat_result) -> tuple:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class PreflightCache:
    """Use a project root; relative input paths are relative to that root."""

    def __init__(self, root: Path):
        candidate = Path(root)
        if not candidate.is_absolute():
            candidate = Path.cwd() / candidate
        _components(candidate)
        if not candidate.is_dir():
            raise ValueError('Project root must be an existing directory')
        self.root = candidate.resolve(strict=True)
        self.cache_dir = self.root / '.model_tests' / 'cache'

    def _path(self, path: Path, *, regular: bool = True) -> Path:
        path = Path(path)
        if not path.is_absolute():
            path = self.root / path
        try:
            _components(path)
            path.relative_to(self.root)
            resolved = path.resolve(strict=regular)
            resolved.relative_to(self.root)
            if regular and not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError('Only regular files are allowed')
            # NTFS alternate data streams are not independent source files.
            if any(':' in part for part in path.relative_to(self.root).parts):
                raise ValueError('Alternate streams are forbidden')
            return path
        except (OSError, RuntimeError, ValueError):
            raise ValueError('Unsafe or unavailable project path') from None

    def _fingerprint(self, path: Path) -> tuple[str, int, tuple]:
        path = self._path(path)
        before = _signature(path.stat())
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            opened = _signature(os.fstat(stream.fileno()))
            # Windows stat/fstat can expose different ctime semantics. Compare
            # identity/size/mtime across APIs, and ctime only within one API.
            if opened[:4] != before[:4]:
                raise ValueError('Source changed during read')
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
            if _signature(os.fstat(stream.fileno())) != opened:
                raise ValueError('Source changed during read')
        self._path(path)
        if _signature(path.stat()) != before:
            raise ValueError('Source changed during read')
        return digest.hexdigest(), before[2], before

    def _read(self, path: Path, key: str) -> dict | None:
        self._path(path, regular=False)
        if not path.exists():
            return None
        self._path(path)
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
            checksum = record.pop('checksum')
            if (record.get('schema') != 1 or record.get('key') != key
                    or not isinstance(record.get('analysis'), dict)
                    or checksum != hashlib.sha256(_json(record)).hexdigest()):
                return None
            return record['analysis']
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def _write(self, path: Path, key: str, analysis: dict) -> None:
        self._path(self.cache_dir, regular=False)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._path(path, regular=False)
        record = {'schema': 1, 'key': key, 'analysis': analysis}
        record['checksum'] = hashlib.sha256(_json(record)).hexdigest()
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.cache_dir, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(_json(record))
                stream.flush()
                os.fsync(stream.fileno())
            self._path(temporary)
            self._path(path, regular=False)
            os.replace(temporary, path)
        finally:
            if temporary is not None and temporary.exists():
                self._path(temporary)
                temporary.unlink()

    def get_or_compute(self, path: Path, *, scope: str, analysis_version: str,
                       analyser: Callable[[Path], dict]) -> dict:
        """Hash every access; only skip the analyser on a validated cache hit.

        Analyser identity is caller-versioned. Checksums detect accidental cache
        corruption, not an attacker who can rewrite both metadata and checksum.
        """
        start = time.perf_counter()
        if not isinstance(scope, str) or not scope.strip() or not isinstance(analysis_version, str) or not analysis_version.strip():
            raise ValueError('Nonempty scope and analysis_version are required')
        path = self._path(path)
        before = self._fingerprint(path)
        key = hashlib.sha256(_json([1, scope, analysis_version,
                                   path.relative_to(self.root).as_posix(), before[0], before[1]])).hexdigest()
        entry = self.cache_dir / (key + '.json')
        analysis = self._read(entry, key)
        hit = analysis is not None
        if not hit:
            try:
                analysis = analyser(path)
                if not isinstance(analysis, dict):
                    raise ValueError('Expected metadata dictionary')
                analysis = json.loads(_json(analysis))
            except Exception:
                raise ValueError('Local metadata analysis failed') from None
        if self._fingerprint(path) != before:
            raise ValueError('Source changed during analysis or cache lookup')
        if not hit:
            self._write(entry, key, analysis)
        return {'file_sha256': before[0], 'size_bytes': before[1],
                'analysis': analysis, 'cache_hit': hit,
                'elapsed_seconds': time.perf_counter() - start}

    def inspect_image(self, path: Path, *, scope: str, model: str,
                      analysis_version: str = 'image-v1') -> dict:
        if not isinstance(model, str) or not model.strip():
            raise ValueError('A model is required')
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError('A scope is required')
        result = self.get_or_compute(path, scope=json.dumps(['image', scope, model]),
                                     analysis_version=analysis_version, analyser=_inspect_image)
        info = result['analysis']
        if model == 'minimax-h3-2k' and not all(256 <= info[side] <= 5760 for side in ('width', 'height')):
            raise ValueError('minimax-h3-2k requires each image side within 256..5760 pixels')
        return result


def _inspect_image(path: Path) -> dict:
    # Verify encoded content, then reopen and actually decode all pixel data.
    with Image.open(path) as image:
        image_format = image.format
        mime = Image.MIME.get(image_format, '')
        expected = Image.registered_extensions().get(path.suffix.lower())
        if not mime.startswith('image/') or (expected and expected != image_format):
            raise ValueError('Image MIME does not match its content')
        image.verify()
    with Image.open(path) as image:
        frames = getattr(image, 'n_frames', 1)
        if frames != 1:
            raise ValueError('Only single-frame images are allowed')
        image.load()
        return dict(width=image.width, height=image.height, format=image.format,
                    mode=image.mode, frames=frames)
