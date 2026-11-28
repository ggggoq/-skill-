"""Additive batch coordination; original provider approval and ID guards remain authoritative."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import threading
import time
import uuid


class BatchError(RuntimeError):
    """Only fixed, user-safe messages cross this boundary."""


_RUN = re.compile(r"r\d{8}T\d{6}-[0-9a-f]{16}\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_TASK_ID = re.compile(r"[A-Za-z0-9_.-]{1,180}\Z")
_STATE = re.compile(r"[a-z_]{1,64}\Z")
_FINAL = {"remote_failed", "technical_fail", "awaiting_review", "needs_attention", "preflight_failed"}
_CANDIDATE_READY = {"candidate_pending_review", "candidate_isolated_for_review", "pending_human_review"}
_JOB_FIELDS = {"key", "provider", "sku", "version", "model", "manifest", "approval_file",
               "inputs", "prompt_sha256", "group_number", "review_policy"}
_INPUT_FIELDS = {"prompt_file", "model_image", "reverse_flat", "action_storyboard", "back_wear", "front_detail"}
_POLICY_FIELDS = {"min_width", "expected_duration_seconds", "expected_aspect_ratio", "sample_times", "require_audio"}
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _digest(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BatchError("JSON存在重复字段")
        result[key] = value
    return result


def _inside(root: Path, path: Path, *, file=False):
    path = path if path.is_absolute() else root / path
    try:
        path = Path(os.path.abspath(path))
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.exists() or current.is_symlink():
                info = current.lstat()
                if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
                    raise ValueError
        if file and not stat.S_ISREG(path.stat().st_mode):
            raise ValueError
        return path
    except Exception:
        raise BatchError("任务路径不在项目内或不是安全普通文件") from None


def _read(root, path):
    path = _inside(root, path, file=True)
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=_unique)
    except Exception:
        raise BatchError("任务JSON无效") from None


def _write(root, path, value, *, exclusive=False):
    path = _inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _inside(root, path.parent)
    if exclusive:
        with path.open("xb") as out:
            out.write(_encoded(value)); out.flush(); os.fsync(out.fileno())
        return
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("xb") as out:
            out.write(_encoded(value)); out.flush(); os.fsync(out.fileno())
        _inside(root, path)
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


@contextmanager
def _run_lock(root, directory):
    key = str(directory)
    with _LOCKS_GUARD:
        lock = _LOCKS.setdefault(key, threading.Lock())
    if not lock.acquire(blocking=False):
        raise BatchError("此批次已有执行者")
    handle = None
    locked = False
    try:
        path = _inside(root, directory / ".run.lock")
        handle = path.open("a+b")
        if path.stat().st_size == 0:
            handle.write(b"0"); handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        locked = True
        yield
    except OSError:
        raise BatchError("批次锁不可用或已有执行者") from None
    finally:
        if handle:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        lock.release()


def validate_plan(value):
    if not isinstance(value, dict) or set(value) - {"schema_version", "label", "jobs", "max_concurrency", "poll_interval_seconds", "poll_timeout_seconds"}:
        raise BatchError("批次计划字段无效")
    if value.get("schema_version") != 1:
        raise BatchError("批次计划版本无效")
    try:
        plan = json.loads(_encoded(value))
    except (ValueError, TypeError):
        raise BatchError("批次计划含无效数值") from None
    plan.setdefault("max_concurrency", 2)
    plan.setdefault("poll_interval_seconds", 30)
    plan.setdefault("poll_timeout_seconds", 1800)
    if type(plan["max_concurrency"]) is not int or plan["max_concurrency"] not in (1, 2):
        raise BatchError("本版并发上限为2")
    for key, minimum, maximum in (("poll_interval_seconds", .05, 300), ("poll_timeout_seconds", .05, 86400)):
        number = plan[key]
        if type(number) not in (int, float) or not minimum <= number <= maximum:
            raise BatchError("查询时间配置无效")
    if "label" in plan and (not isinstance(plan["label"], str) or len(plan["label"]) > 160):
        raise BatchError("批次名称无效")
    jobs = plan.get("jobs")
    if not isinstance(jobs, list) or not 1 <= len(jobs) <= 100:
        raise BatchError("批次需要1至100个任务")
    keys, identities = set(), set()
    for job in jobs:
        if not isinstance(job, dict) or set(job) - _JOB_FIELDS:
            raise BatchError("任务字段无效；不接受任意命令或凭据")
        for key in ("key", "provider", "sku", "version", "model"):
            if not isinstance(job.get(key), str) or not _NAME.fullmatch(job[key]) or job[key] in (".", ".."):
                raise BatchError("任务身份字段无效")
        identity = (job["provider"], job["sku"], job["version"])
        if job["key"] in keys or identity in identities:
            raise BatchError("批次内任务编号或供应商款号版本重复")
        keys.add(job["key"]); identities.add(identity)
        if "group_number" in job and (type(job["group_number"]) is not int or job["group_number"] <= 0):
            raise BatchError("参考组编号无效")
        for key in ("manifest", "approval_file"):
            if key in job and (not isinstance(job[key], str) or not job[key] or len(job[key]) > 1024):
                raise BatchError("任务文件路径无效")
        inputs = job.get("inputs", {})
        if not isinstance(inputs, dict) or set(inputs) - _INPUT_FIELDS or any(not isinstance(v, str) or not v or len(v) > 1024 for v in inputs.values()):
            raise BatchError("输入素材字段无效")
        if "prompt_sha256" in job and not _SHA.fullmatch(str(job["prompt_sha256"])):
            raise BatchError("提示词指纹无效")
        policy = job.get("review_policy", {})
        if not isinstance(policy, dict) or set(policy) - _POLICY_FIELDS:
            raise BatchError("验收策略字段无效")
        from .model_test_review import _policy
        try:
            _policy(policy)
        except (ValueError, TypeError):
            raise BatchError("验收策略取值无效") from None
    return plan


class BatchRunner:
    def __init__(self, project_root, *, adapter_factory=None, sleep=time.sleep, monotonic=time.monotonic):
        self.root = Path(project_root).resolve(strict=True)
        self.base = _inside(self.root, self.root / ".model_tests" / "runs")
        self.base.mkdir(parents=True, exist_ok=True)
        self.sleep, self.monotonic = sleep, monotonic
        self._state_guard = threading.RLock()
        if adapter_factory is None:
            from .model_test_cache import PreflightCache
            from .model_test_adapters import ProductionAdapters
            registry = ProductionAdapters(self.root, cache=PreflightCache(self.root))
            adapter_factory = registry.get
        self.factory = adapter_factory

    def _directory(self, run_id):
        if not isinstance(run_id, str) or not _RUN.fullmatch(run_id):
            raise BatchError("批次编号无效")
        directory = _inside(self.root, self.base / run_id)
        if not directory.is_dir():
            raise BatchError("批次不存在")
        return directory

    def status(self, run_id):
        return _read(self.root, self._directory(run_id) / "state.json")

    def _persist(self, directory, state):
        with self._state_guard:
            state["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
            _write(self.root, directory / "state.json", state)

    def _timing(self, item, key, value, *, add=False):
        with self._state_guard:
            previous = item["timings_seconds"].get(key, 0) if add else 0
            item["timings_seconds"][key] = round(previous + value, 6)

    def _report(self, directory, state):
        try:
            from .model_test_review import write_batch_report
            paths = write_batch_report(directory, state)
            state["reports"] = {k: str(v) for k, v in paths.items()}
        except Exception:
            state["report_status"] = "unavailable"
        self._persist(directory, state)

    @staticmethod
    def _preflight(adapter, job):
        result = adapter.preflight(dict(job))
        if not isinstance(result, dict) or not _SHA.fullmatch(str(result.get("fingerprint", ""))):
            raise BatchError("适配器未返回有效输入绑定")
        from .model_test_review import _params
        hits = result.get("cache_hits", 0)
        count = sum(x.get("cache_hit") is True for x in hits if isinstance(x, dict)) if isinstance(hits, list) else hits
        count = count if type(count) is int and count >= 0 else 0
        return {"fingerprint": result["fingerprint"], "paid_ready": result.get("paid_ready") is True,
                "cache_hits": count, "cache_checks": len(hits) if isinstance(hits, list) else count,
                "params": _params(result.get("params"))}

    def prepare(self, plan):
        if not isinstance(plan, dict):
            plan = _read(self.root, Path(plan))
        plan = validate_plan(plan)
        run_id = datetime.now(timezone.utc).strftime("r%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:16]
        directory = _inside(self.root, self.base / run_id)
        directory.mkdir(exist_ok=False)
        _write(self.root, directory / "plan.json", plan, exclusive=True)
        state = {"schema_version": 1, "run_id": run_id, "status": "preparing",
                 "plan_sha256": _digest(plan), "jobs": []}
        self._persist(directory, state)

        def prepare_job(job):
            started = self.monotonic()
            item = {k: job[k] for k in ("key", "provider", "model", "sku", "version", "group_number") if k in job}
            item.update(state="prepared", task_id=None, human_review="pending", timings_seconds={})
            try:
                item["preflight"] = self._preflight(self.factory(job["provider"]), job)
                item["params"] = item["preflight"]["params"]
            except Exception:
                item.update(state="preflight_failed", error_category="preflight_failed")
            item["timings_seconds"]["preparation"] = round(self.monotonic() - started, 6)
            return item

        with ThreadPoolExecutor(max_workers=plan["max_concurrency"]) as pool:
            state["jobs"] = list(pool.map(prepare_job, plan["jobs"]))
        state["status"] = "prepared" if all(j["state"] == "prepared" for j in state["jobs"]) else "preflight_failed"
        self._report(directory, state)
        return state

    def run(self, run_id, *, confirm_paid_request=False):
        if confirm_paid_request is not True:
            raise BatchError("真实生成需要本轮明确付费确认")
        return self._execute(run_id, allow_submit=True)

    def resume(self, run_id):
        return self._execute(run_id, allow_submit=False)

    def _load(self, directory):
        state = _read(self.root, directory / "state.json")
        plan = validate_plan(_read(self.root, directory / "plan.json"))
        if state.get("plan_sha256") != _digest(plan) or state.get("run_id") != directory.name:
            raise BatchError("批次计划已改变，请重新离线准备")
        expected = [(j["key"], j["provider"], j["sku"], j["version"], j["model"]) for j in plan["jobs"]]
        actual = [(j.get("key"), j.get("provider"), j.get("sku"), j.get("version"), j.get("model")) for j in state.get("jobs", [])]
        if expected != actual:
            raise BatchError("批次身份记录不一致")
        return plan, state

    def _execute(self, run_id, *, allow_submit):
        directory = self._directory(run_id)
        with _run_lock(self.root, directory):
            plan, state = self._load(directory)
            # Once ANY job has started, run also behaves as resume. No incremental creates.
            pristine = all(j["state"] == "prepared" for j in state["jobs"])
            submit_batch = allow_submit and pristine
            if submit_batch:
                try:
                    for job, item in zip(plan["jobs"], state["jobs"]):
                        current = self._preflight(self.factory(job["provider"]), job)
                        if current["fingerprint"] != item["preflight"]["fingerprint"] or not current["paid_ready"]:
                            raise BatchError("输入绑定改变或批次尚未全部审批")
                except Exception:
                    state["status"] = "blocked_before_submission"
                    self._report(directory, state)
                    raise BatchError("全批预检未通过：检查绑定和审批；没有发出生成请求") from None
            stop = threading.Event()
            state["status"] = "running" if submit_batch else "resuming"
            self._persist(directory, state)

            def persist(item, **updates):
                with self._state_guard:
                    item.update(updates)
                    self._persist(directory, state)

            def work(pair):
                job, item = pair
                recoverable = item["state"] == "needs_attention" and item.get("error_category") == "candidate_review_unavailable"
                if item["state"] in _FINAL and not recoverable:
                    return
                if not submit_batch and item["state"] in {"prepared", "not_started"}:
                    return
                adapter = self.factory(job["provider"])
                result = None
                work_started = self.monotonic()
                if submit_batch:
                    with self._state_guard:
                        if stop.is_set():
                            persist(item, state="not_started")
                            return
                        persist(item, state="submitting", generation_attempts=1)
                    started = self.monotonic()
                    try:
                        bound_job = dict(job, expected_preflight_fingerprint=item["preflight"]["fingerprint"])
                        result = adapter.submit(bound_job, paid_confirmation=True)
                    except Exception:
                        result = {"state": "creation_unknown", "failure_category": "submission_exception"}
                    self._timing(item, "submission", self.monotonic() - started)
                    if not self._task_id(result):
                        stop.set()
                        self._timing(item, "total", self.monotonic() - work_started, add=True)
                        persist(item, state="creation_unknown", error_category="no_confirmed_task_id")
                        return
                    persist(item, task_id=self._task_id(result), state="task_known")
                deadline = self.monotonic() + plan["poll_timeout_seconds"]
                first = True
                while True:
                    if result is None or not first:
                        started = self.monotonic()
                        try:
                            query_job = dict(job)
                            if item.get("task_id"):
                                query_job["task_id"] = item["task_id"]
                            result = adapter.resume(query_job)
                        except Exception:
                            result = {"state": "query_error"}
                        self._timing(item, "query_download", self.monotonic() - started, add=True)
                    first = False
                    returned_id = self._task_id(result)
                    if returned_id and item.get("task_id") and returned_id != item["task_id"]:
                        persist(item, state="needs_attention", error_category="identity_mismatch")
                        break
                    if returned_id:
                        persist(item, task_id=returned_id)
                    remote = result.get("state", "unknown") if isinstance(result, dict) else "unknown"
                    if remote in {"remote_failed", "failed", "rejected"}:
                        persist(item, state="remote_failed")
                        break
                    if remote in {"identity_mismatch", "remote_completed_no_media", "technical_validation_failed"}:
                        persist(item, state="needs_attention", error_category=remote)
                        break
                    if isinstance(result, dict) and result.get("error_category") in {"identity_mismatch", "task_id_mismatch", "invalid_task_id"}:
                        persist(item, state="needs_attention", error_category="identity_mismatch")
                        break
                    if remote in _CANDIDATE_READY and isinstance(result, dict) and result.get("candidate_path"):
                        self._review(directory, job, item, result["candidate_path"], persist)
                        break
                    if not item.get("task_id"):
                        persist(item, state="creation_unknown", error_category="no_confirmed_task_id")
                        break
                    if self.monotonic() >= deadline:
                        persist(item, state="poll_timeout", error_category="poll_timeout")
                        break
                    persist(item, state="processing", remote_status=remote if isinstance(remote, str) and _STATE.fullmatch(remote) else "unknown")
                    started = self.monotonic()
                    self.sleep(plan["poll_interval_seconds"])
                    self._timing(item, "polling_wait", self.monotonic() - started, add=True)
                    result = None
                self._timing(item, "total", self.monotonic() - work_started, add=True)
                persist(item)

            def guarded_work(pair):
                try:
                    return work(pair)
                except BaseException:
                    # Set the stop flag in this worker, before the pool can start
                    # another queued create. The collector may be waiting on a
                    # slower, earlier job and cannot provide this guarantee.
                    stop.set()
                    raise

            with ThreadPoolExecutor(max_workers=plan["max_concurrency"]) as pool:
                futures = [pool.submit(guarded_work, pair) for pair in zip(plan["jobs"], state["jobs"])]
                for future, item in zip(futures, state["jobs"]):
                    try:
                        future.result()
                    except Exception:
                        stop.set()
                        interrupted = "creation_unknown" if item.get("state") == "submitting" else "poll_timeout"
                        persist(item, state=interrupted, error_category="worker_interrupted")
            states = {j["state"] for j in state["jobs"]}
            if states & {"creation_unknown", "submitting", "not_started", "needs_attention", "poll_timeout"}:
                state["status"] = "needs_attention"
            elif states & {"remote_failed", "technical_fail", "preflight_failed"}:
                state["status"] = "completed_with_failures"
            elif states == {"awaiting_review"}:
                state["status"] = "awaiting_review"
            else:
                state["status"] = "prepared"
            self._report(directory, state)
            return state

    @staticmethod
    def _task_id(result):
        value = result.get("task_id") if isinstance(result, dict) else None
        return value if isinstance(value, str) and _TASK_ID.fullmatch(value) else None

    def _review(self, directory, job, item, candidate, persist):
        started = self.monotonic()
        try:
            from .model_test_review import inspect_candidate
            source = _inside(self.root, Path(candidate), file=True)
            output = _inside(self.root, directory / "jobs" / job["key"])
            output.mkdir(parents=True, exist_ok=True)
            target = output / ("candidate-" + uuid.uuid4().hex[:8] + source.suffix.lower())
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
                dst.flush(); os.fsync(dst.fileno())
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                raise BatchError("复制核验失败")
            review = inspect_candidate(self.root, target, output, policy=job.get("review_policy"))
            candidate_path = str(target)
            good = review.get("status") == "technical_pass"
            persist(item, state="awaiting_review" if good else "technical_fail", candidate_path=candidate_path,
                    review=review, human_review="pending", error_category=None)
        except Exception:
            persist(item, state="needs_attention", error_category="candidate_review_unavailable", human_review="pending")
        finally:
            self._timing(item, "technical_review", self.monotonic() - started, add=True)
