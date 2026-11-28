"""In-memory orchestration for a safe Kling MCP retry.

The local project never uploads private assets and never persists signed URLs.
The host (Codex plus the Kling MCP connector) supplies :class:`McpAsset` values
after the connector upload call.  This module only decides which resources are
still needed and passes their addresses directly, in memory, to one creation
callback.  Polling and downloading are deliberately separate from submission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping


_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_VERSION = re.compile(r"^v[0-9]{3,}$")
_KINDS = frozenset({"image", "video"})


class McpRetryError(RuntimeError):
    """Raised when an MCP retry cannot safely continue."""


@dataclass(frozen=True)
class RetryScope:
    """Identity of one SKU/reference-group task.

    The scope intentionally contains both the SKU and all current evidence
    fingerprints.  It is the cache boundary: a different SKU or reference
    group cannot reuse another task's analysis, prompt, or resource address.
    """

    sku: str
    version: str
    reference_group: str
    analysis_sha256: str
    prompt_sha256: str
    asset_fingerprint: str

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.sku):
            raise ValueError("invalid retry scope SKU")
        if not _VERSION.fullmatch(self.version):
            raise ValueError("invalid retry scope version")
        if not _IDENTIFIER.fullmatch(self.reference_group):
            raise ValueError("invalid retry scope reference group")
        for name, value in (
            ("analysis", self.analysis_sha256),
            ("prompt", self.prompt_sha256),
            ("asset", self.asset_fingerprint),
        ):
            if not _HEX64.fullmatch(value.casefold()):
                raise ValueError(f"invalid retry scope {name} fingerprint")
            object.__setattr__(self, f"{name}_sha256" if name != "asset" else "asset_fingerprint", value.casefold())


@dataclass(frozen=True, repr=False)
class McpAsset:
    """One connector-uploaded resource held only in process memory.

    ``address`` is deliberately excluded from repr/metadata.  It may be a
    signed URL or another private connector handle and must never reach a log
    file, JSON record, terminal, or chat message.
    """

    name: str
    kind: str
    sha256: str
    address: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _IDENTIFIER.fullmatch(self.name):
            raise ValueError("invalid MCP asset name")
        if self.kind not in _KINDS:
            raise ValueError("invalid MCP asset kind")
        if not _HEX64.fullmatch(self.sha256.casefold()):
            raise ValueError("invalid MCP asset fingerprint")
        object.__setattr__(self, "sha256", self.sha256.casefold())
        if not isinstance(self.address, str) or not self.address.strip():
            raise ValueError("MCP asset address must be non-empty")


class InMemoryAssetCache:
    """Process-local asset cache keyed by the complete :class:`RetryScope`."""

    def __init__(self) -> None:
        self._entries: dict[RetryScope, dict[str, McpAsset]] = {}

    def put(self, scope: RetryScope, assets: Mapping[str, McpAsset]) -> None:
        copied = dict(assets)
        for name, asset in copied.items():
            if name != asset.name:
                raise ValueError("MCP asset map key must match asset name")
        self._entries.setdefault(scope, {}).update(copied)

    def get(self, scope: RetryScope, name: str) -> McpAsset | None:
        return self._entries.get(scope, {}).get(name)

    def remove(self, scope: RetryScope, names: tuple[str, ...] | list[str]) -> None:
        bucket = self._entries.get(scope)
        if bucket is None:
            return
        for name in names:
            bucket.pop(name, None)

    def names(self, scope: RetryScope) -> tuple[str, ...]:
        return tuple(sorted(self._entries.get(scope, {})))


@dataclass(frozen=True)
class RetryPlan:
    scope: RetryScope
    reused: tuple[str, ...]
    reupload: tuple[str, ...]
    analysis_reused: bool
    prompt_reused: bool


class McpRetryCoordinator:
    """Plan one in-memory MCP retry and enforce single creation submission."""

    def __init__(
        self,
        *,
        cache: InMemoryAssetCache,
        scope: RetryScope,
        required_assets: tuple[str, ...] | list[str],
        analysis_cached: bool = True,
        prompt_cached: bool = True,
    ) -> None:
        required = tuple(dict.fromkeys(required_assets))
        if not required or any(not _IDENTIFIER.fullmatch(name) for name in required):
            raise ValueError("required MCP assets must be non-empty safe names")
        if analysis_cached is not True or prompt_cached is not True:
            raise McpRetryError(
                "retry requires the current SKU/reference-group analysis and prompt cache"
            )
        self.cache = cache
        self.scope = scope
        self.required_assets = required
        self.analysis_calls = 0
        self.prompt_build_calls = 0
        self.create_calls = 0
        self.poll_calls = 0
        self.download_calls = 0
        self.state = "assets_pending"
        self.generation_id: str | None = None
        self._invalid: set[str] = set()
        self._downloaded = False

    def plan(self) -> RetryPlan:
        """Return only current-scope resources that must be uploaded."""
        reused = tuple(sorted(
            name
            for name in self.required_assets
            if self.cache.get(self.scope, name) is not None and name not in self._invalid
        ))
        reupload = tuple(name for name in self.required_assets if name not in reused)
        if not reupload and self.state == "assets_pending":
            self.state = "assets_ready"
        return RetryPlan(
            scope=self.scope,
            reused=reused,
            reupload=reupload,
            analysis_reused=True,
            prompt_reused=True,
        )

    def mark_invalid(self, names: tuple[str, ...] | list[str]) -> None:
        """Invalidate only failed resources; analysis and prompt stay untouched."""
        invalid = tuple(sorted(set(names)))
        unknown = [name for name in invalid if name not in self.required_assets]
        if unknown:
            raise McpRetryError("invalid resource is not part of the current task")
        self._invalid.update(invalid)
        self.cache.remove(self.scope, invalid)
        if self.state not in {"creation_unknown", "created", "polling", "downloaded"}:
            self.state = "assets_pending"

    def accept_uploads(self, assets: Mapping[str, McpAsset]) -> None:
        """Accept connector upload results without writing their addresses anywhere."""
        if self.state in {"creation_unknown", "created", "polling", "downloaded"}:
            raise McpRetryError("uploads are closed after generation creation")
        for name, asset in assets.items():
            if name not in self.required_assets or name != asset.name:
                raise McpRetryError("upload result is outside the current task")
        self.cache.put(self.scope, assets)
        self._invalid.difference_update(assets)
        self.plan()

    def cached_addresses(self) -> dict[str, str]:
        """Expose addresses only to the immediate in-process generation call."""
        plan = self.plan()
        return {
            name: self.cache.get(self.scope, name).address  # type: ignore[union-attr]
            for name in self.required_assets
            if name in plan.reused
        }

    def submit_once(self, create: Callable[[Mapping[str, str]], str | None]) -> str:
        """Call the paid creation callback at most once and return its ID immediately."""
        if self.state == "creation_unknown":
            raise McpRetryError("creation result is unknown; retry is blocked")
        if self.generation_id is not None:
            return self.generation_id
        plan = self.plan()
        if plan.reupload:
            raise McpRetryError("missing or invalid resources: " + ", ".join(plan.reupload))
        addresses = self.cached_addresses()
        self.create_calls += 1
        try:
            generation_id = create(addresses)
        except Exception as exc:
            self.state = "creation_unknown"
            raise McpRetryError("creation result is unknown; retry is blocked") from exc
        if not isinstance(generation_id, str) or not _IDENTIFIER.fullmatch(generation_id):
            self.state = "creation_unknown"
            raise McpRetryError("creation result is unknown; retry is blocked")
        self.generation_id = generation_id
        self.state = "polling"
        return generation_id

    def poll(self, query: Callable[[str], Mapping[str, object]]) -> Mapping[str, object]:
        """Poll the already-created task without touching analysis or prompt state."""
        if self.generation_id is None or self.state not in {"polling", "download_pending"}:
            raise McpRetryError("generation has not been created")
        self.poll_calls += 1
        result = query(self.generation_id)
        if not isinstance(result, Mapping):
            raise McpRetryError("invalid polling result")
        status = result.get("status")
        if status in {"succeed", "failed", "canceled"}:
            self.state = "download_pending" if status == "succeed" else "finished"
        return result

    def download_once(self, download: Callable[[str], object]) -> object:
        """Download the completed result in a later/background step."""
        if self.generation_id is None or self.state != "download_pending":
            raise McpRetryError("generation is not ready for download")
        if self._downloaded:
            return None
        self.download_calls += 1
        result = download(self.generation_id)
        self._downloaded = True
        self.state = "downloaded"
        return result

    def safe_metadata(self) -> dict[str, object]:
        """Return log-safe state; private addresses and upload tickets are omitted."""
        return {
            "sku": self.scope.sku,
            "version": self.scope.version,
            "reference_group": self.scope.reference_group,
            "analysis_sha256": self.scope.analysis_sha256,
            "prompt_sha256": self.scope.prompt_sha256,
            "asset_fingerprint": self.scope.asset_fingerprint,
            "required_assets": self.required_assets,
            "cached_assets": self.cache.names(self.scope),
            "generation_id": self.generation_id,
            "state": self.state,
            "create_calls": self.create_calls,
            "poll_calls": self.poll_calls,
            "download_calls": self.download_calls,
            "analysis_calls": self.analysis_calls,
            "prompt_build_calls": self.prompt_build_calls,
        }
