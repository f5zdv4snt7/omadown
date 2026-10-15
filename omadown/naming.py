"""File-name schemes.

Downie 4 offers "custom name schemes" and refuses a scheme that lacks ``title``
(``Localizable.strings``: "The file name format must contain title."). We keep
the same rule and expose the same token idea to both backends: our own sanitizer
for the native downloaders and a translation table for yt-dlp's ``-o`` template.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Mapping

DEFAULT_FORMAT = "{title}.{ext}"

# token -> yt-dlp output-template field
YTDLP_FIELDS: dict[str, str] = {
    "title": "%(title)s",
    "id": "%(id)s",
    "ext": "%(ext)s",
    "index": "%(playlist_index)s",
    "playlist": "%(playlist_title)s",
    "uploader": "%(uploader)s",
    "date": "%(upload_date)s",
    "resolution": "%(resolution)s",
    "quality": "%(format_note)s",
    "extractor": "%(extractor)s",
    "duration": "%(duration_string)s",
}

TOKENS = tuple(YTDLP_FIELDS)
TOKEN_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# POSIX keeps everything except '/' and NUL, but a media library shared with
# Windows/SMB mounts (common on Omarchy setups) breaks on more than that.
_ILLEGAL = r'<>:"/\\|?*'
_ILLEGAL_RE = re.compile(f"[{re.escape(_ILLEGAL)}]")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DOTS_RE = re.compile(r"^\.+$")


class NameSchemeError(ValueError):
    pass


def validate_format(fmt: str) -> None:
    """Raise ``NameSchemeError`` when a scheme is unusable."""
    if not fmt or not fmt.strip():
        raise NameSchemeError("file name format is empty")
    tokens = TOKEN_RE.findall(fmt)
    unknown = sorted({t for t in tokens if t not in YTDLP_FIELDS})
    if unknown:
        raise NameSchemeError(
            f"unknown token(s): {', '.join('{' + t + '}' for t in unknown)}; "
            f"available: {', '.join('{' + t + '}' for t in TOKENS)}"
        )
    if "title" not in tokens:
        raise NameSchemeError("the file name format must contain {title}")


def sanitize_component(name: str, max_bytes: int = 180) -> str:
    """Make one path component safe on every filesystem we care about."""
    name = unicodedata.normalize("NFC", name or "")
    name = _CONTROL_RE.sub("", name)
    name = _ILLEGAL_RE.sub("_", name)
    name = name.replace("\u200b", "").strip()
    # No hidden files, and no way to spell "..".
    name = re.sub(r"^\.+", "", name)
    # Windows dislikes trailing dots/spaces; so do some network shares.
    name = name.rstrip(" .")
    if not name:
        name = "untitled"
    encoded = name.encode("utf-8")
    if len(encoded) > max_bytes:
        name = encoded[:max_bytes].decode("utf-8", "ignore").rstrip(" .")
    return name or "untitled"


def render(fmt: str, meta: Mapping[str, Any], *, ext: str | None = None) -> str:
    """Fill a scheme with metadata.

    Separators written *in the scheme* stay separators; separators that arrive
    inside a metadata value (``/`` in a title, for example) are neutralised, so a
    video called ``AC/DC live`` cannot create a directory.
    """
    validate_format(fmt)
    values: dict[str, str] = {}
    for token in TOKENS:
        value = meta.get(token)
        if token == "ext" and ext:
            value = ext
        if value is None or value == "":
            # An absent playlist/index simply disappears from the name.
            values[token] = "" if token in _DROPPABLE else "unknown"
        else:
            values[token] = str(value)

    chunks = TOKEN_RE.split(fmt)
    pieces: list[str] = []
    for index, chunk in enumerate(chunks):
        if index % 2 == 0:
            pieces.append(_sanitize_literal(chunk))
        else:
            raw = values.get(chunk, "unknown")
            pieces.append(sanitize_component(raw) if raw else "")

    out = "".join(pieces).replace("\\", "/")
    out = re.sub(r"/{2,}", "/", out)

    final_parts: list[str] = []
    for part in out.split("/"):
        part = re.sub(r"^\.+", "", part).rstrip(" .")
        if part:
            final_parts.append(part)
    return "/".join(final_parts) or "untitled"


_DROPPABLE = frozenset({"playlist", "index"})


def _sanitize_literal(chunk: str) -> str:
    """Clean the fixed text of a scheme while keeping ``/`` separators and spacing.

    Unlike :func:`sanitize_component` this must not touch spaces or dots - a
    scheme such as ``{uploader} - {title}.{ext}`` relies on them.
    """
    if not chunk:
        return ""
    cleaned = _CONTROL_RE.sub("", unicodedata.normalize("NFC", chunk))
    return "/".join(_ILLEGAL_RE.sub("_", part) for part in cleaned.split("/"))


def to_ytdlp_template(fmt: str, *, into_subdir: bool = False) -> str:
    """Translate our scheme to a yt-dlp ``-o`` template.

    yt-dlp does its own sanitizing, so we only swap tokens. Path separators in
    the scheme survive, which is how users group a channel into a folder.
    """
    validate_format(fmt)
    template = TOKEN_RE.sub(lambda m: YTDLP_FIELDS[m.group(1)], fmt)
    if into_subdir and not template.startswith(("~", "/")):
        template = template
    return template


def unique_path(path: Path) -> Path:
    """``movie.mp4`` -> ``movie (2).mp4`` when the name is already taken."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for n in range(2, 1000):
        candidate = parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"cannot find a free name for {path}")


def sidecar_path(media: Path, kind: str, lang: str = "", ext: str = "") -> Path:
    """Subtitle / metadata sidecars derived from the media path."""
    suffix = f".{lang}" if lang else ""
    extension = f".{ext}" if ext else ""
    return media.with_name(f"{media.stem}{suffix}.{kind}{extension}")
