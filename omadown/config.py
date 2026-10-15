"""Configuration: ``~/.config/omadown/config.toml``.

Sections mirror Downie's preference panes (General / Quality / Subtitles /
Network / Postprocessing) so the mapping stays auditable.
"""

from __future__ import annotations

import dataclasses
import os
import tomllib
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import naming
from .util import config_dir, ensure_dir, expand


@dataclass
class GeneralConfig:
    download_dir: str = "~/Videos/omadown"
    max_concurrent: int = 3
    file_name_format: str = naming.DEFAULT_FORMAT
    group_playlists: bool = False
    start_automatically: bool = True
    clipboard_watch: bool = False
    notify: bool = True
    automated: bool = False
    remove_completed: bool = False
    history_limit: int = 500
    max_retries: int = 2
    open_on_finish: bool = False


@dataclass
class QualityConfig:
    max_resolution: int = 0          # 0 = unlimited (Downie's wording, kept verbatim)
    prefer_mp4: bool = True
    force_mp4: bool = False
    audio_only: bool = False
    preferred_audio_format: str = "m4a"
    enforce_mp3: bool = False
    merge_live_streams: bool = True


@dataclass
class SubtitleConfig:
    enabled: bool = False
    languages: list[str] = field(default_factory=lambda: ["zh-Hans", "en"])
    mode: str = "single"             # single | multiple
    include_generated: bool = True
    embed: bool = True
    convert_to: str = "srt"


@dataclass
class NetworkConfig:
    proxy: str = ""
    cookies_from_browser: str = ""
    connections: int = 8
    chunked: bool = True
    rate_limit: str = ""
    retries: int = 10
    fragment_retries: int = 10
    timeout: int = 30
    user_agent: str = ""


@dataclass
class PostprocessConfig:
    enabled: bool = True
    hls_mode: str = "remux"          # remux | reencode | none
    transcode: str = ""              # "" | h264 | h265
    hardware_accel: str = "auto"     # auto | vaapi | nvenc | qsv | none
    keep_original: bool = False
    extract_audio: bool = False
    write_metadata: bool = True
    write_thumbnail: bool = True
    custom_script: str = ""
    save_metadata_json: bool = False


@dataclass
class TuiConfig:
    theme: str = "omarchy"
    refresh_ms: int = 400
    confirm_quit: bool = False


@dataclass
class Config:
    general: GeneralConfig = field(default_factory=GeneralConfig)
    quality: QualityConfig = field(default_factory=QualityConfig)
    subtitles: SubtitleConfig = field(default_factory=SubtitleConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    postprocessing: PostprocessConfig = field(default_factory=PostprocessConfig)
    tui: TuiConfig = field(default_factory=TuiConfig)

    # ------------------------------------------------------------------ loading
    @classmethod
    def path(cls) -> Path:
        override = os.environ.get("OMADOWN_CONFIG")
        return expand(override) if override else config_dir() / "config.toml"

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        target = expand(path) if path else cls.path()
        cfg = cls()
        if not target.exists():
            return cfg
        with open(target, "rb") as fh:
            data = tomllib.load(fh)
        known = {f.name for f in fields(cls)}
        for section, values in data.items():
            if section not in known:
                continue
            current = getattr(cfg, section)
            valid = {f.name for f in fields(current)}
            for key, value in (values or {}).items():
                if key in valid:
                    setattr(current, key, value)
        cfg.validate()
        return cfg

    # ------------------------------------------------------------------ writing
    def to_toml(self) -> str:
        lines: list[str] = ["# omadown configuration - see `omadown --help` and README.md"]
        for f in fields(self):
            section = getattr(self, f.name)
            lines.append("")
            lines.append(f"[{f.name}]")
            for sf in fields(section):
                lines.append(f"{sf.name} = {_toml_value(getattr(section, sf.name))}")
        return "\n".join(lines) + "\n"

    def save(self, path: Path | str | None = None) -> Path:
        target = expand(path) if path else self.path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_text(self.to_toml(), encoding="utf-8")
        tmp.replace(target)
        return target

    def as_dict(self) -> dict[str, Any]:
        return {f.name: asdict(getattr(self, f.name)) for f in fields(self)}

    # ---------------------------------------------------------------- behaviour
    def validate(self) -> None:
        naming.validate_format(self.general.file_name_format)
        g = self.general
        g.max_concurrent = max(1, min(32, int(g.max_concurrent)))
        g.max_retries = max(0, min(20, int(g.max_retries)))
        self.network.connections = max(1, min(32, int(self.network.connections)))
        if self.subtitles.mode not in ("single", "multiple"):
            self.subtitles.mode = "single"
        if self.postprocessing.hls_mode not in ("remux", "reencode", "none"):
            self.postprocessing.hls_mode = "remux"
        if self.postprocessing.transcode not in ("", "h264", "h265"):
            self.postprocessing.transcode = ""
        if self.quality.audio_only and self.quality.enforce_mp3:
            self.quality.preferred_audio_format = "mp3"
        self.general.download_dir = str(
            expand(self.general.download_dir)
        )

    @property
    def output_dir(self) -> Path:
        return ensure_dir(self.general.download_dir)

    def get(self, dotted: str, default: Any = None) -> Any:
        """``cfg.get("quality.max_resolution")`` - handy for CLI overrides."""
        section, _, key = dotted.partition(".")
        if not key:
            return getattr(self, section, default)
        target = getattr(self, section, None)
        return getattr(target, key, default) if target else default

    def set(self, dotted: str, value: Any) -> None:
        section, _, key = dotted.partition(".")
        target = getattr(self, section, None)
        if target is None or not hasattr(target, key):
            raise KeyError(f"unknown setting: {dotted}")
        current = getattr(target, key)
        if isinstance(current, bool) and isinstance(value, str):
            value = value.strip().lower() in ("1", "true", "yes", "on")
        elif isinstance(current, int) and not isinstance(current, bool) and isinstance(value, str):
            value = int(value)
        setattr(target, key, value)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'
