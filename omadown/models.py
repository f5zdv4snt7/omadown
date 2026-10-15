"""Domain model: what we know about a media item and a queued download."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class State(str, Enum):
    QUEUED = "queued"
    PREPARING = "preparing"
    DOWNLOADING = "downloading"
    POSTPROCESSING = "postprocessing"
    DONE = "done"
    FAILED = "failed"
    PAUSED = "paused"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (State.DONE, State.FAILED, State.CANCELLED)

    @property
    def is_active(self) -> bool:
        return self in (State.PREPARING, State.DOWNLOADING, State.POSTPROCESSING)


class Priority(int, Enum):
    HIGH = 0
    NORMAL = 1
    LOW = 2


# Downie exposes these exact buckets in its quality menu.
QUALITY_HEIGHTS: dict[str, int] = {
    "4k": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "720p": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
}
QUALITY_ALIASES = {
    "2160": "4k", "2160p": "4k", "uhd": "4k",
    "1440": "1440p", "2k": "1440p",
    "1080": "1080p", "fhd": "1080p",
    "720": "720p", "hd": "720p",
    "480": "480p", "360": "360p", "240": "240p", "144": "144p",
    "best": "best", "max": "best", "worst": "worst", "audio": "audio",
}


@dataclass
class MediaFormat:
    format_id: str = ""
    ext: str = ""
    resolution: str = ""
    height: int = 0
    width: int = 0
    fps: float = 0.0
    vcodec: str = "none"
    acodec: str = "none"
    filesize: int | None = None
    tbr: float | None = None
    abr: float | None = None
    protocol: str = ""
    url: str = ""
    note: str = ""
    language: str = ""

    @property
    def is_audio_only(self) -> bool:
        return self.vcodec in ("none", "", None) and self.acodec not in ("none", "", None)

    @property
    def is_video_only(self) -> bool:
        return self.acodec in ("none", "", None) and self.vcodec not in ("none", "", None)

    @property
    def label(self) -> str:
        bits = [self.resolution or self.ext or self.format_id]
        if self.fps and self.fps >= 50:
            bits.append(f"{self.fps:.0f}fps")
        if self.vcodec and self.vcodec != "none":
            bits.append(self.vcodec.split(".")[0])
        if self.is_audio_only:
            bits = [self.ext or "audio"]
            if self.abr:
                bits.append(f"{self.abr:.0f}kbps")
        if self.filesize:
            from .util import human_bytes
            bits.append(human_bytes(self.filesize))
        return " | ".join(bits)


@dataclass
class MediaInfo:
    """Result of probing a URL, before it becomes a queued download."""

    url: str
    title: str = ""
    id: str = ""
    uploader: str = ""
    duration: float | None = None
    thumbnail: str = ""
    extractor: str = ""
    webpage_url: str = ""
    description: str = ""
    is_playlist: bool = False
    playlist_title: str = ""
    playlist_index: int | None = None
    playlist_count: int | None = None
    is_live: bool = False
    formats: list[MediaFormat] = field(default_factory=list)
    entries: list["MediaInfo"] = field(default_factory=list)
    width: int = 0
    height: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    def meta(self) -> dict[str, Any]:
        return {
            "title": self.title or self.id or "untitled",
            "id": self.id,
            "uploader": self.uploader,
            "duration": self.duration,
            "extractor": self.extractor,
            "resolution": f"{self.height}p" if self.height else "",
            "quality": f"{self.height}p" if self.height else "",
            "playlist": self.playlist_title,
            "playlist_index": self.playlist_index,
            "date": "",
        }


@dataclass
class DownloadOptions:
    """Per-download overrides. None means "inherit from config"."""

    quality: str = "best"
    audio_only: bool = False
    audio_format: str = ""          # m4a / mp3 / flac / opus / wav
    max_resolution: int = 0         # 0 = unlimited, mirrors Downie's "Use 0 for unlimited"
    prefer_mp4: bool | None = None
    force_mp4: bool | None = None
    subtitles: bool | None = None
    subtitle_langs: list[str] | None = None
    subtitle_mode: str = ""         # single / multiple
    embed_subs: bool | None = None
    include_generated_subs: bool | None = None
    subs_only: bool = False
    playlist: bool | None = None
    playlist_items: str = ""        # "1-20" / "1,3,5" / "" for all
    rate_limit: str = ""            # "5M", "500K"
    proxy: str = ""
    cookies_from_browser: str = ""
    cookies_file: str = ""
    connections: int = 0
    output_dir: str = ""
    name_format: str = ""
    postprocess: bool | None = None
    hls_mode: str = ""              # remux / reencode / none
    transcode: str = ""             # "" / h264 / h265
    keep_original: bool | None = None
    custom_script: str = ""
    write_metadata: bool | None = None
    write_thumbnail: bool | None = None
    priority: int = Priority.NORMAL.value
    tag: str = ""
    #: force a specific engine ("" = pick automatically). Mirrors Downie's
    #: "Use native M3U8 (HLS) download engine" toggle.
    backend: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "DownloadOptions":
        data = dict(data or {})
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass
class DownloadItem:
    """A row in the queue / history."""

    id: int | None = None
    url: str = ""
    title: str = ""
    state: str = State.QUEUED.value
    backend: str = ""
    progress: float = 0.0
    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    stage: str = ""
    error: str = ""
    output_path: str = ""
    extractor: str = ""
    playlist_title: str = ""
    playlist_index: int | None = None
    duration: float | None = None
    attempts: int = 0
    priority: int = Priority.NORMAL.value
    options: DownloadOptions = field(default_factory=DownloadOptions)
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancelled: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    # ---------------------------------------------------------------- behaviour
    @property
    def state_enum(self) -> State:
        try:
            return State(self.state)
        except ValueError:
            return State.QUEUED

    def mark(self, state: State, **kwargs: Any) -> None:
        self.state = state.value
        for key, value in kwargs.items():
            setattr(self, key, value)

    def reset_for_retry(self) -> None:
        self.cancelled = False
        self.error = ""
        self.progress = 0.0
        self.downloaded = 0
        self.speed = None
        self.eta = None
        self.stage = ""
        self.started_at = None
        self.finished_at = None
        self.state = State.QUEUED.value

    def progress_fraction(self) -> float | None:
        if self.total:
            return min(1.0, self.downloaded / self.total)
        return None

    def to_row(self) -> tuple:
        from json import dumps
        return (
            self.url, self.title, self.state, self.backend, self.progress,
            self.downloaded, self.total, self.error, self.output_path,
            self.extractor, self.playlist_title, self.playlist_index,
            self.duration, self.attempts, self.priority,
            dumps(self.options.to_dict()),
            self.created_at, self.started_at, self.finished_at,
            dumps(self.meta),
        )

    @classmethod
    def from_row(cls, row) -> "DownloadItem":
        from json import loads
        return cls(
            id=row["id"], url=row["url"], title=row["title"], state=row["state"],
            backend=row["backend"], progress=row["progress"] or 0.0,
            downloaded=row["downloaded"] or 0, total=row["total"],
            error=row["error"] or "", output_path=row["output_path"] or "",
            extractor=row["extractor"] or "", playlist_title=row["playlist_title"] or "",
            playlist_index=row["playlist_index"], duration=row["duration"],
            attempts=row["attempts"] or 0, priority=row["priority"] or Priority.NORMAL.value,
            options=DownloadOptions.from_dict(_safe_json(loads, row["options"])),
            created_at=row["created_at"] or 0.0, started_at=row["started_at"],
            finished_at=row["finished_at"],
            meta=_safe_json(loads, row["meta"]) or {},
        )


def _safe_json(loader, value):
    if not value:
        return {}
    try:
        return loader(value)
    except Exception:
        return {}
