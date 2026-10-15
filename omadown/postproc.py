"""Post-processing pipeline: remux, transcode, extract audio, subtitles, scripts.

Downie's help text: "Not all downloads are made equal. Some download in formats
that you don't want... Downie has powerful means to postprocess the download".
Plus a custom-script hook ("Edit Custom Script...") and an option to keep the
original file when conversion fails.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .models import DownloadItem
from .util import ensure_dir, popen, run, shorten, which

AUDIO_CODECS = {
    "mp3": ["-c:a", "libmp3lame", "-q:a", "0"],
    "m4a": ["-c:a", "aac", "-b:a", "256k"],
    "aac": ["-c:a", "copy"],
    "flac": ["-c:a", "flac", "-compression_level", "8"],
    "opus": ["-c:a", "libopus", "-b:a", "160k"],
    "ogg": ["-c:a", "libvorbis", "-q:a", "6"],
    "wav": ["-c:a", "pcm_s16le"],
}

ENCODERS = {
    "h264": {"vaapi": "h264_vaapi", "nvenc": "h264_nvenc", "qsv": "h264_qsv", "sw": "libx264"},
    "h265": {"vaapi": "hevc_vaapi", "nvenc": "hevc_nvenc", "qsv": "hevc_qsv", "sw": "libx265"},
}

VAAPI_DEVICE = "/dev/dri/renderD128"


class PostProcessError(RuntimeError):
    pass


@dataclass
class PostProcessResult:
    path: Path
    steps: list[str]
    warnings: list[str]


def find_ffmpeg() -> str | None:
    override = os.environ.get("OMADOWN_FFMPEG")
    if override and Path(override).exists():
        return override
    return which("ffmpeg")


def find_ffprobe() -> str | None:
    return which("ffprobe")


def detect_hardware_accel(ffmpeg: str | None) -> str:
    """Return 'vaapi' | 'nvenc' | 'qsv' | 'none' - the best encoder we can actually use."""
    if not ffmpeg:
        return "none"
    if Path(VAAPI_DEVICE).exists():
        try:
            proc = run([ffmpeg, "-hide_banner", "-encoders"])
            if "h264_vaapi" in proc.stdout:
                return "vaapi"
        except OSError:
            pass
    try:
        proc = run([ffmpeg, "-hide_banner", "-encoders"])
    except OSError:
        return "none"
    listing = proc.stdout or ""
    if "h264_nvenc" in listing:
        return "nvenc"
    if "h264_qsv" in listing:
        return "qsv"
    return "none"


def probe_duration(ffprobe: str | None, path: Path) -> float | None:
    if not ffprobe:
        return None
    proc = run([
        ffprobe, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ])
    try:
        return float(proc.stdout.strip())
    except ValueError:
        return None


class PostProcessor:
    """Runs the configured steps in place, returning the resulting path."""

    def __init__(
        self,
        config,
        *,
        on_step: Callable[[str], None] | None = None,
        cancel=None,
    ) -> None:
        self.config = config
        self.ffmpeg = find_ffmpeg()
        self.ffprobe = find_ffprobe()
        self.hwaccel = detect_hardware_accel(self.ffmpeg)
        self.on_step = on_step or (lambda _msg: None)
        self.cancel = cancel
        self.warnings: list[str] = []
        self.last_output: str = ""

    # ------------------------------------------------------------------ pipeline
    def run_for(self, item: DownloadItem, path: Path, workdir: Path) -> PostProcessResult:
        cfg = self.config.postprocessing
        steps: list[str] = []

        if not cfg.enabled:
            return PostProcessResult(path, steps, self.warnings)

        is_audio_only = item.options.audio_only or (
            self.config.quality.audio_only if self.config else False
        )
        target_ext = None
        if cfg.save_metadata_json:
            self._save_metadata_json(item, workdir, path)
            steps.append("metadata-json")

        if is_audio_only:
            audio_format = item.options.audio_format or self.config.quality.preferred_audio_format
            if self.config.quality.enforce_mp3:
                audio_format = "mp3"
            path = self.extract_audio(path, audio_format, workdir)
            steps.append(f"extract-audio:{audio_format}")
        else:
            source_ext = path.suffix.lower()
            if source_ext in (".ts", ".m4s") or cfg.hls_mode != "none":
                if cfg.transcode:
                    path = self.transcode(path, cfg.transcode, workdir)
                    steps.append(f"transcode:{cfg.transcode}")
                elif source_ext in (".ts", ".m4s", ".mkv", ".flv", ".webm"):
                    path = self.remux(path, "mp4", workdir)
                    steps.append("remux:mp4")

            if item.options.write_metadata is not False and cfg.write_metadata:
                if self.embed_metadata(path, item, workdir):
                    steps.append("metadata")

            subtitles = self._subtitle_files(path, workdir)
            if subtitles and item.options.embed_subs:
                if self.embed_subtitles(path, subtitles, workdir):
                    steps.append("subtitles")

        if cfg.custom_script:
            self.run_custom_script(cfg.custom_script, path, item)
            steps.append("custom-script")

        return PostProcessResult(path, steps, self.warnings)

    # --------------------------------------------------------------------- steps
    def remux(self, source: Path, container: str, workdir: Path) -> Path:
        self._require_ffmpeg()
        staged = workdir / f"{source.stem}.remux.{container}"
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-c", "copy"]
        if container == "mp4":
            args += ["-movflags", "+faststart", "-bsf:a", "aac_adtstoasc"]
        args.append(str(staged))
        if self._exec(args) and staged.exists() and staged.stat().st_size > 0:
            return self._promote(source, staged, f".{container}")
        staged.unlink(missing_ok=True)
        self.warnings.append(f"remux 失败，保留原始文件 {source.name}")
        return source

    def transcode(self, source: Path, codec: str, workdir: Path) -> Path:
        self._require_ffmpeg()
        codec = codec if codec in ENCODERS else "h264"
        mode = self.config.postprocessing.hardware_accel
        if mode == "auto":
            mode = self.hwaccel
        encoder = ENCODERS[codec].get(mode if mode != "auto" else "sw", ENCODERS[codec]["sw"])
        if not self._encoder_available(encoder):
            encoder = ENCODERS[codec]["sw"]
        staged = workdir / f"{source.stem}.{codec}.mp4"
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if encoder.endswith("_vaapi"):
            args += ["-hwaccel", "vaapi", "-hwaccel_device", VAAPI_DEVICE,
                     "-hwaccel_output_format", "vaapi"]
        args += ["-i", str(source), "-c:v", encoder]
        args += _rate_args(encoder, codec)
        args += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(staged)]
        if self._exec(args) and staged.exists() and staged.stat().st_size > 0:
            return self._promote(source, staged, ".mp4")
        staged.unlink(missing_ok=True)
        self.warnings.append("转码失败，保留原文件")
        return source

    def extract_audio(self, source: Path, audio_format: str, workdir: Path) -> Path:
        self._require_ffmpeg()
        audio_format = audio_format if audio_format in AUDIO_CODECS else "m4a"
        staged = workdir / f"{source.stem}.audio.{audio_format}"
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vn", *AUDIO_CODECS[audio_format], str(staged)]
        if self._exec(args) and staged.exists() and staged.stat().st_size > 0:
            return self._promote(source, staged, f".{audio_format}")
        staged.unlink(missing_ok=True)
        raise PostProcessError("提取音频失败")

    def embed_metadata(self, path: Path, item: DownloadItem, workdir: Path) -> bool:
        self._require_ffmpeg()
        staged = workdir / f"{path.stem}.meta{path.suffix}"
        title = item.title or item.meta.get("title") or ""
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(path), "-c", "copy",
                "-metadata", f"title={title}",
                "-metadata", f"comment={item.url}",
                "-metadata", "encoded_by=omadown"]
        if item.meta.get("uploader"):
            args += ["-metadata", f"artist={item.meta['uploader']}"]
        args.append(str(staged))
        if self._exec(args) and staged.exists() and staged.stat().st_size > 0:
            self._promote(path, staged, path.suffix)
            return True
        staged.unlink(missing_ok=True)
        self.warnings.append("写入元数据失败")
        return False

    def embed_subtitles(self, path: Path, subtitles: list[Path], workdir: Path) -> bool:
        self._require_ffmpeg()
        staged = workdir / f"{path.stem}.subs{path.suffix}"
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(path)]
        for subtitle in subtitles:
            args += ["-i", str(subtitle)]
        args += ["-map", "0"]
        for index in range(len(subtitles)):
            args += ["-map", f"{index + 1}:0"]
        args += ["-c:v", "copy", "-c:a", "copy"]
        if staged.suffix.lower() in (".mp4", ".mov", ".m4v"):
            args += ["-c:s", "mov_text"]
        args.append(str(staged))
        if self._exec(args) and staged.exists() and staged.stat().st_size > 0:
            self._promote(path, staged, path.suffix)
            if not self.config.postprocessing.keep_original:
                for subtitle in subtitles:
                    subtitle.unlink(missing_ok=True)
            return True
        staged.unlink(missing_ok=True)
        self.warnings.append("字幕封装失败，字幕已作为独立文件保留")
        return False

    def run_custom_script(self, script: str, path: Path, item: DownloadItem) -> None:
        env = {
            "OMADOWN_FILE": str(path),
            "OMADOWN_TITLE": item.title,
            "OMADOWN_URL": item.url,
            "OMADOWN_EXTRACTOR": item.extractor,
            "OMADOWN_PLAYLIST": item.playlist_title,
            "OMADOWN_ID": str(item.id or ""),
        }
        proc = popen(["/bin/sh", "-c", script], env=env)
        code = proc.wait()
        if code != 0:
            self.warnings.append(f"自定义脚本退出码 {code}")

    # ------------------------------------------------------------------- helpers
    def _save_metadata_json(self, item: DownloadItem, workdir: Path, path: Path) -> None:
        payload = {
            "id": item.id, "url": item.url, "title": item.title,
            "extractor": item.extractor, "playlist": item.playlist_title,
            "playlist_index": item.playlist_index, "duration": item.duration,
            "output": str(path.name), "options": item.options.to_dict(),
            **{k: v for k, v in item.meta.items() if isinstance(v, (str, int, float, bool))},
        }
        target = path.with_suffix(path.suffix + ".info.json")
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _subtitle_files(self, media: Path, workdir: Path) -> list[Path]:
        return sorted(
            p for p in list(workdir.glob(f"{media.stem}*.srt"))
            + list(workdir.glob(f"{media.stem}*.vtt"))
            + list(workdir.glob(f"{media.stem}*.ass"))
            if p != media and p.suffix.lower() != media.suffix.lower()
        )

    def _encoder_available(self, name: str) -> bool:
        if not self.ffmpeg:
            return False
        proc = run([self.ffmpeg, "-hide_banner", "-encoders"])
        return name in (proc.stdout or "")

    def _require_ffmpeg(self) -> None:
        if not self.ffmpeg:
            raise PostProcessError(
                "未找到 ffmpeg，请先安装：omarchy-pkg-install ffmpeg"
            )

    def _exec(self, args: list[str]) -> bool:
        if self.cancel is not None:
            self.cancel.raise_if_cancelled()
        proc = popen(args)
        if self.cancel is not None:
            self.cancel.bind(proc)
        code = proc.wait()
        self.last_output = ""
        if proc.stdout is not None:
            try:
                self.last_output = (proc.stdout.read() or "").strip()
            except (ValueError, OSError):
                pass
            try:
                proc.stdout.close()
            except OSError:
                pass
        if self.cancel is not None:
            self.cancel.release(proc)
        if code != 0 and self.last_output:
            self.warnings.append(self.last_output.splitlines()[-1][:200])
        return code == 0

    @staticmethod
    def _promote(source: Path, staged: Path, new_suffix: str) -> Path:
        """Replace ``source`` with the freshly produced file, returning its real path.

        The caller keeps using the returned path, so it must never be a name we
        merely *hope* exists.
        """
        final = source.with_suffix(new_suffix)
        if final.exists() and final != source:
            final.unlink()
        if source.exists() and source != final:
            source.unlink()
        shutil.move(str(staged), str(final))
        return final


def _rate_args(encoder: str, codec: str) -> list[str]:
    if encoder.endswith("_vaapi"):
        return ["-qp", "22"]
    if encoder.endswith("_nvenc"):
        return ["-preset", "p5", "-cq", "23"]
    if encoder.endswith("_qsv"):
        return ["-global_quality", "23"]
    return ["-crf", "24" if codec == "h265" else "20", "-preset", "medium"]
