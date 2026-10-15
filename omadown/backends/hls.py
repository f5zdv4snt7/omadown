"""Native HLS (M3U8) download engine.

Downie ships a native HLS engine ("The native HLS Engine is faster, more
powerful and more efficient" / "Use native M3U8 (HLS) download engine"), with
AES-128 key handling, live-stream support and an MPEG-TS container option for
live capture. This module is the equivalent: a self-contained M3U8 parser plus a
fragment downloader with parallel fetches, decryption and muxing.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .. import crypto
from ..models import MediaFormat, MediaInfo, QUALITY_HEIGHTS
from ..net import NetOptions, NetworkError, build_opener, request
from ..util import human_bytes, human_speed
from .base import Backend, BackendError, DownloadContext, ProbeError, ProgressCb

MAX_LIVE_SECONDS = 6 * 3600       # safety valve for endless live captures
LIVE_POLL_MARGIN = 1.0


class HlsParseError(BackendError):
    pass


# ------------------------------------------------------------------- data model

@dataclass
class HlsKey:
    method: str = "NONE"
    uri: str = ""
    iv: bytes | None = None
    key_format: str = "identity"


@dataclass
class HlsRendition:
    kind: str = "AUDIO"
    group_id: str = ""
    name: str = ""
    language: str = ""
    uri: str = ""
    default: bool = False
    autoselect: bool = False


@dataclass
class HlsVariant:
    uri: str = ""
    bandwidth: int = 0
    average_bandwidth: int = 0
    width: int = 0
    height: int = 0
    codecs: str = ""
    frame_rate: float = 0.0
    audio_group: str = ""
    name: str = ""
    resolution: str = ""

    def as_format(self, index: int) -> MediaFormat:
        return MediaFormat(
            format_id=f"hls-{index}",
            ext="ts",
            resolution=self.resolution or (f"{self.height}p" if self.height else ""),
            height=self.height,
            width=self.width,
            fps=self.frame_rate,
            vcodec=self.codecs or "h264",
            acodec="none",
            tbr=(self.bandwidth / 1000.0) if self.bandwidth else None,
            protocol="m3u8_native",
            url=self.uri,
            note=self.name,
        )


@dataclass
class HlsSegment:
    uri: str = ""
    duration: float = 0.0
    title: str = ""
    byte_range: tuple[int, int] | None = None
    key: HlsKey | None = None
    map_uri: str | None = None
    discontinuity: bool = False
    sequence: int = 0
    index: int = 0


@dataclass
class HlsPlaylist:
    kind: str = "media"                 # master | media
    variants: list[HlsVariant] = field(default_factory=list)
    renditions: list[HlsRendition] = field(default_factory=list)
    segments: list[HlsSegment] = field(default_factory=list)
    target_duration: float = 0.0
    media_sequence: int = 0
    version: int = 0
    endlist: bool = False
    playlist_type: str = ""
    map_uri: str | None = None

    @property
    def is_live(self) -> bool:
        return not self.endlist and self.playlist_type.upper() != "VOD"

    @property
    def total_duration(self) -> float:
        return sum(s.duration for s in self.segments)


# ------------------------------------------------------------------ M3U8 parser

def parse_attributes(text: str) -> dict[str, str]:
    """Parse ``KEY=VALUE,KEY="VALUE,WITH,COMMAS"`` attribute lists."""
    result: dict[str, str] = {}
    i, n = 0, len(text)
    while i < n:
        eq = text.find("=", i)
        if eq == -1:
            break
        key = text[i:eq].strip()
        i = eq + 1
        if i < n and text[i] == '"':
            close = text.find('"', i + 1)
            if close == -1:
                value, i = text[i + 1:], n
            else:
                value, i = text[i + 1:close], close + 1
            while i < n and text[i] != ",":
                i += 1
        else:
            comma = text.find(",", i)
            if comma == -1:
                value, i = text[i:], n
            else:
                value, i = text[i:comma], comma
        if key:
            result[key.upper()] = value
        if i < n and text[i] == ",":
            i += 1
    return result


def parse_m3u8(text: str, base_url: str = "") -> HlsPlaylist:
    lines = text.splitlines()
    if not any(line.strip().startswith("#EXTM3U") for line in lines[:3]):
        raise HlsParseError("not an M3U8 playlist (missing #EXTM3U)")

    playlist = HlsPlaylist()
    pending_variant: dict[str, str] | None = None
    pending_key: HlsKey | None = None
    pending_duration = 0.0
    pending_title = ""
    pending_range: tuple[int, int] | None = None
    pending_discontinuity = False
    current_map = playlist.map_uri
    sequence = 0
    last_range_end = 0

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if not line.startswith("#"):
            uri = urljoin(base_url, line) if base_url else line
            if pending_variant is not None:
                variant = _variant_from_attrs(pending_variant, uri)
                playlist.variants.append(variant)
                pending_variant = None
            else:
                if pending_range and pending_range[0] is None:
                    pending_range = (last_range_end, pending_range[1])
                if pending_range:
                    last_range_end = pending_range[0] + pending_range[1]
                playlist.segments.append(HlsSegment(
                    uri=uri,
                    duration=pending_duration,
                    title=pending_title,
                    byte_range=pending_range,
                    key=pending_key,
                    map_uri=current_map,
                    discontinuity=pending_discontinuity,
                    sequence=sequence,
                    index=len(playlist.segments),
                ))
                sequence += 1
                pending_duration, pending_title = 0.0, ""
                pending_range, pending_discontinuity = None, False
            continue

        tag, _, value = line.partition(":")
        tag = tag.strip().upper()
        value = value.strip()

        if tag == "#EXT-X-VERSION":
            playlist.version = _to_int(value, 0)
        elif tag == "#EXT-X-TARGETDURATION":
            playlist.target_duration = _to_float(value, 0.0)
        elif tag == "#EXT-X-MEDIA-SEQUENCE":
            playlist.media_sequence = _to_int(value, 0)
            sequence = playlist.media_sequence
        elif tag == "#EXT-X-PLAYLIST-TYPE":
            playlist.playlist_type = value.upper()
        elif tag == "#EXT-X-ENDLIST":
            playlist.endlist = True
        elif tag == "#EXT-X-KEY":
            attrs = parse_attributes(value)
            method = (attrs.get("METHOD") or "NONE").upper()
            if method == "NONE":
                pending_key = None
            else:
                pending_key = HlsKey(
                    method=method,
                    uri=urljoin(base_url, attrs.get("URI", "")) if attrs.get("URI") else "",
                    iv=crypto.parse_hex_iv(attrs.get("IV")),
                    key_format=attrs.get("KEYFORMAT", "identity"),
                )
        elif tag == "#EXT-X-MAP":
            attrs = parse_attributes(value)
            if attrs.get("URI"):
                current_map = urljoin(base_url, attrs["URI"])
                playlist.map_uri = current_map
        elif tag == "#EXT-X-MEDIA":
            attrs = parse_attributes(value)
            playlist.renditions.append(HlsRendition(
                kind=attrs.get("TYPE", "AUDIO").upper(),
                group_id=attrs.get("GROUP-ID", ""),
                name=attrs.get("NAME", ""),
                language=attrs.get("LANGUAGE", ""),
                uri=urljoin(base_url, attrs["URI"]) if attrs.get("URI") else "",
                default=attrs.get("DEFAULT", "NO").upper() == "YES",
                autoselect=attrs.get("AUTOSELECT", "NO").upper() == "YES",
            ))
        elif tag == "#EXT-X-STREAM-INF":
            pending_variant = parse_attributes(value)
        elif tag == "#EXTINF":
            duration, _, title = value.partition(",")
            pending_duration = _to_float(duration, 0.0)
            pending_title = title.strip()
        elif tag == "#EXT-X-BYTERANGE":
            length, _, offset = value.partition("@")
            pending_range = (_to_int(offset, None) if offset else None, _to_int(length, 0))
        elif tag == "#EXT-X-DISCONTINUITY":
            pending_discontinuity = True
        # unknown tags are intentionally ignored (spec says decoders must)

    playlist.kind = "master" if playlist.variants else "media"
    return playlist


def _variant_from_attrs(attrs: dict[str, str], uri: str) -> HlsVariant:
    resolution = attrs.get("RESOLUTION", "")
    width = height = 0
    if "x" in resolution.lower():
        try:
            width, height = (int(part) for part in resolution.lower().split("x", 1))
        except ValueError:
            width = height = 0
    return HlsVariant(
        uri=uri,
        bandwidth=_to_int(attrs.get("BANDWIDTH"), 0),
        average_bandwidth=_to_int(attrs.get("AVERAGE-BANDWIDTH"), 0),
        width=width,
        height=height,
        resolution=resolution,
        codecs=attrs.get("CODECS", ""),
        frame_rate=_to_float(attrs.get("FRAME-RATE"), 0.0),
        audio_group=attrs.get("AUDIO", ""),
        name=attrs.get("NAME", ""),
    )


def _to_int(value: str | None, default: int | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _to_float(value: str | None, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def select_variant(
    playlist: HlsPlaylist,
    *,
    quality: str = "best",
    max_resolution: int = 0,
) -> HlsVariant:
    """Pick a rendition, honouring the max-resolution cap the same way Downie does.

    Downie: "If you limit the downloads to e.g. 480p and only larger videos are
    available, then the smallest available video will be chosen."
    """
    variants = [v for v in playlist.variants if v.uri]
    if not variants:
        raise HlsParseError("master playlist contains no variants")
    variants.sort(key=lambda v: (v.height, v.bandwidth))

    if quality in ("worst", "144p"):
        return variants[0]
    if quality == "best" and not max_resolution:
        return variants[-1]

    target = QUALITY_HEIGHTS.get(quality, 0)
    cap = max_resolution or target
    if not cap:
        return variants[-1]

    within = [v for v in variants if v.height and v.height <= cap]
    if within:
        return within[-1]
    return variants[0]


# ------------------------------------------------------------------- downloader

@dataclass
class HlsPlan:
    media_url: str
    audio_url: str = ""
    variant: HlsVariant | None = None
    audio: HlsRendition | None = None


class HlsBackend(Backend):
    name = "hls"
    priority = 20

    def claim(self, url: str) -> bool:
        lowered = url.lower()
        if ".m3u8" in lowered:
            return True
        return urlsplit(lowered).path.endswith(("/master", "/index", "/playlist")) and "m3u8" in lowered

    # ------------------------------------------------------------------- probing
    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        opts = self._net_options(ctx)
        text = self._fetch_text(url, opts)
        playlist = parse_m3u8(text, url)

        info = MediaInfo(
            url=url, title=_title_from_url(url), extractor="hls",
            webpage_url=url, is_live=playlist.is_live,
            duration=playlist.total_duration or None,
        )
        if playlist.kind == "master":
            info.formats = [v.as_format(i) for i, v in enumerate(playlist.variants)]
            chosen = select_variant(
                playlist,
                quality=ctx.item.options.quality,
                max_resolution=ctx.item.options.max_resolution,
            )
            info.height = chosen.height
            info.width = chosen.width
            info.title = f"{info.title} [{chosen.resolution or chosen.bandwidth}]"
            ctx.item.meta["hls_plan"] = {
                "media_url": chosen.uri,
                "variant_resolution": chosen.resolution,
                "audio_group": chosen.audio_group,
            }
            info.is_live = self._is_live(chosen.uri, opts)
        else:
            info.formats = [MediaFormat(
                format_id="hls-media", ext="ts", protocol="m3u8_native",
                vcodec="h264", acodec="aac", url=url,
                note="live" if playlist.is_live else "",
            )]
            info.is_live = playlist.is_live
        return info

    # ------------------------------------------------------------------ download
    def download(self, ctx: DownloadContext, progress: ProgressCb) -> Path:
        item = ctx.item
        options = item.options
        opts = self._net_options(ctx)
        opener = build_opener(opts)

        plan = self._plan(item.url, ctx, opts)
        workdir = ctx.workdir
        workdir.mkdir(parents=True, exist_ok=True)

        progress_stage = "downloading"
        self.emit(progress, stage="preparing", message="读取播放列表", fraction=0.0)

        media_playlist = self._fetch_playlist(plan.media_url, opts)
        is_live = media_playlist.is_live
        total_expected = None if is_live else len(media_playlist.segments)

        segment_dir = workdir / "segments"
        segment_dir.mkdir(exist_ok=True)

        done: dict[int, Path] = {}
        bytes_downloaded = 0
        started = time.monotonic()
        lock = threading.Lock()
        audio_path: Path | None = None

        if plan.audio_url:
            audio_path = self._download_track(
                plan.audio_url, segment_dir / "audio", "audio", opts,
                ctx, progress, lambda n: None, is_live=is_live,
            )

        seen_sequences: set[int] = set()
        segments = list(media_playlist.segments)
        live_deadline = started + MAX_LIVE_SECONDS

        while True:
            fresh = [s for s in segments if s.sequence not in seen_sequences]
            if fresh:
                bytes_downloaded = self._download_segments(
                    fresh, segment_dir, opts, ctx, progress,
                    done, lock, bytes_downloaded, total_expected, started,
                )
                seen_sequences.update(s.sequence for s in fresh)

            if not is_live:
                break
            if time.monotonic() > live_deadline:
                break
            self.emit(
                progress, stage="downloading",
                downloaded=bytes_downloaded, total=None,
                message=f"直播录制中 · 已获取 {len(seen_sequences)} 个分片",
            )
            wait = max(1.0, (media_playlist.target_duration or 6.0) / 2.0 + LIVE_POLL_MARGIN)
            ctx.item.options  # noqa: B018 - keep reference clear
            self._sleep(ctx, min(wait, 10.0))
            try:
                refreshed = self._fetch_playlist(plan.media_url, opts)
            except NetworkError:
                continue
            media_playlist = refreshed
            is_live = refreshed.is_live
            segments.extend(s for s in refreshed.segments
                            if s.sequence not in {x.sequence for x in segments})

        ordered = sorted(done.items())
        if not ordered:
            raise BackendError("no segments were downloaded")

        suffix = ".ts" if (is_live and not media_playlist.map_uri) else (".mp4" if media_playlist.map_uri else ".ts")
        output = workdir / f"output{suffix}"

        self.emit(progress, stage="postprocessing", message="合并分片", fraction=None)
        with open(output, "wb") as handle:
            if media_playlist.map_uri:
                handle.write(self._fetch_bytes(media_playlist.map_uri, opts))
            for _, path in ordered:
                with open(path, "rb") as part:
                    while True:
                        chunk = part.read(1 << 20)
                        if not chunk:
                            break
                        handle.write(chunk)

        if audio_path and audio_path.exists():
            merged = workdir / f"output.muxed{suffix}"
            if self._mux(output, audio_path, merged, ctx):
                output = merged

        item.meta["hls_is_live"] = is_live
        item.meta["hls_segments"] = len(ordered)
        return output

    # ------------------------------------------------------------------- helpers
    def _download_track(
        self, media_url: str, segment_dir: Path, label: str, opts: NetOptions,
        ctx: DownloadContext, progress: ProgressCb, extra, *, is_live: bool,
    ) -> Path | None:
        playlist = self._fetch_playlist(media_url, opts)
        segment_dir.mkdir(parents=True, exist_ok=True)
        done: dict[int, Path] = {}
        lock = threading.Lock()
        self._download_segments(
            playlist.segments, segment_dir, opts, ctx, progress,
            done, lock, 0, len(playlist.segments), time.monotonic(),
            prefix=label,
        )
        if not done:
            return None
        target = segment_dir.parent / f"{label}.ts"
        with open(target, "wb") as handle:
            if playlist.map_uri:
                handle.write(self._fetch_bytes(playlist.map_uri, opts))
            for _, path in sorted(done.items()):
                handle.write(path.read_bytes())
        return target

    def _download_segments(
        self,
        segments: list[HlsSegment],
        segment_dir: Path,
        opts: NetOptions,
        ctx: DownloadContext,
        progress: ProgressCb,
        done: dict[int, Path],
        lock: threading.Lock,
        bytes_so_far: int,
        total_segments: int | None,
        started: float,
        prefix: str = "seg",
    ) -> int:
        if not segments:
            return bytes_so_far
        connections = max(1, min(16, ctx.item.options.connections or 4))
        # Byte-range segments reference one resource, so they stay sequential.
        if any(s.byte_range for s in segments):
            connections = 1

        key_cache: dict[str, bytes] = {}
        key_lock = threading.Lock()

        def fetch(segment: HlsSegment) -> tuple[HlsSegment, Path, int]:
            self._raise_if_cancelled(ctx)
            data = self._fetch_bytes(segment.uri, opts, byte_range=segment.byte_range)
            size = len(data)
            if segment.key and segment.key.method.upper() in ("AES-128", "AES-192", "AES-256"):
                with key_lock:
                    key = key_cache.get(segment.key.uri)
                if key is None:
                    key = self._fetch_bytes(segment.key.uri, opts)
                    with key_lock:
                        key_cache[segment.key.uri] = key
                iv = segment.key.iv or crypto.iv_from_sequence(segment.sequence)
                try:
                    data = crypto.decrypt_segment(data, key, iv)
                except crypto.DecryptionError as exc:
                    raise BackendError(f"无法解密分片 {segment.index}: {exc}") from exc
            path = segment_dir / f"{prefix}_{segment.sequence:08d}.part"
            path.write_bytes(data)
            return segment, path, size

        with ThreadPoolExecutor(max_workers=connections, thread_name_prefix="hls") as pool:
            futures = {pool.submit(fetch, seg): seg for seg in segments}
            for future in as_completed(futures):
                segment, path, size = future.result()
                with lock:
                    done[segment.sequence] = path
                    bytes_so_far += size
                    count = len(done)
                self.emit(
                    progress,
                    stage="downloading",
                    downloaded=bytes_so_far,
                    total=None,
                    started=started,
                    message=(
                        f"{count}/{total_segments or '?'} 分片"
                        if total_segments else f"{count} 分片"
                    ),
                    fraction=(count / total_segments) if total_segments else None,
                )
        return bytes_so_far

    def _plan(self, url: str, ctx: DownloadContext, opts: NetOptions) -> HlsPlan:
        cached = ctx.item.meta.get("hls_plan") or {}
        text = self._fetch_text(cached.get("media_url") or url, opts)
        playlist = parse_m3u8(text, cached.get("media_url") or url)

        plan = HlsPlan(media_url=cached.get("media_url") or url)
        if playlist.kind == "master":
            variant = select_variant(
                playlist,
                quality=ctx.item.options.quality,
                max_resolution=ctx.item.options.max_resolution,
            )
            plan.variant = variant
            plan.media_url = variant.uri
            if variant.audio_group:
                candidates = [r for r in playlist.renditions
                              if r.kind == "AUDIO" and r.group_id == variant.audio_group and r.uri]
                if candidates:
                    preferred = next((r for r in candidates if r.default), candidates[0])
                    plan.audio = preferred
                    plan.audio_url = preferred.uri
        return plan

    def _fetch_playlist(self, url: str, opts: NetOptions) -> HlsPlaylist:
        return parse_m3u8(self._fetch_text(url, opts), url)

    def _is_live(self, media_url: str, opts: NetOptions) -> bool:
        """A stream is live when its media playlist has no ``#EXT-X-ENDLIST``."""
        try:
            return self._fetch_playlist(media_url, opts).is_live
        except (BackendError, HlsParseError):
            return False

    def _fetch_text(self, url: str, opts: NetOptions) -> str:
        return self._fetch_bytes(url, opts).decode("utf-8", errors="replace")

    def _fetch_bytes(
        self, url: str, opts: NetOptions, *, byte_range: tuple[int, int] | None = None,
        attempts: int = 3,
    ) -> bytes:
        last: Exception | None = None
        for attempt in range(attempts):
            try:
                with request(url, opts, byte_range=byte_range) as resp:
                    return resp.read()
            except (NetworkError, OSError) as exc:
                last = exc
                time.sleep(0.5 * (attempt + 1))
        raise BackendError(f"分片获取失败: {url} ({last})")

    def _net_options(self, ctx: DownloadContext) -> NetOptions:
        options = ctx.item.options
        base = ctx.config.network if ctx.config else None
        headers = {"Accept": "*/*"}
        return NetOptions(
            user_agent=(base.user_agent if base and base.user_agent else NetOptions().user_agent),
            referer=ctx.item.meta.get("referer", "") or ctx.item.url,
            cookie=ctx.item.meta.get("cookie", ""),
            proxy=options.proxy or (base.proxy if base else ""),
            timeout=float(base.timeout) if base else 30.0,
            headers=headers,
        )

    def _sleep(self, ctx: DownloadContext, seconds: float) -> None:
        token = ctx.options.get("cancel")
        if token is not None:
            token.sleep(seconds)
        else:
            time.sleep(seconds)

    def _raise_if_cancelled(self, ctx: DownloadContext) -> None:
        token = ctx.options.get("cancel")
        if token is not None:
            token.raise_if_cancelled()

    def _mux(self, video: Path, audio: Path, target: Path, ctx: DownloadContext) -> bool:
        ffmpeg = ctx.ffmpeg
        if not ffmpeg:
            return False
        from ..util import run
        proc = run([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(video), "-i", str(audio),
            "-map", "0:v:0?", "-map", "1:a:0?", "-c", "copy", str(target),
        ])
        return proc.returncode == 0 and target.exists()


def _title_from_url(url: str) -> str:
    path = urlsplit(url).path
    stem = path.rsplit("/", 1)[-1] or "stream"
    stem = re.sub(r"\.(m3u8|mpd)$", "", stem, flags=re.I)
    return stem or "stream"
