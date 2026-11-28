"""Official Aimaxhug Seedance model and resolution policy."""

from __future__ import annotations


MODEL_RESOLUTIONS: dict[str, frozenset[str]] = {
    "Seedance-2.0-mini": frozenset({"480p", "720p"}),
    "Seedance-2.0-fast": frozenset({"480p", "720p"}),
    "Seedance-2.0-pro": frozenset({"480p", "720p", "1080p", "4k"}),
    "Seedance-2.5": frozenset({"480p", "720p"}),
    "seedance-mini-2": frozenset({"480p", "720p"}),
    "seedance-fast-2": frozenset({"480p", "720p"}),
    "seedance2-0-pro-2": frozenset({"480p", "720p", "1080p"}),
}

SEEDANCE_MODELS = tuple(MODEL_RESOLUTIONS)


def supports_resolution(model: object, resolution: object) -> bool:
    return (
        isinstance(model, str)
        and isinstance(resolution, str)
        and resolution in MODEL_RESOLUTIONS.get(model, ())
    )
