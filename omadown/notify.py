"""Desktop notifications.

On Omarchy/Hyprland the runner is ``notify-send`` (libnotify) or, if the user
replaced it, ``makoctl``/``dunstify``. We degrade to a log line so headless runs
stay quiet instead of crashing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Iterable

from .util import popen

APP_NAME = "omadown"

_URGENCY = {"low": "low", "normal": "normal", "critical": "critical"}


def available() -> str | None:
    for candidate in ("notify-send", "dunstify", "makoctl"):
        if shutil.which(candidate):
            return candidate
    return None


def notify(
    summary: str,
    body: str = "",
    *,
    urgency: str = "normal",
    icon: str = "folder-download",
    actions: Iterable[tuple[str, str]] = (),
    enabled: bool = True,
) -> bool:
    if not enabled:
        return False
    runner = available()
    if runner is None:
        print(f"[notify] {summary}{': ' + body if body else ''}", file=sys.stderr)
        return False

    if runner == "makoctl":
        cmd = ["makoctl", "notify", "-t", summary, "-b", body]
    else:
        cmd = [runner, "-a", APP_NAME, "-u", _URGENCY.get(urgency, "normal"), "-i", icon]
        if runner == "dunstify":
            cmd.append("--appname")
            cmd.append(APP_NAME)
        cmd += [summary, body]

    try:
        proc = popen(cmd)
        proc.wait(timeout=5)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def download_finished(title: str, path: str, enabled: bool = True) -> None:
    notify("下载完成", f"{title}\n{path}", icon="folder-download", enabled=enabled)


def download_failed(title: str, error: str, enabled: bool = True) -> None:
    notify("下载失败", f"{title}\n{error}", urgency="critical", icon="dialog-error", enabled=enabled)


def batch_finished(count: int, enabled: bool = True) -> None:
    notify("队列已完成", f"共成功处理 {count} 个下载", enabled=enabled)


def clipboard_caught(url: str, enabled: bool = True) -> None:
    notify("已加入队列", url, urgency="low", icon="edit-copy", enabled=enabled)


def daemon_ready(host: str = "", enabled: bool = True) -> None:
    where = f" ({host})" if host else ""
    notify("omadown 守护进程已启动", f"正在监听剪贴板{where}", urgency="low", enabled=enabled)
