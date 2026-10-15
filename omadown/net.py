"""HTTP plumbing shared by the native downloaders.

``yt-dlp`` brings its own transport; the HLS / direct / sniffer backends use
this so proxy, cookies and user-agent behave the same everywhere.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


class NetworkError(RuntimeError):
    pass


@dataclass
class NetOptions:
    user_agent: str = DEFAULT_UA
    referer: str = ""
    cookie: str = ""
    proxy: str = ""
    timeout: float = 30.0
    headers: dict[str, str] = field(default_factory=dict)
    insecure: bool = False

    def build_headers(self, url: str) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": self.user_agent or DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
        }
        if self.referer:
            headers["Referer"] = self.referer
        elif url:
            parsed = urllib.parse.urlsplit(url)
            if parsed.scheme and parsed.netloc:
                headers["Referer"] = f"{parsed.scheme}://{parsed.netloc}/"
        if self.cookie:
            headers["Cookie"] = self.cookie
        headers.update(self.headers)
        return headers


def build_opener(opts: NetOptions) -> urllib.request.OpenerDirector:
    handlers: list[urllib.request.BaseHandler] = []
    if opts.proxy:
        handlers.append(urllib.request.ProxyHandler({
            "http": opts.proxy, "https": opts.proxy,
        }))
    else:
        handlers.append(urllib.request.ProxyHandler())  # honour env vars / no proxy
    if opts.insecure:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=ctx))
    return urllib.request.build_opener(*handlers)


def request(
    url: str,
    opts: NetOptions,
    *,
    method: str = "GET",
    data: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    byte_range: tuple[int, int] | None = None,
    opener: urllib.request.OpenerDirector | None = None,
):
    headers = opts.build_headers(url)
    if extra_headers:
        headers.update(extra_headers)
    if byte_range:
        start, length = byte_range
        headers["Range"] = f"bytes={start}-{start + length - 1}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    opener = opener or build_opener(opts)
    try:
        return opener.open(req, timeout=opts.timeout)
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"HTTP {exc.code} for {url}") from exc
    except urllib.error.URLError as exc:
        raise NetworkError(f"{exc.reason} for {url}") from exc
    except TimeoutError as exc:
        raise NetworkError(f"timeout for {url}") from exc


def get_bytes(url: str, opts: NetOptions, **kwargs) -> bytes:
    with request(url, opts, **kwargs) as resp:
        return resp.read()


def get_text(url: str, opts: NetOptions, encoding: str = "utf-8", **kwargs) -> str:
    raw = get_bytes(url, opts, **kwargs)
    return raw.decode(encoding, errors="replace")


def get_json(url: str, opts: NetOptions, **kwargs):
    return json.loads(get_text(url, opts, **kwargs))


def stream_to_file(
    url: str,
    target: Path,
    opts: NetOptions,
    *,
    on_chunk: Callable[[int], None] | None = None,
    resume: bool = True,
    expected_total: int | None = None,
    chunk_size: int = 262144,
    extra_headers: dict[str, str] | None = None,
) -> int:
    """Download ``url`` to ``target`` with HTTP-range resume. Returns bytes written."""
    target.parent.mkdir(parents=True, exist_ok=True)
    start = target.stat().st_size if (resume and target.exists()) else 0
    headers = dict(extra_headers or {})
    if start:
        headers["Range"] = f"bytes={start}-"

    wrote = start
    with request(url, opts, extra_headers=headers) as resp:
        status = getattr(resp, "status", 200)
        if start and status != 206:
            # Server ignored the range: start over rather than corrupt the file.
            start = 0
            wrote = 0
            target.unlink(missing_ok=True)
        mode = "ab" if start else "wb"
        with open(target, mode) as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                wrote += len(chunk)
                if on_chunk:
                    on_chunk(wrote)
    if expected_total and wrote < expected_total:
        raise NetworkError(
            f"short read for {url}: got {wrote} of {expected_total} bytes"
        )
    return wrote


def content_length(url: str, opts: NetOptions) -> int | None:
    try:
        with request(url, opts, method="HEAD") as resp:
            value = resp.headers.get("Content-Length")
            return int(value) if value else None
    except NetworkError:
        return None


def sniff_kind(url: str, content_type: str = "") -> str:
    """Classify a URL by container so the right backend is chosen."""
    lowered = url.lower().split("?")[0]
    if lowered.endswith(".m3u8") or "mpegurl" in content_type:
        return "hls"
    if lowered.endswith(".mpd") or "dash+xml" in content_type:
        return "dash"
    if lowered.endswith((".mp4", ".mkv", ".webm", ".mov", ".m4v", ".flv", ".ts", ".m4s")):
        return "file"
    if lowered.endswith((".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg", ".wav")):
        return "audio"
    return "page"
