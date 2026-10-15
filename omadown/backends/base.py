"""Backend contract shared by every downloader."""

from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..models import DownloadItem, MediaInfo


class BackendError(RuntimeError):
    """Download failed in a way the user should see verbatim."""


class ProbeError(BackendError):
    """The URL could not be understood at all."""


class Cancelled(BackendError):
    """Raised inside a backend when the user asked to stop."""


@dataclass
class Progress:
    stage: str = "downloading"
    fraction: float | None = None
    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    message: str = ""


ProgressCb = Callable[[Progress], None]


class CancelToken:
    """Cooperative cancellation that also kills spawned subprocesses."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._procs: list = []
        self._lock = threading.Lock()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        from ..util import terminate
        for proc in procs:
            try:
                terminate(proc)
            except Exception:
                pass

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise Cancelled("cancelled by user")

    def bind(self, proc) -> None:
        with self._lock:
            self._procs.append(proc)
        if self._event.is_set():
            from ..util import terminate
            terminate(proc)

    def release(self, proc) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def sleep(self, seconds: float, step: float = 0.2) -> None:
        """Sleep that wakes early when cancelled."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.raise_if_cancelled()
            time.sleep(min(step, max(0.0, deadline - time.monotonic())))


@dataclass
class DownloadContext:
    """Everything a backend needs to run one job."""

    item: DownloadItem
    workdir: Path
    options: dict = field(default_factory=dict)
    net: object = None
    ffmpeg: str | None = None
    config: object = None
    env: dict[str, str] = field(default_factory=dict)


class Backend(ABC):
    """Base class for the four download strategies.

    ``priority`` decides which backend wins when several claim a URL; yt-dlp's
    site extractors (Downie's equivalents) always outrank generic sniffing.
    """

    name: str = "base"
    priority: int = 50

    def __init__(self) -> None:
        self.available_errors: str = ""

    # ------------------------------------------------------------------ contract
    @classmethod
    def available(cls) -> bool:
        return True

    def claim(self, url: str) -> bool:
        return False

    @abstractmethod
    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        """Resolve metadata and format list without downloading."""

    @abstractmethod
    def download(self, ctx: DownloadContext, progress: ProgressCb) -> Path:
        """Fetch the media. Return the final path on disk."""

    # -------------------------------------------------------------------- helper
    @staticmethod
    def emit(
        progress: ProgressCb,
        *,
        stage: str = "downloading",
        downloaded: int = 0,
        total: int | None = None,
        started: float | None = None,
        message: str = "",
        fraction: float | None = None,
        speed: float | None = None,
        eta: float | None = None,
    ) -> None:
        if fraction is None and total:
            fraction = min(1.0, downloaded / total)
        if speed is None and started and downloaded:
            elapsed = max(1e-6, time.monotonic() - started)
            speed = downloaded / elapsed
        if eta is None and speed and total:
            eta = max(0.0, (total - downloaded) / speed)
        progress(Progress(
            stage=stage, fraction=fraction, downloaded=downloaded, total=total,
            speed=speed, eta=eta, message=message,
        ))


class BackendRegistry:
    """Ordered backend list; first claim wins, generic backends last."""

    def __init__(self, backends: Iterable[Backend]) -> None:
        self.backends = sorted(backends, key=lambda b: b.priority)

    def for_url(self, url: str) -> Backend:
        for backend in self.backends:
            if backend.available() and backend.claim(url):
                return backend
        raise ProbeError(f"no backend can handle {url}")

    def by_name(self, name: str) -> Backend:
        for backend in self.backends:
            if backend.name == name:
                return backend
        raise KeyError(name)

    def all(self) -> list[Backend]:
        return list(self.backends)
