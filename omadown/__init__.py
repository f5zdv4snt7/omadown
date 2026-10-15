"""omadown - a Downie 4 style queue downloader for Omarchy / Linux.

Backends:
    ytdlp   - 1600+ site extractors via the yt-dlp CLI
    hls     - native M3U8 engine with AES-128 decryption and live capture
    uge     - user-guided extraction (page sniffing, optional headless browser)
    direct  - resumable HTTP(S) downloads
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]

from .config import Config  # noqa: E402
from .db import Store  # noqa: E402
from .engine import Engine  # noqa: E402
from .models import DownloadItem, DownloadOptions, MediaInfo, State  # noqa: E402
