"""Small shared helpers: paths, formatting, subprocess plumbing."""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import Iterable, Sequence


# --------------------------------------------------------------------------- paths

def which(name: str) -> str | None:
    """Locate an executable, honouring the user's PATH."""
    return shutil.which(name)


def expand(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(str(path))))


def ensure_dir(path: str | os.PathLike[str]) -> Path:
    p = expand(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def config_dir() -> Path:
    return ensure_dir(os.environ.get("OMADOWN_CONFIG_HOME")
                      or Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")) / "omadown")


def data_dir() -> Path:
    return ensure_dir(os.environ.get("OMADOWN_DATA_HOME")
                      or Path(os.environ.get("XDG_DATA_HOME", "~/.local/share")) / "omadown")


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if base:
        return ensure_dir(Path(base) / "omadown")
    return ensure_dir(data_dir() / "run")


def state_path() -> Path:
    """JSON snapshot of the queue, read by the Waybar module."""
    return runtime_dir() / "state.json"


def control_socket_path() -> Path:
    return runtime_dir() / "control.sock"


# ----------------------------------------------------------------------- formatting

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def human_bytes(value: float | int | None) -> str:
    if value is None:
        return "-"
    n = float(value)
    for unit in _UNITS:
        if abs(n) < 1024.0 or unit == _UNITS[-1]:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} PiB"


def human_speed(value: float | int | None) -> str:
    if not value:
        return "-"
    return human_bytes(value) + "/s"


def human_duration(seconds: float | int | None) -> str:
    if not seconds or seconds < 0:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h:d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def display_width(text: str) -> int:
    """Columns a string occupies in a terminal (CJK glyphs are double width)."""
    import unicodedata
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def fit(text: str, columns: int) -> str:
    """Truncate to at most ``columns`` terminal cells.

    ``str.ljust``-style padding is character based, which overflows as soon as the
    text contains CJK: 99 characters can need 150 columns. Curses then refuses to
    write, so every TUI string goes through here first.
    """
    import unicodedata
    if columns <= 0:
        return ""
    if display_width(text) <= columns:
        return text
    out: list[str] = []
    used = 0
    for char in text:
        cell = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        )
        if used + cell > columns:
            break
        out.append(char)
        used += cell
    return "".join(out)


def pad(text: str, columns: int) -> str:
    """Pad with spaces to exactly ``columns`` cells (clipping first if needed)."""
    clipped = fit(text, columns)
    return clipped + " " * max(0, columns - display_width(clipped))


def shorten_wide(text: str, columns: int) -> str:
    """``shorten`` for terminal output, measured in display cells."""
    if columns <= 1:
        return fit(text, max(0, columns))
    if display_width(text) <= columns:
        return text
    return fit(text, columns - 3) + "..."


def progress_bar(fraction: float | None, width: int = 24, filled: str = "#", empty: str = "-") -> str:
    if fraction is None:
        return empty * width
    fraction = max(0.0, min(1.0, fraction))
    done = int(round(fraction * width))
    return filled * done + empty * (width - done)


def shorten(text: str, width: int) -> str:
    if width <= 1:
        return text[:width]
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


# ---------------------------------------------------------------------- subprocess

def popen(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: dict[str, str] | None = None,
    text: bool = True,
) -> subprocess.Popen:
    """Spawn a child in its own process group so we can kill the whole tree."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    return subprocess.Popen(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=text,
        bufsize=1,
        start_new_session=True,
    )


def terminate(proc: subprocess.Popen, grace: float = 3.0) -> None:
    """Ask a process group to stop, then insist."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            proc.terminate()
        except OSError:
            return
    try:
        proc.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()


def run(
    cmd: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    check: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(cmd),
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=check,
        timeout=timeout,
    )


class LineReader(threading.Thread):
    """Drain a child's stdout on a background thread, yielding lines to a callback."""

    def __init__(self, stream, on_line, name: str = "omadown-reader") -> None:
        super().__init__(daemon=True, name=name)
        self._stream = stream
        self._on_line = on_line
        self.tail: list[str] = []

    def run(self) -> None:
        try:
            for raw in iter(self._stream.readline, ""):
                line = raw.rstrip("\r\n")
                if not line:
                    continue
                self.tail.append(line)
                if len(self.tail) > 40:
                    del self.tail[0]
                try:
                    self._on_line(line)
                except Exception:
                    pass
        except (ValueError, OSError):
            pass


# --------------------------------------------------------------------------- misc

def split_urls(text: str) -> list[str]:
    """Pull http(s) URLs out of arbitrary text (clipboard, CSV, argv)."""
    return re.findall(r"https?://[^\s\"'<>()\[\]]+", text or "")


def dedupe(seq: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out
