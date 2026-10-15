"""Clipboard watcher - Downie's "Load Links in Clipboard" / "Pass Cookies to Downie".

On Wayland (Omarchy runs Hyprland) we use ``wl-paste --watch`` when available and
fall back to polling. Every distinct clipboard payload that looks like media is
offered to the queue exactly once.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Iterable
from urllib.parse import urlsplit

from . import notify
from .models import DownloadOptions
from .net import sniff_kind
from .util import dedupe, split_urls

#: Hosts that are almost always media; used to cut clipboard noise.
KNOWN_HOSTS = (
    "youtube.com", "youtu.be", "bilibili.com", "b23.tv", "vimeo.com",
    "twitter.com", "x.com", "facebook.com", "instagram.com", "tiktok.com",
    "douyin.com", "weibo.com", "twitch.tv", "dailymotion.com", "nicovideo.jp",
    "ted.com", "soundcloud.com", "bandcamp.com", "spotify.com", "youku.com",
    "iqiyi.com", "qq.com", "ixigua.com", "kuaishou.com", "xiaohongshu.com",
    "zhihu.com", "reddit.com", "streamable.com", "archive.org", "coursera.org",
    "udemy.com", "pornhub.com", "xvideos.com", "xhamster.com", "xvideos.es",
    "rutube.ru", "odysee.com", "rumble.com", "vk.com", "ok.ru", "naver.com",
    "nicovideo.jp", "pixiv.net", "pinterest.com", "tumblr.com", "linkedin.com",
)

MEDIA_HINT_RE = re.compile(
    r"(\.(m3u8|mpd|mp4|m4v|mkv|webm|mov|flv|ts|mp3|m4a|flac|aac|opus|ogg|wav)"
    r"(\?|$|#)|/(video|watch|v|embed|shorts|playlist|clip|reel)(/|\?|=|$))",
    re.IGNORECASE,
)


class ClipboardWatcher:
    def __init__(
        self,
        on_urls: Callable[[list[str]], None],
        *,
        options: DownloadOptions | None = None,
        poll_interval: float = 0.6,
        notify_enabled: bool = True,
        accept_all: bool = False,
        max_history: int = 200,
    ) -> None:
        self.on_urls = on_urls
        self.options = options or DownloadOptions()
        self.poll_interval = poll_interval
        self.notify_enabled = notify_enabled
        self.accept_all = accept_all
        self._seen: list[str] = []
        self._seen_set: set[str] = set()
        self._max_history = max_history
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if shutil.which("wl-paste"):
            self._start_wl_paste()
        else:
            threading.Thread(target=self._poll_loop, name="omadown-clip", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except OSError:
                pass

    # ---------------------------------------------------------------- backends
    def _start_wl_paste(self) -> None:
        try:
            self._proc = subprocess.Popen(
                ["wl-paste", "--watch", "cat", "--type", "text"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        except OSError:
            threading.Thread(target=self._poll_loop, name="omadown-clip", daemon=True).start()
            return
        threading.Thread(target=self._read_loop, name="omadown-clip-read", daemon=True).start()

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buffer: list[str] = []
        for line in iter(self._proc.stdout.readline, ""):
            buffer.append(line)
            # wl-paste --watch flushes on every clipboard change; process eagerly.
            self._handle("".join(buffer))
            buffer = []

    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.run(
                    ["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=3,
                )
                if proc.returncode == 0:
                    self._handle(proc.stdout)
            except (OSError, subprocess.TimeoutExpired):
                pass
            self._stop.wait(self.poll_interval)

    # ------------------------------------------------------------------ filtering
    def _handle(self, text: str) -> None:
        urls = self.filter(split_urls(text))
        if not urls:
            return
        fingerprint = hashlib.sha1("|".join(urls).encode()).hexdigest()
        if fingerprint in self._seen_set:
            return
        self._remember(fingerprint)
        for url in urls:
            notify.clipboard_caught(url, self.notify_enabled)
        self.on_urls(urls)

    def filter(self, urls: Iterable[str]) -> list[str]:
        out: list[str] = []
        for url in dedupe(urls):
            if self.accept_all or self.looks_downloadable(url):
                out.append(url)
        return out

    @staticmethod
    def looks_downloadable(url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if any(host == known or host.endswith("." + known) for known in KNOWN_HOSTS):
            return True
        if MEDIA_HINT_RE.search(parts.path + ("?" + parts.query if parts.query else "")):
            return True
        return sniff_kind(url) != "page"

    def _remember(self, fingerprint: str) -> None:
        self._seen.append(fingerprint)
        self._seen_set.add(fingerprint)
        if len(self._seen) > self._max_history:
            oldest = self._seen.pop(0)
            self._seen_set.discard(oldest)


def watch_once() -> list[str]:
    """Read the clipboard a single time (``omadown paste``)."""
    for command in (["wl-paste", "--no-newline"], ["xclip", "-o", "-selection", "clipboard"],
                    ["pbpaste"]):
        if not shutil.which(command[0]):
            continue
        try:
            proc = subprocess.run(command, capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                return split_urls(proc.stdout)
        except (OSError, subprocess.TimeoutExpired):
            continue
    return []
