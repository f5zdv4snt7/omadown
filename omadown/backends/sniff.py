"""User-Guided Extraction equivalent.

Downie: "The User-Guided Extraction is a powerful feature that can extract videos
that Downie is unable to extract on its own" - it loads the page in an embedded
browser and watches which media resources the page pulls in. We do the same in two
tiers:

* ``static`` - fetch the HTML and mine ``<video>``/``<source>``/OpenGraph/JSON-LD
  plus inline scripts for manifests and media files.
* ``render`` - when Playwright is installed, actually run the page in headless
  Chromium and capture every ``.m3u8``/``.mpd``/media request it makes. This is
  the closest analogue of Downie's embedded browser.
"""

from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..models import MediaFormat, MediaInfo
from ..net import NetOptions, NetworkError, get_text, sniff_kind
from .base import Backend, BackendError, DownloadContext, ProbeError, ProgressCb
from .direct import DirectBackend
from .hls import HlsBackend

MEDIA_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".mov", ".flv", ".ts", ".m4s")
AUDIO_EXT = (".mp3", ".m4a", ".aac", ".flac", ".opus", ".ogg", ".wav")

MANIFEST_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^\s"'<>\\)]+?\.(?:m3u8|mpd)(?:\?[^\s"'<>\\)]*)?)""",
    re.IGNORECASE,
)
MEDIA_URL_RE = re.compile(
    r"""(?P<url>(?:https?:)?//[^\s"'<>\\)]+?(?:""" +
    "|".join(ext.lstrip(".") for ext in MEDIA_EXT + AUDIO_EXT) +
    r""")(?:\?[^\s"'<>\\)]*)?)""",
    re.IGNORECASE,
)
META_KEYS = (
    "og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream",
    "og:audio:secure_url", "og:audio", "video_src", "twitter:player",
)
IFRAME_RE = re.compile(r"""<iframe[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
SOURCE_RE = re.compile(r"""<(?:video|source|audio)[^>]+src=["']([^"']+)["']""", re.IGNORECASE)
META_TAG_RE = re.compile(
    r"""<meta[^>]+(?:property|name|itemprop)=["']([^"']+)["'][^>]+content=["']([^"']*)["']""",
    re.IGNORECASE,
)
JSONLD_RE = re.compile(
    r"""<script[^>]+type=["']application/ld\+json["'][^>]*>(.*?)</script>""",
    re.DOTALL | re.IGNORECASE,
)

BROWSER_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


@dataclass
class Candidate:
    url: str
    kind: str = "file"          # hls | dash | file | audio
    label: str = ""
    height: int = 0
    source: str = ""
    score: int = 0

    def as_format(self, index: int) -> MediaFormat:
        return MediaFormat(
            format_id=f"uge-{index}",
            ext=self.kind,
            resolution=f"{self.height}p" if self.height else "",
            height=self.height,
            protocol={"hls": "m3u8_native", "dash": "dash"}.get(self.kind, "https"),
            url=self.url,
            note=f"{self.source}{' · ' + self.label if self.label else ''}",
        )


class UgeBackend(Backend):
    name = "uge"
    priority = 30

    #: set by the CLI's ``--render`` flag / config
    render: bool = False

    def __init__(self) -> None:
        super().__init__()
        self.last_title = ""

    @classmethod
    def available(cls) -> bool:
        return True

    def claim(self, url: str) -> bool:
        if not url.startswith(("http://", "https://")):
            return False
        return sniff_kind(url) == "page"

    # ------------------------------------------------------------------- probing
    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        candidates = self.discover(url, ctx)
        if not candidates:
            raise ProbeError(
                "页面中没有发现可下载的媒体资源；可尝试 --render 使用无头浏览器，"
                "或直接给出 m3u8/mp4 地址"
            )
        info = MediaInfo(
            url=url,
            title=_title_from_html(url, self.last_title),
            extractor="uge",
            webpage_url=url,
            is_live=any(c.kind == "hls" for c in candidates),
        )
        info.formats = [c.as_format(i) for i, c in enumerate(candidates)]
        best = candidates[0]
        info.height = best.height
        ctx.item.meta["uge_candidates"] = [c.__dict__ for c in candidates]
        ctx.item.meta["uge_best"] = best.url
        return info

    def discover(self, url: str, ctx: DownloadContext) -> list[Candidate]:
        self.last_title = ""
        opts = self._net_options(ctx, referer=url)
        candidates: list[Candidate] = []

        if self.render or ctx.options.get("render"):
            rendered = self._discover_rendered(url, ctx)
            candidates.extend(rendered)

        try:
            page = get_text(url, opts, encoding="utf-8")
        except NetworkError as exc:
            if not candidates:
                raise ProbeError(f"页面抓取失败: {exc}") from exc
            page = ""
        candidates.extend(self._discover_static(page, url))

        # One level of iframes - players are almost always embedded.
        for frame in self._iframes(page, url)[:4]:
            if frame in (url,):
                continue
            try:
                frame_html = get_text(frame, self._net_options(ctx, referer=url))
            except NetworkError:
                continue
            candidates.extend(self._discover_static(frame_html, frame))

        return self._rank(_dedupe(candidates))

    def _discover_static(self, page: str, base: str) -> list[Candidate]:
        out: list[Candidate] = []
        if not page:
            return out

        match = re.search(r"<title[^>]*>(.*?)</title>", page, re.DOTALL | re.IGNORECASE)
        if match and not self.last_title:
            self.last_title = html.unescape(match.group(1)).strip()

        for tag in META_TAG_RE.finditer(page):
            key, value = tag.group(1).lower(), tag.group(2)
            if key in META_KEYS and value:
                out.append(self._candidate(urljoin(base, html.unescape(value)), "meta", base))

        for source in SOURCE_RE.finditer(page):
            out.append(self._candidate(urljoin(base, html.unescape(source.group(1))), "element", base))

        for block in JSONLD_RE.finditer(page):
            try:
                payload = json.loads(block.group(1).strip())
            except json.JSONDecodeError:
                continue
            for url in _walk_json_for_urls(payload):
                out.append(self._candidate(urljoin(base, url), "json-ld", base))

        # Media URLs are frequently embedded in JS string literals where every
        # slash is escaped ("https:\/\/..."), so scan an unescaped copy too.
        scan = page.replace("\\/", "/")
        for pattern in (MANIFEST_RE, MEDIA_URL_RE):
            for found in pattern.finditer(scan):
                raw = html.unescape(found.group("url"))
                if raw.startswith("//"):
                    raw = urlsplit(base).scheme + ":" + raw
                out.append(self._candidate(urljoin(base, raw), "script", base))

        return out

    def _discover_rendered(self, url: str, ctx: DownloadContext) -> list[Candidate]:
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
        except Exception:  # noqa: BLE001
            raise ProbeError(
                "未安装 Playwright，无法使用渲染模式。"
                "请执行: omarchy-pkg-install python-playwright && playwright install chromium"
            )

        seen: list[str] = []
        proxy = self._net_options(ctx).proxy or None

        def note(request) -> None:
            target = request.url
            if sniff_kind(target) in ("hls", "dash", "file", "audio"):
                seen.append(target)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, proxy={"server": proxy} if proxy else None)
            context = browser.new_context(user_agent=BROWSER_UA)
            page = context.new_page()
            page.on("request", note)
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_timeout(3500)
            except Exception:  # noqa: BLE001
                pass
            if not self.last_title:
                self.last_title = (page.title() or "").strip()
            browser.close()

        return [self._candidate(target, "network", url) for target in seen]

    # ------------------------------------------------------------------ download
    def download(self, ctx: DownloadContext, progress: ProgressCb) -> Path:
        best = ctx.item.meta.get("uge_best")
        if not best:
            raise ProbeError("没有可用的候选链接")
        kind = sniff_kind(best)
        if kind == "hls":
            return HlsBackend().download(ctx, progress)
        if kind in ("file", "audio"):
            return DirectBackend().download(ctx, progress)
        if kind == "dash":
            raise BackendError(
                "检测到 DASH(.mpd) 流，原生引擎暂不支持，请安装 yt-dlp 后重试"
            )
        raise BackendError(f"无法下载 {best}")

    # ------------------------------------------------------------------- helpers
    def _candidate(self, url: str, source: str, base: str) -> Candidate:
        kind = sniff_kind(url)
        height = _height_hint(url)
        label = ""
        if kind == "hls":
            label = "HLS"
        elif kind == "dash":
            label = "DASH"
        score = {"hls": 60, "dash": 50, "file": 30, "audio": 20}.get(kind, 5)
        # Resolution breaks ties inside a container, but never lets a 1080p
        # progressive file outrank a manifest (which can still offer 4K).
        score += min(20, height // 100)
        if _looks_like_thumbnail(url):
            score -= 80
        return Candidate(url=url, kind=kind, label=label, height=height, source=source, score=score)

    def _rank(self, candidates: list[Candidate]) -> list[Candidate]:
        return sorted(candidates, key=lambda c: (-c.score, c.url))

    def _iframes(self, page: str, base: str) -> list[str]:
        out = []
        for match in IFRAME_RE.finditer(page or ""):
            out.append(urljoin(base, html.unescape(match.group(1))))
        return out

    def _net_options(self, ctx: DownloadContext, referer: str = "") -> NetOptions:
        options = ctx.item.options
        network = ctx.config.network if ctx.config else None
        return NetOptions(
            user_agent=BROWSER_UA,
            referer=referer or ctx.item.url,
            cookie=ctx.item.meta.get("cookie", ""),
            proxy=options.proxy or (network.proxy if network else ""),
            timeout=float(network.timeout) if network else 30.0,
        )


# ----------------------------------------------------------------- tiny helpers

def _dedupe(candidates: list[Candidate]) -> list[Candidate]:
    seen: set[str] = set()
    out: list[Candidate] = []
    for candidate in candidates:
        key = candidate.url.split("#")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(candidate)
    return out


def _walk_json_for_urls(node, depth: int = 0) -> list[str]:
    if depth > 6:
        return []
    found: list[str] = []
    if isinstance(node, str):
        if node.startswith(("http://", "https://", "//")) and sniff_kind(node) != "page":
            found.append(node)
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_walk_json_for_urls(value, depth + 1))
    elif isinstance(node, list):
        for value in node:
            found.extend(_walk_json_for_urls(value, depth + 1))
    return found


def _height_hint(url: str) -> int:
    match = re.search(r"(?:^|[^0-9])(2160|1440|1080|720|480|360|240|144)(?:p|[^0-9]|$)", url)
    if match:
        return int(match.group(1))
    for token, height in (("4k", 2160), ("fhd", 1080), ("hd", 720), ("sd", 480)):
        if token in url.lower():
            return height
    return 0


def _looks_like_thumbnail(url: str) -> bool:
    lowered = url.lower()
    return any(token in lowered for token in ("thumb", "poster", "preview", "sprite", "storyboard"))


def _title_from_html(url: str, title: str) -> str:
    if title:
        return title
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1] or urlsplit(url).netloc or "video"
