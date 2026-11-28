"""Additive adapters for the three existing image-generation bridge contracts.

Preflight writes only a safety log and optional metadata cache, never approvals.
Native preview/review artifacts must already exist before paid_ready can be true
for MiniMax or Muse. Muse prompt_sha256 is the *composed* native prompt SHA.
No subprocess commands, reference-video continuation, or upload entry points.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PureWindowsPath
import re
import stat

from PIL import Image

from . import minimax_bridge as mini, muse_bridge as muse, omni_bridge as omni
from .processing_log import ProcessingLog

ADAPTER_VERSION = "production-adapters-v2"
_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_SENSITIVE = re.compile(r"https?://|data:|bearer|authorization|api.?key|secret|token", re.I)
_SAFE_ENUMS = frozenset({
    "blocked", "native_result_unavailable", "creation_unknown", "task_known",
    "task_known_main_saved", "task_known_receipt_saved", "task_known_audit_state_pending",
    "task_id_persistence_failed", "processing", "remote_failed", "transport_error", "http_error",
    "identity_mismatch", "echo_missing", "api_error", "remote_completed_no_media",
    "candidate_pending_review", "candidate_isolated_for_review", "candidate_validation_pending",
    "candidate_download_failed", "technical_validation_failed", "pending_human_review", "not_ready",
    "unproven", "unknown", "queued", "pending", "running", "in_progress", "created",
    "completed", "complete", "succeeded", "success", "done", "finished", "failed", "failure",
    "error", "cancelled", "canceled", "rejected", "credential_unavailable", "invalid_request_body",
    "request_build_error", "connect_timeout", "connect_error", "read_timeout", "write_timeout",
    "protocol_error", "other_transport_error", "transport_unknown", "credential_lookup", "serialize",
    "request_build", "send", "client_call", "audit_state_unavailable", "task_id_ambiguous",
    "task_id_missing", "candidate_collision", "candidate_validation", "recorded", "unavailable",
    "none", "timeout", "connection", "response_parse", "http_rejection", "missing_task_id",
})


class AdapterError(RuntimeError):
    """Only fixed local categories, never provider error text."""


def supported_providers() -> list[str]:
    return ["minimax", "muse", "omni"]


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("invalid_document")
        result[key] = value
    return result


def _document(path):
    value = json.loads(muse._read_stable(path), object_pairs_hook=_unique)
    if not isinstance(value, dict):
        raise AdapterError("invalid_document")
    return value


def _execution_dependencies(provider):
    if provider == "muse":
        from .muse_client import MuseClient
        from .muse_credentials import MuseCredentialStore
        credentials = MuseCredentialStore()
        return credentials, MuseClient(api_key_provider=credentials.read)
    from .minimax_client import MiniMaxClient
    from .seedance_credentials import SeedanceCredentialStore
    credentials = SeedanceCredentialStore()
    return credentials, MiniMaxClient(api_key_provider=credentials.read)


class ProductionAdapters:
    def __init__(self, root: Path, cache=None):
        root = Path(root).absolute()
        for part in (root, *root.parents):
            info = part.lstat()
            if part.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                raise AdapterError("invalid_project_root")
        if not root.is_dir():
            raise AdapterError("invalid_project_root")
        self.root = root.resolve(strict=True)
        self.cache = cache

    @staticmethod
    def supported_providers() -> list[str]:
        return supported_providers()

    def get(self, provider: str):
        if type(provider) is not str or provider not in supported_providers():
            raise AdapterError("unsupported_provider")
        return _Adapter(self.root, provider, self.cache)


class _Adapter:
    def __init__(self, root, provider, cache):
        self.root, self.provider, self.cache = root, provider, cache

    def _file(self, value):
        if (type(value) is not str or not value or len(value) > 1024
                or ":" in value or "\x00" in value
                or PureWindowsPath(value).drive or PureWindowsPath(value).root
                or ".." in PureWindowsPath(value).parts):
            raise AdapterError("invalid_project_file")
        path = self.root / value
        try:
            path.resolve(strict=True).relative_to(self.root)
            current = self.root
            for part in Path(value).parts:
                current = current / part
                info = current.lstat()
                if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError
        except (OSError, ValueError):
            raise AdapterError("invalid_project_file") from None
        return path

    def _identity(self, job):
        if not isinstance(job, dict) or job.get("provider") != self.provider:
            raise AdapterError("identity_mismatch")
        for key in ("key", "sku", "version", "model"):
            value = job.get(key)
            if type(value) is not str or not _ID.fullmatch(value) or _SENSITIVE.search(value):
                raise AdapterError("invalid_identity")
        if not mini._SKU.fullmatch(job["sku"]) or not re.fullmatch(r"v[0-9]{3,6}", job["version"]):
            raise AdapterError("invalid_identity")
        models = {"minimax": mini.MODEL_KEYS, "muse": (muse.MODEL,), "omni": omni._MODELS}
        if job["model"] not in models[self.provider]:
            raise AdapterError("identity_mismatch")
        for field in ("group", "group_number"):
            if field in job and (type(job[field]) is not int or job[field] <= 0):
                raise AdapterError("invalid_identity")
        if "group" in job and "group_number" in job and job["group"] != job["group_number"]:
            raise AdapterError("identity_mismatch")
        if "task_id" in job and (type(job["task_id"]) is not str or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", job["task_id"])):
            raise AdapterError("invalid_task_id")
        if "prompt_sha256" in job and (type(job["prompt_sha256"]) is not str or not _SHA.fullmatch(job["prompt_sha256"])):
            raise AdapterError("prompt_binding_mismatch")
        if "expected_preflight_fingerprint" in job:
            expected = job["expected_preflight_fingerprint"]
            if type(expected) is not str or not _SHA.fullmatch(expected):
                raise AdapterError("invalid_preflight_fingerprint")
        return {key: job[key] for key in ("key", "provider", "sku", "version", "model")}

    def _match(self, job, record):
        if any(record.get(k) != job[k] for k in ("sku", "version", "model")):
            raise AdapterError("identity_mismatch")
        group = job.get("group_number", job.get("group"))
        if group is not None and record.get("group_number") != group:
            # Muse/Omni native records have no group field. Never infer one.
            raise AdapterError("identity_mismatch")

    def _sources(self, job):
        files, images = {}, {}
        manifest = None
        if self.provider == "muse":
            data = job.get("inputs")
            required = {"prompt_file", "model_image", "reverse_flat", "action_storyboard"}
            if not isinstance(data, dict) or not required <= set(data) or set(data) - required - {"back_wear", "front_detail"}:
                raise AdapterError("invalid_inputs")
            if "group_number" in job or "group" in job:
                raise AdapterError("identity_mismatch")
            for role, value in data.items():
                files[role] = self._file(value)
                if role != "prompt_file":
                    images[role] = files[role]
        else:
            files["manifest"] = self._file(job.get("manifest"))
            manifest = _document(files["manifest"])
            self._match(job, manifest)
            files["prompt_file"] = self._file(manifest.get("prompt_file"))
            if not isinstance(manifest.get("images"), dict):
                raise AdapterError("invalid_inputs")
            for role, value in manifest["images"].items():
                files[role] = images[role] = self._file(value)
            if self.provider == "minimax":
                action = manifest.get("action_source")
                if not isinstance(action, dict):
                    raise AdapterError("invalid_inputs")
                files["action_source"] = self._file(action.get("path"))
        if job.get("approval_file") is not None:
            files["approval_file"] = self._file(job["approval_file"])
        return files, images, manifest

    def _bound_sources(self, job):
        files, images, manifest = self._sources(job)
        if self.provider in {"minimax", "muse"}:
            for suffix, role in ((".json", "native_review"), (".prompt.txt", "native_prompt")):
                value = f".{self.provider}_bridge/previews/{job['sku']}/{job['version']}{suffix}"
                if (self.root / value).exists():
                    files[role] = self._file(value)
        return files, images, manifest

    def _binding_digest(self, job, snapshot, versions):
        # The expected digest is a comparison operand, never part of its own hash.
        return _digest(_json(dict(identity=self._identity(job), files=snapshot, versions=versions,
                                 confirmed_prompt_sha256=job.get("prompt_sha256"))))

    def _assert_submit_binding(self, job, expected):
        files, _, _ = self._bound_sources(job)
        if self._binding_digest(job, self._snapshot(files), self._versions()) != expected:
            raise AdapterError("preflight_fingerprint_mismatch")

    def _versions(self):
        # Hash code, not timestamps: changing a policy invalidates every entry.
        names = ["model_test_adapters.py", "processing_log.py", "validation.py", "client.py",
                 "muse_bridge.py", "muse_client.py", "model_test_cache.py"]
        if self.provider == "minimax":
            names += ["minimax_bridge.py", "minimax_client.py", "seedance_client.py", "seedance_credentials.py"]
        elif self.provider == "omni":
            names += ["omni_bridge.py", "seedance_credentials.py"]
        else:
            names += ["muse_credentials.py"]
        versions = {"adapter": ADAPTER_VERSION}
        for name in names:
            path = Path(__file__).parent / name
            if path.is_file():
                versions[name] = _digest(path.read_bytes())
        for name in ("AGENTS.md", "docs/当前需求与审查口径.md", "docs/Omni固定10秒规则.md",
                     "docs/Omni飞书图片职责规则.md"):
            if (self.root / name).exists():
                versions[name] = _digest(muse._read_stable(self._file(name)))
        if self.provider == "omni":
            scripts = Path.home() / ".codex/skills/aimaxhug-generation/scripts"
            for name in ("omni_video.py", "shared_client.py", "shared_markers.py", "shared_menus.py"):
                path = scripts / name
                versions[name] = _digest(path.read_bytes()) if path.is_file() else "absent"
        return versions

    def _snapshot(self, files):
        return {role: {"path": path.relative_to(self.root).as_posix(),
                       "sha256": _digest(muse._read_stable(path))}
                for role, path in files.items()}

    def _source_probe(self, job, versions, hits):
        def probe(path):
            if self.cache is None:
                return muse.probe_video_source(path)
            result = self.cache.get_or_compute(path,
                scope=json.dumps([self.provider, job["sku"], job["version"], job["model"],
                                  job.get("group_number", job.get("group")), "action_source"]),
                analysis_version=_digest(_json(versions)),
                analyser=lambda p: {"duration_seconds": muse.probe_video_source(p)})
            hits.append({"role": "action_source", "cache_hit": result["cache_hit"]})
            return result["analysis"]["duration_seconds"]
        return probe

    def _prepare(self, job, files, *, source_probe=None):
        if self.provider == "minimax":
            return mini.MiniMaxBridge(self.root, source_video_probe=source_probe).prepare(job["manifest"])
        if self.provider == "omni":
            return omni.OmniBridge(self.root)._prepare(job["manifest"], include_image_bytes=False)
        return muse.prepare_image_request(muse._read_stable(files["prompt_file"]),
            files["model_image"], files["reverse_flat"], files["action_storyboard"],
            front_detail=files.get("front_detail"), back_wear=files.get("back_wear"))

    def _metadata(self, prepared, job):
        if self.provider == "minimax":
            return {"schema_version": 1, "provider": "aimaxhug", "sku": prepared.sku,
                "version": prepared.version, "group_number": prepared.group_number,
                "model": prepared.model, "mode": "reference_images", "duration": prepared.duration,
                "ratio": prepared.ratio, "prompt_sha256": prepared.prompt_sha256,
                "request_body_sha256": prepared.body_sha256, "roles": list(prepared.roles),
                "asset_fingerprints": [{"role": a.role, "sha256": a.sha256, "size_bytes": a.size_bytes,
                    "width": a.width, "height": a.height, "mime_type": a.mime_type} for a in prepared.assets],
                "action_source": dict(prepared.action_source)}
        if self.provider == "muse":
            return {"schema_version": 1, "sku": job["sku"], "version": job["version"],
                "model": job["model"], "mode": prepared.mode, "prompt_sha256": prepared.prompt_sha256,
                "roles": list(prepared.roles), "asset_fingerprints": list(prepared.asset_fingerprints),
                "seconds": prepared.body["seconds"], "aspect_ratio": prepared.body["aspect_ratio"]}
        preview = omni.OmniBridge._preview(prepared)
        return {"sku": prepared.sku, "version": prepared.version, "model": prepared.model,
                "prompt_sha256": prepared.prompt_sha256, "roles": list(prepared.images),
                "duration_seconds": preview["duration_seconds"], "aspect_ratio": preview["aspect_ratio"]}

    def _params(self, meta):
        """Report whitelist derived solely from validated native metadata."""
        duration_field = {"minimax": "duration", "muse": "seconds", "omni": "duration_seconds"}[self.provider]
        ratio_field = "ratio" if self.provider == "minimax" else "aspect_ratio"
        return {"duration_seconds": int(meta[duration_field]), "aspect_ratio": meta[ratio_field]}

    def _approval_ready(self, job, files, manifest, meta):
        if "prompt_sha256" in job and job["prompt_sha256"] != meta["prompt_sha256"]:
            raise AdapterError("prompt_binding_mismatch")
        if self.provider == "omni":
            review = manifest.get("prompt_review")
            if (not isinstance(review, dict) or set(review) != {"reviewer", "decision", "sha256"}
                    or review.get("reviewer") != "Sol" or review.get("decision") != "approved"
                    or str(review.get("sha256", "")).lower() != meta["prompt_sha256"]):
                raise AdapterError("approval_binding_mismatch")
        if "approval_file" in files:
            approval = _document(files["approval_file"])
            if self.provider != "minimax":
                raise AdapterError("unsupported_approval_file")
            expected = {key: meta[key] for key in mini._APPROVAL_FIELDS - {"reviewer", "decision"}}
            reviewer = approval.get("reviewer")
            if (set(approval) != mini._APPROVAL_FIELDS or approval.get("decision") != "approved"
                    or type(reviewer) is not str or not reviewer.strip() or len(reviewer) > 128
                    or any(ord(c) < 32 for c in reviewer)
                    or any(approval.get(k) != v for k, v in expected.items())):
                raise AdapterError("approval_binding_mismatch")
        if self.provider == "omni":
            return True
        base = ".minimax_bridge" if self.provider == "minimax" else ".muse_bridge"
        record_rel = f"{base}/previews/{job['sku']}/{job['version']}.json"
        prompt_rel = f"{base}/previews/{job['sku']}/{job['version']}.prompt.txt"
        if not (self.root / record_rel).exists() or not (self.root / prompt_rel).exists():
            return False
        review = _document(self._file(record_rel))
        if any(review.get(k) != v for k, v in meta.items()):
            raise AdapterError("review_binding_mismatch")
        if _digest(muse._read_stable(self._file(prompt_rel))) != meta["prompt_sha256"]:
            raise AdapterError("review_binding_mismatch")
        if self.provider == "minimax":
            return "approval_file" in files and review.get("status") == "awaiting_supervisor"
        return job.get("prompt_sha256") == meta["prompt_sha256"]

    def _preflight(self, job):
        identity = self._identity(job)
        files, images, manifest = self._bound_sources(job)
        before = self._snapshot(files)
        versions = self._versions()
        fingerprint = self._binding_digest(job, before, versions)
        hits, inputs = [], []
        for role, path in images.items():
            entry = {"role": role, **before[role], "size_bytes": path.stat().st_size}
            if self.cache is not None:
                result = self.cache.inspect_image(path, scope=json.dumps([
                    self.provider, job["sku"], job["version"], manifest.get("group_number") if manifest else None, role]),
                    model=job["model"], analysis_version=_digest(_json(versions)))
                entry.update(result["analysis"], cache_hit=result["cache_hit"])
                hits.append({"role": role, "cache_hit": result["cache_hit"]})
            else:
                with Image.open(path) as image:
                    entry.update(width=image.width, height=image.height, format=image.format, cache_hit=False)
            inputs.append(entry)
        if manifest and self.provider == "minimax" and manifest["action_source"].get("sha256") != before["action_source"]["sha256"]:
            raise AdapterError("source_binding_mismatch")
        def analyse(_path):
            prepared = self._prepare(job, files, source_probe=self._source_probe(job, versions, hits))
            return self._metadata(prepared, job)
        if self.cache is None:
            meta = analyse(None)
        else:
            result = self.cache.get_or_compute(files.get("manifest", files["prompt_file"]),
                scope=f"native:{self.provider}:{fingerprint}", analysis_version=_digest(_json(versions)), analyser=analyse)
            meta = result["analysis"]
            hits.append({"role": "native_metadata", "cache_hit": result["cache_hit"]})
        ready = self._approval_ready(job, files, manifest, meta)
        if self._snapshot(files) != before or self._versions() != versions:
            raise AdapterError("inputs_changed")
        result = {**identity, "state": "offline_preflight_passed", "fingerprint": fingerprint,
                "paid_ready": ready, "approval_present": "approval_file" in files or self.provider == "omni",
                "prompt_sha256": meta["prompt_sha256"], "roles": meta["roles"],
                "params": self._params(meta), "inputs": inputs, "cache_hits": hits, "credentials_checked": False}
        if "group_number" in meta:
            result["group_number"] = meta["group_number"]
            result["action_source"] = meta["action_source"]
        return result

    def _record(self, job, bridge):
        if self.provider != "omni":
            # Old native readers check leaf files; also check ancestor components.
            for category in ("tasks", "receipts", "previews"):
                rel = f".{self.provider}_bridge/{category}/{job['sku']}/{job['version']}.json"
                if (self.root / rel).exists() or (self.root / rel).is_symlink():
                    self._file(rel)
        if self.provider == "omni":
            return bridge._read_record(job["sku"], job["version"])[1]
        return bridge.store.task_record(job["sku"], job["version"])

    def _normalise(self, result, record=None):
        # Allow only bounded enums/numbers and paths; never copy provider payloads.
        result = result if isinstance(result, dict) else {}
        safe = {"state": "native_result_unavailable"}
        enum_fields = {"state", "failure_category", "failure_stage", "remote_status", "remote_state",
                       "review_status", "watermark_status", "failure_log_status"}
        for key in enum_fields:
            value = result.get(key)
            if type(value) is str and value in _SAFE_ENUMS:
                safe[key] = value
        for key in ("http_status", "width", "height", "duration_seconds"):
            value = result.get(key)
            if type(value) in (int, float) and 0 <= value < 10**9:
                safe[key] = value
        record = record or {}
        task_id = record.get("task_id") or result.get("task_id")
        if type(task_id) is str and re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", task_id) and not _SENSITIVE.search(task_id):
            safe["task_id"] = task_id
        value = record.get("candidate_path") or record.get("local_path") or result.get("candidate_path") or result.get("local_path")
        if type(value) is str and not _SENSITIVE.search(value):
            try:
                path = Path(value)
                if path.is_absolute():
                    value = path.relative_to(self.root).as_posix()
                if (":" in value or ".." in PureWindowsPath(value).parts
                        or PureWindowsPath(value).drive or PureWindowsPath(value).root):
                    raise ValueError
                path = self.root / value
                path.resolve(strict=False).relative_to(self.root)
                current = self.root
                for part in Path(value).parts:
                    current = current / part
                    if current.exists() or current.is_symlink():
                        info = current.lstat()
                        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                            raise ValueError
                safe["candidate_path"] = path.relative_to(self.root).as_posix()
            except (OSError, ValueError, AdapterError):
                pass
        return safe

    def _guard_resume_record(self, job, bridge, expected_task_id):
        """Check again on the native read inside its lock, closing the read gap."""
        def check(record):
            if not isinstance(record, dict):
                raise AdapterError("exact_task_required")
            self._match(job, record)
            if record.get("task_id") != expected_task_id:
                raise AdapterError("task_id_mismatch")
            return record
        if self.provider == "omni":
            original = bridge._read_record
            def read(sku, version):
                path, record = original(sku, version)
                return path, check(record)
            bridge._read_record = read
        else:
            original = bridge.store.task_record
            def read(sku, version):
                return check(original(sku, version))
            bridge.store.task_record = read

    def _execute(self, job, operation, paid, log):
        self._identity(job)
        if operation == "submit":
            if paid is not True:
                raise AdapterError("paid_confirmation_required")
            if self.provider == "minimax" and not job.get("approval_file"):
                raise AdapterError("approval_required")
            checked = self._preflight(job)
            expected = job.get("expected_preflight_fingerprint", checked["fingerprint"])
            if checked["fingerprint"] != expected:
                raise AdapterError("preflight_fingerprint_mismatch")
            if not checked["paid_ready"]:
                raise AdapterError("approval_required")
        client = None
        bridge = None
        record = None
        try:
            if self.provider == "omni":
                bridge = omni.OmniBridge(self.root)
                native_prepare = bridge._prepare
                def checked_prepare(*args, **kwargs):
                    prepared = native_prepare(*args, **kwargs)
                    self._match(job, self._metadata(prepared, job))
                    if "prompt_sha256" in job and prepared.prompt_sha256 != job["prompt_sha256"]:
                        raise AdapterError("prompt_binding_mismatch")
                    if operation == "submit":
                        self._assert_submit_binding(job, expected)
                    return prepared
                bridge._prepare = checked_prepare
                if operation == "submit":
                    native_body = bridge._body
                    def checked_body(prepared):
                        body = native_body(prepared)
                        # Omni's native approval binds the prompt, not image bytes.
                        # Recheck after client setup/body building, before create intent.
                        self._assert_submit_binding(job, expected)
                        return body
                    bridge._body = checked_body
            else:
                # Read and check stored identity before creating any network client.
                bridge = (mini.MiniMaxBridge(self.root) if self.provider == "minimax" else
                          muse.MuseBridge(self.root, client=None, credential_store=None))
            if operation == "resume":
                record = self._record(job, bridge)
                if not isinstance(record, dict) or not record.get("task_id"):
                    raise AdapterError("exact_task_required")
                self._match(job, record)
                bound_job = job
                if self.provider != "muse" and "manifest" in job:
                    manifest = _document(self._file(job["manifest"]))
                    self._match(job, manifest)
                    if self.provider == "minimax":
                        if record.get("group_number") != manifest.get("group_number"):
                            raise AdapterError("identity_mismatch")
                        bound_job = dict(job, group_number=manifest["group_number"])
                if "task_id" in job and job["task_id"] != record["task_id"]:
                    raise AdapterError("task_id_mismatch")
                self._guard_resume_record(bound_job, bridge, record["task_id"])
            if self.provider != "omni":
                credentials, client = _execution_dependencies(self.provider)
                bridge.client, bridge.credentials, bridge.audit_log = client, credentials, log
            if operation == "submit":
                files, _, _ = self._sources(job)
                if self.provider == "minimax":
                    prepared = bridge.prepare(job["manifest"])
                    self._match(job, self._metadata(prepared, job))
                    self._assert_submit_binding(job, expected)
                    result = bridge.submit(prepared, supervisor_approval=_document(files["approval_file"]), paid_confirmation=True)
                elif self.provider == "muse":
                    prepared = self._prepare(job, files)
                    self._assert_submit_binding(job, expected)
                    result = bridge.submit(prepared, sku=job["sku"], version=job["version"],
                        confirmed_prompt_sha256=job["prompt_sha256"], confirm_paid_request=True)
                else:
                    result = bridge.submit(job["manifest"], paid_confirmation=True)
            elif self.provider == "minimax":
                result = bridge.resume(job["sku"], job["version"], group_number=record["group_number"])
            else:
                result = bridge.resume(job["sku"], job["version"])
            return self._normalise(result, self._record(job, bridge))
        except AdapterError as exc:
            return {**self._normalise({"state": "blocked"}, record), "error_category": str(exc)}
        except Exception:
            # A native failure after POST may still have a registered exact task.
            record = None
            if bridge is not None:
                try:
                    record = self._record(job, bridge)
                except Exception:
                    pass
            result = self._normalise({"state": "blocked"}, record)
            result["error_category"] = "native_operation_failed"
            if record and record.get("state") == "creation_unknown":
                result["state"] = "creation_unknown"
            return result
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _run(self, job, operation, paid=False):
        log = None
        result = {"state": "blocked", "error_category": "local_validation_failed"}
        try:
            log = ProcessingLog.start(self.root, command=f"model-test-{self.provider}-{operation}",
                sku=job.get("sku") if isinstance(job, dict) else None,
                version=job.get("version") if isinstance(job, dict) else None,
                network_mode={"preflight": "offline", "submit": "paid", "resume": "read_only"}[operation],
                paid_authorization_present=paid is True)
            result = self._preflight(job) if operation == "preflight" else self._execute(job, operation, paid, log)
        except AdapterError as exc:
            result = {"state": "blocked", "error_category": str(exc)}
        except Exception:
            pass
        finally:
            if log is not None:
                try:
                    failed = "error_category" in result
                    log.record_step(model="local", role="query", reason="Model test adapter operation",
                                    result="rejected" if failed else "returned")
                    log.finish(status="failure" if failed else "success", exit_code=1 if failed else 0)
                except Exception:
                    result = {**result, "error_category": "audit_failed", "state": "blocked"}
        if operation == "preflight" and "error_category" in result:
            result["paid_ready"] = False
        return result

    def preflight(self, job: dict) -> dict:
        return self._run(job, "preflight")

    def submit(self, job: dict, *, paid_confirmation: bool) -> dict:
        return self._run(job, "submit", paid_confirmation)

    def resume(self, job: dict) -> dict:
        return self._run(job, "resume")
