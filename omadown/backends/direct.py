"""Plain HTTP(S) file downloader with resume, used for direct media links.

Also the last-resort backend: if yt-dlp is missing, a straight ``.mp4`` link still
works.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlsplit, unquote

from ..models import MediaFormat, MediaInfo
from ..net import NetOptions, NetworkError, content_length, stream_to_file
from ..util import human_speed
from .base import Backend, BackendError, DownloadContext, ProbeError, ProgressCb


class DirectBackend(Backend):
    name = "direct"
    priority = 90

    def claim(self, url: str) -> bool:
        return url.startswith(("http://", "https://"))

    # ------------------------------------------------------------------- probing
    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        opts = self._net_options(ctx)
        size = content_length(url, opts)
        name = _filename_from_url(url)
        return MediaInfo(
            url=url,
            title=Path(name).stem or "download",
            extractor="direct",
            webpage_url=url,
            formats=[MediaFormat(
                format_id="direct", ext=Path(name).suffix.lstrip(".") or "bin",
                filesize=size, protocol="https", url=url,
            )],
        )

    # ------------------------------------------------------------------ download
    def download(self, ctx: DownloadContext, progress: ProgressCb) -> Path:
        token = ctx.options.get("cancel")
        opts = self._net_options(ctx)
        name = ctx.item.meta.get("filename") or _filename_from_url(ctx.item.url)
        target = ctx.workdir / name
        target.parent.mkdir(parents=True, exist_ok=True)

        total = ctx.item.total or content_length(ctx.item.url, opts)
        started = time.monotonic()
        last = [0.0]

        def on_chunk(written: int) -> None:
            if token is not None:
                token.raise_if_cancelled()
            now = time.monotonic()
            if now - last[0] < 0.15:
                return
            last[0] = now
            self.emit(progress, stage="downloading", downloaded=written, total=total, started=started)

        try:
            written = stream_to_file(
                ctx.item.url, target, opts, on_chunk=on_chunk, expected_total=total
            )
        except NetworkError as exc:
            raise BackendError(str(exc)) from exc

        self.emit(progress, stage="downloading", downloaded=written, total=written or total,
                  fraction=1.0, started=started)
        return target

    def _net_options(self, ctx: DownloadContext) -> NetOptions:
        options = ctx.item.options
        network = ctx.config.network if ctx.config else None
        return NetOptions(
            user_agent=(network.user_agent if network and network.user_agent else NetOptions().user_agent),
            referer=ctx.item.meta.get("referer", "") or ctx.item.url,
            cookie=ctx.item.meta.get("cookie", ""),
            proxy=options.proxy or (network.proxy if network else ""),
            timeout=float(network.timeout) if network else 30.0,
        )


def _filename_from_url(url: str) -> str:
    path = urlsplit(url).path
    name = unquote(path.rsplit("/", 1)[-1]) or "download"
    if "." not in name:
        name += ".mp4"
    return name.replace("/", "_")
