"""Local-only technical audit artifacts for completed video runs.

This module deliberately makes no model, network, credential, or paid calls.
Semantic matching and approval remain a human decision.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path
from typing import Any, Iterable

import cv2
from PIL import Image


def _safe_component(value: str, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"{label} contains unsafe path characters")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _extract_video_evidence_frames(video_path: Path) -> list[tuple[float, Image.Image]]:
    """Sample a few evenly spaced local output frames for human review evidence."""
    capture = None
    try:
        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            return []
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT))
        if fps <= 0 or frame_count <= 0:
            return []
        indices = sorted(
            {
                round((frame_count - 1) * fraction / 4)
                for fraction in range(5)
            }
        )
        frames: list[tuple[float, Image.Image]] = []
        for index in indices:
            capture.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = capture.read()
            if not ok or frame is None or getattr(frame, "size", 0) == 0:
                continue
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            frames.append((index / fps, Image.fromarray(rgb)))
        return frames
    except Exception:
        return []
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return str(value)


def write_local_audit(
    *,
    sku: str,
    version: str,
    video_path: Path,
    history_sku_dir: Path,
    validation: dict[str, Any],
    reference_names: Iterable[str],
    evidence_frames: Iterable[tuple[float, Any]] | None = None,
    expected_aspect_ratio: str | None = None,
) -> dict[str, str]:
    """Create 01-style evidence/report files without claiming human approval.

    The output is append-only: an artifact collision stops without replacing it.
    ``evidence_frames`` may contain already-decoded ``(seconds, PIL.Image)`` pairs.
    """
    sku = _safe_component(sku, "SKU")
    version = _safe_component(version, "version")
    if not re.fullmatch(r"v[0-9]{3,}", version) or int(version[1:]) == 0:
        raise ValueError("version must be v001 or greater")
    video = Path(video_path)
    if video.is_symlink() or not video.is_file():
        raise ValueError("video must be a regular local file")
    history = Path(history_sku_dir)
    if history.name != sku:
        raise ValueError("history folder must match SKU")
    history.mkdir(parents=True, exist_ok=True)
    stem = version
    paths = {
        "json": history / f"{stem}_审核数据.json",
        "html": history / f"{stem}_审核报告.html",
        "screenshots": history / f"{stem}_问题截图",
        "suggestions": history / f"{stem}_修改建议.txt",
    }
    if any(path.exists() for path in paths.values()):
        if all(path.exists() for path in paths.values()):
            try:
                existing = json.loads(paths["json"].read_text(encoding="utf-8"))
                expected_digest = _sha256(video)
                if (
                    existing.get("sku") == sku
                    and existing.get("version") == version
                    and existing.get("video_sha256") == expected_digest
                    and existing.get("audit_mode") == "local_only"
                    and existing.get("audit_status") == "需要人工复核"
                    and paths["html"].read_text(encoding="utf-8").strip()
                    and paths["suggestions"].read_text(encoding="utf-8").strip()
                ):
                    return {key: str(path) for key, path in paths.items()}
            except (OSError, ValueError, AttributeError):
                pass
        raise FileExistsError("local audit artifacts already exist but cannot be safely reused")

    screenshot_dir = paths["screenshots"]
    screenshot_dir.mkdir()
    keyframes: list[dict[str, Any]] = []
    frames = (
        evidence_frames
        if evidence_frames is not None
        else _extract_video_evidence_frames(video)
    )
    for index, (seconds, image) in enumerate(frames, start=1):
        timestamp = max(0.0, float(seconds))
        frame_path = screenshot_dir / f"{timestamp:08.2f}s_{index:04d}.jpg"
        with frame_path.open("xb") as target:
            image.convert("RGB").save(target, format="JPEG", quality=90)
        keyframes.append({"time_seconds": timestamp, "path": frame_path.name})

    video_digest = _sha256(video)
    refs = [str(name) for name in reference_names]
    dims = [validation.get("width"), validation.get("height")]
    actual_ratio = None
    if all(isinstance(side, (int, float)) and side > 0 for side in dims):
        actual_ratio = f"{int(dims[0])}:{int(dims[1])}"
    report: dict[str, Any] = {
        "schema_version": "1.0",
        "sku": sku,
        "version": version,
        "video_sha256": video_digest,
        "video_size_bytes": video.stat().st_size,
        "validation": _json_safe(validation),
        "reference_names": refs,
        "expected_aspect_ratio": expected_aspect_ratio,
        "actual_dimensions": dims if actual_ratio else None,
        "actual_aspect_ratio": actual_ratio,
        "keyframes": keyframes,
        "keyframes_available": bool(keyframes),
        "audit_mode": "local_only",
        "audit_status": "需要人工复核",
        "semantic_review": "无法判断：本地技术检查不等于逐帧语义审核",
        "approval_status": "未审核通过",
    }
    suggestions = (
        "本地技术检查已生成；此文件不代表人工审核通过。\n"
        "请人工逐帧复核视频与全部参考素材，重点检查人物、服装、场景、动作、镜头连续性和构图。\n"
        "确认前请勿移入 03_审核通过。\n"
    )
    report_text = json.dumps(report, ensure_ascii=False, indent=2)
    html_text = (
        "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>本地视频审核报告</title>"
        "<h1>本地视频审核报告</h1><p>状态：需要人工复核（未审核通过）</p>"
        f"<p>款号：{html.escape(sku)}　版本：{html.escape(version)}</p>"
        f"<p>视频 SHA-256：{video_digest}</p>"
        f"<pre>{html.escape(report_text)}</pre></html>"
    )
    for key, content in (("json", report_text), ("html", html_text), ("suggestions", suggestions)):
        with paths[key].open("x", encoding="utf-8", newline="\n") as target:
            target.write(content)
    return {key: str(path) for key, path in paths.items()}
