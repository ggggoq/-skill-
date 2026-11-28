"""One authorized synthetic integration probe; never uses product materials.

This entry point is diagnostic, not an alternative product-generation workflow.
It submits at most one 4-second task, retains its ID, and never retries creation.
"""
from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import httpx

from .seedance_client import SeedanceClient, _public_url, _identifier, _unique_object
from .seedance_credentials import SeedanceCredentialStore
from .processing_log import ProcessingLog
from .workflow import _locked, _reject_reparse_components

_RESPONSE_FIELDS = frozenset({
    "id", "task_id", "status", "data", "result", "output", "outputs", "videos", "video",
    "url", "video_url", "videoUrl", "urlWithoutWatermark", "url_without_watermark",
    "watermark_enabled", "watermark_info", "enabled", "model", "object", "code",
    "message", "error", "errors", "request_id", "duration", "resolution", "ratio",
    "tmp_url", "name", "type", "size", "created_at", "updated_at", "progress",
})


def _shape(value, depth=0):
    if depth > 6:
        return "nested"
    if isinstance(value, dict):
        return {key: _shape(item, depth + 1) for key, item in value.items()
                if isinstance(key, str) and key in _RESPONSE_FIELDS}
    if isinstance(value, list):
        return [_shape(value[0], depth + 1)] if value else []
    if value is None:
        return "null"
    return type(value).__name__


class _ObservedTransport(httpx.BaseTransport):
    def __init__(self, observations):
        self.inner = httpx.HTTPTransport(retries=0)
        self.observations = observations
        self.last_payload = None

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        response.read()
        try:
            self.last_payload = response.json()
        except Exception:
            self.last_payload = None
        self.observations.append({"method": request.method,
                                  "http_status": response.status_code,
                                  "response_shape": _shape(self.last_payload)})
        return response

    def close(self):
        self.inner.close()


def _diagnostic_url(payload):
    if not isinstance(payload, dict):
        return None
    # Candidate fields for a DIAGNOSTIC download, not watermark proof.
    for key in ("urlWithoutWatermark", "url_without_watermark", "video_url", "url"):
        if _public_url(payload.get(key), https_only=True):
            return payload[key]
    for key in ("data", "result", "output"):
        result = _diagnostic_url(payload.get(key))
        if result:
            return result
    return None


def _write_state(path, data):
    _reject_reparse_components(path)
    temporary = path.with_suffix(".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    temporary.replace(path)


def _read_state(path):
    _reject_reparse_components(path)
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("invalid diagnostic state")
    state = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    required = {"provider", "model", "duration", "synthetic_only", "generation_attempts", "stage"}
    optional = {"task_id", "upload_verified", "watermark_observation", "diagnostic_downloaded",
                "diagnostic_sha256", "product_delivery"}
    if not isinstance(state, dict) or not required <= set(state) or set(state) - required - optional:
        raise ValueError("invalid diagnostic state")
    if (state["provider"] != "aimaxhug" or state["model"] != "Seedance-2.0-pro"
            or type(state["duration"]) is not int or state["duration"] != 4
            or state["synthetic_only"] is not True or type(state["generation_attempts"]) is not int):
        raise ValueError("invalid diagnostic identity")
    stage = state["stage"]
    if stage not in {"preparing", "creation_unknown", "submitted", "processing", "completed", "failed", "timed_out"}:
        raise ValueError("invalid diagnostic stage")
    task_id = state.get("task_id")
    if task_id is not None:
        _identifier(task_id)
    if (state["generation_attempts"] != (0 if stage == "preparing" else 1)
            or ((stage not in {"preparing", "creation_unknown"}) != bool(task_id))):
        raise ValueError("invalid diagnostic attempt state")
    for key in ("upload_verified", "diagnostic_downloaded"):
        if key in state and type(state[key]) is not bool:
            raise ValueError("invalid diagnostic flag")
    if "product_delivery" in state and state["product_delivery"] is not False:
        raise ValueError("diagnostic delivery forbidden")
    if "watermark_observation" in state and state["watermark_observation"] not in {"unverified_false", "unverified_true", "unavailable"}:
        raise ValueError("invalid watermark observation")
    if "diagnostic_sha256" in state and not re.fullmatch(r"[0-9a-f]{64}", str(state["diagnostic_sha256"])):
        raise ValueError("invalid diagnostic fingerprint")
    return state


def _synthetic_sources(root, folder, *, suffix=""):
    if suffix != "" and not re.fullmatch(r"-retry-[1-9][0-9]*", suffix):
        raise ValueError("invalid diagnostic source suffix")
    from PIL import Image, ImageDraw
    image = folder / f"synthetic{suffix}.png"
    video = folder / f"synthetic{suffix}.mp4"
    if image.exists() or video.exists():
        raise ValueError("diagnostic source already exists")
    picture = Image.new("RGB", (360, 640), "white")
    ImageDraw.Draw(picture).rectangle((120, 270, 240, 390), fill="red")
    picture.save(image)
    ffmpeg = Path((root / "04系统配置" / "ffmpeg路径.txt").read_text(encoding="utf-8-sig").strip())
    if not ffmpeg.is_file():
        raise ValueError("local ffmpeg unavailable")
    result = subprocess.run([
        str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
        "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=8:duration=4",
        "-f", "lavfi", "-i", "sine=frequency=523.25:sample_rate=44100:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(video),
    ], capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not video.is_file():
        raise ValueError("synthetic video creation failed")
    return image, video


def _next_retry_number(folder: Path) -> int:
    number = 1
    while any((folder / name).exists() for name in (
        f"response-shapes-retry-{number}.json",
        f"synthetic-retry-{number}.png",
        f"synthetic-retry-{number}.mp4",
    )):
        number += 1
    return number


def main(argv=None, *, root=None):
    args = list(sys.argv[1:] if argv is None else argv)
    retry_upload = args == ["--retry-upload-after-auth"]
    if args not in (["--confirm-paid-test"], ["--resume"], ["--retry-upload-after-auth"]):
        print("接口诊断必须明确指定 --confirm-paid-test、--retry-upload-after-auth 或 --resume。")
        return 2
    root = Path(root) if root is not None else Path(__file__).resolve().parents[2]
    folder = root / ".review-seedance-20261031" / "live-probe"
    _reject_reparse_components(folder)
    folder.mkdir(parents=True, exist_ok=True)
    state_path = folder / "probe.json"
    with _locked(folder / ".probe.lock"):
        try:
            state = _read_state(state_path)
        except Exception:
            print("接口测试记录无效；禁止读取凭据、查询或重提，请人工核查。")
            return 2
        retryable_upload = bool(
            state and not state.get("task_id") and state.get("stage") == "preparing"
            and state.get("generation_attempts") == 0
        )
        if state and not state.get("task_id") and not (retry_upload and retryable_upload):
            print("已有测试记录但没有安全任务编号；禁止再次提交，请人工核查。")
            return 2
        if not state and args in (["--resume"], ["--retry-upload-after-auth"]):
            print("没有可续查的接口测试记录。")
            return 2
        if retry_upload and state and state.get("task_id"):
            print("已有安全任务编号；只能续查，不会重新上传或创建。")
            return 2
        credential_store = SeedanceCredentialStore()
        try:
            if credential_store.status()["api-key"] != "PRESENT":
                print("Aimaxhug 凭据状态：ABSENT；未提交。")
                return 2
        except Exception:
            print("无法读取 Aimaxhug 凭据状态；未提交。")
            return 2
        retry_number = _next_retry_number(folder) if retry_upload else None
        paid_attempt = state is None or retry_upload
        log = ProcessingLog.start(root, command="seedance-probe",
                                  network_mode="paid" if paid_attempt else "read_only",
                                  paid_authorization_present=paid_attempt)
        observations = []
        transport = _ObservedTransport(observations)
        client = SeedanceClient(
            model="Seedance-2.0-pro",
            api_key_provider=credential_store.read,
            api_transport=transport,
        )
        uploader = None
        success = False
        try:
            if state is None:
                from .seedance_upload import SeedanceUploader
                state = {"stage": "preparing", "provider": "aimaxhug", "model": "Seedance-2.0-pro", "duration": 4,
                         "synthetic_only": True, "generation_attempts": 0}
                _write_state(state_path, state)
                picture, video = _synthetic_sources(root, folder)
            elif retry_upload:
                from .seedance_upload import SeedanceUploader
                picture, video = _synthetic_sources(root, folder, suffix=f"-retry-{retry_number}")
            if state.get("stage") == "preparing" and not state.get("task_id"):
                uploader = SeedanceUploader(api_key_provider=credential_store.read,
                                            transport=_ObservedTransport(observations))
                uploaded = uploader.upload_file(video, allowed_root=folder,
                    expected_sha256=hashlib.sha256(video.read_bytes()).hexdigest())
                state.update({"upload_verified": True, "stage": "creation_unknown", "generation_attempts": 1})
                _write_state(state_path, state)
                payload = {
                    "model": "Seedance-2.0-pro", "duration": 4, "aspect_ratio": "9:16", "resolution": "720p",
                    "prompt": "接口合成测试：@image1 保持 @video1 测试图案的运动和4秒原速，竖版9:16，保留参考视频原始音轨。无人物、无商品、无文字、无水印。",
                    "image_url": "data:image/png;base64," + base64.b64encode(picture.read_bytes()).decode("ascii"),
                    "reference_videos": [uploaded.url],
                }
                remote = client.create_video(payload)
                state.update({"task_id": _identifier(remote.task_id), "stage": "submitted"})
                _write_state(state_path, state)
                print("合成测试已提交一次，任务编号已登记。", flush=True)
            deadline = time.monotonic() + 1800
            previous = None
            while time.monotonic() < deadline:
                remote = client.query_video(task_id=state["task_id"])
                status = "completed" if remote.status == "succeed" else remote.status
                if status != previous:
                    print("任务状态：" + status, flush=True)
                    previous = status
                state["stage"] = status
                _write_state(state_path, state)
                if status == "failed":
                    print("测试任务失败，不会重新提交。")
                    break
                if status == "completed":
                    state["watermark_observation"] = (
                        "unverified_false" if remote.watermark_enabled is False
                        else "unverified_true" if remote.watermark_enabled is True else "unavailable"
                    )
                    url = remote.download_url or _diagnostic_url(transport.last_payload)
                    output = folder / "diagnostic-result.mp4"
                    if url and not output.exists():
                        client.download(url, output, allowed_root=folder)
                        state["diagnostic_downloaded"] = True
                        state["diagnostic_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
                    state["product_delivery"] = False
                    _write_state(state_path, state)
                    print("远端完成；结果仅作接口诊断，不进入商品待审核目录。")
                    success = True
                    break
                time.sleep(10)
            else:
                state["stage"] = "timed_out"
                _write_state(state_path, state)
                print("测试查询超时，编号已保留；只可续查，不会重提。")
            return 0 if success else 2
        except Exception as error:
            print("接口测试停止（" + type(error).__name__ + "）；已保留记录，不会自动重提。")
            return 2
        finally:
            response_name = (f"response-shapes-retry-{retry_number}.json"
                             if retry_upload else "response-shapes.json")
            _write_state(folder / response_name, {"observations": observations})
            if uploader is not None:
                uploader.close()
            client.close()
            log.finish(status="success" if success else "failure", exit_code=0 if success else 2)


if __name__ == "__main__":
    raise SystemExit(main())
