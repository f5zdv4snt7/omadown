"""Backend registry: priority order mirrors "site extractor first, sniffing last"."""

from __future__ import annotations

from .base import (  # noqa: F401
    Backend,
    BackendError,
    BackendRegistry,
    CancelToken,
    Cancelled,
    DownloadContext,
    ProbeError,
    Progress,
    ProgressCb,
)
from .direct import DirectBackend
from .hls import HlsBackend, parse_m3u8, select_variant
from .sniff import UgeBackend
from .ytdlp import YtDlpBackend, find_ytdlp

__all__ = [
    "Backend", "BackendError", "BackendRegistry", "CancelToken", "Cancelled",
    "DownloadContext", "ProbeError", "Progress", "ProgressCb",
    "DirectBackend", "HlsBackend", "UgeBackend", "YtDlpBackend",
    "build_registry", "find_ytdlp", "parse_m3u8", "select_variant",
]


def build_registry(*, render: bool = False, enable_uge: bool = True) -> BackendRegistry:
    backends: list[Backend] = [YtDlpBackend(), HlsBackend()]
    if enable_uge:
        uge = UgeBackend()
        uge.render = render
        backends.append(uge)
    backends.append(DirectBackend())
    return BackendRegistry(backends)
