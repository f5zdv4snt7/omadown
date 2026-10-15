"""yt-dlp backend - the site-extractor workhorse, i.e. Downie's 1600+ downloaders.

We drive the ``yt-dlp`` executable rather than importing it: on Arch the package
is updated far more often than any pinned Python dependency, and the CLI contract
(``--progress-template``, ``--print after_move``) is stable enough to parse.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from .. import naming
from ..models import DownloadItem, MediaFormat, MediaInfo, QUALITY_HEIGHTS
from ..util import LineReader, human_bytes, popen, which
from .base import Backend, BackendError, DownloadContext, ProbeError, ProgressCb

PROGRESS_PREFIX = "OMADOWN\t"
OUTPUT_PREFIX = "OMADOWN-OUT\t"
POSTPROCESS_MARKERS = (
    "[Merger]", "[ExtractAudio]", "[VideoConvertor]", "[VideoRemuxer]",
    "[EmbedSubtitle]", "[EmbedThumbnail]", "[Metadata]", "[FixupM3u8]",
    "[FixupM4a]", "[SponsorBlock]", "[MoveFiles]",
)

PROGRESS_TEMPLATE = (
    f"download:{PROGRESS_PREFIX}%(progress.status)s"
    "\t%(progress.downloaded_bytes)s"
    "\t%(progress.total_bytes)s"
    "\t%(progress.total_bytes_estimate)s"
    "\t%(progress.speed)s"
    "\t%(progress.eta)s"
    "\t%(progress.fragment_index)s"
    "\t%(progress.fragment_count)s"
)


def find_ytdlp() -> str | None:
    override = os.environ.get("OMADOWN_YTDLP")
    if override:
        return override
    return which("yt-dlp") or which("youtube-dl")


class YtDlpBackend(Backend):
    name = "ytdlp"
    priority = 10

    def __init__(self) -> None:
        super().__init__()
        self.binary = find_ytdlp()

    @classmethod
    def available(cls) -> bool:
        return find_ytdlp() is not None

    def claim(self, url: str) -> bool:
        return url.startswith(("http://", "https://")) and self.binary is not None

    # --------------------------------------------------------------- arg building
    def _base_args(self, ctx: DownloadContext) -> list[str]:
        options = ctx.item.options
        network = ctx.config.network if ctx.config else None
        args = [
            str(self.binary), "--ignore-config", "--no-warnings", "--no-color",
            "--newline", "--no-simulate",
            "--progress-template", PROGRESS_TEMPLATE,
            "--print", f"after_move:{OUTPUT_PREFIX}%(filepath)s",
            # --print implies --quiet, which silently suppresses the
            # --progress-template output above -> every download appeared stuck
            # at 0% with no error. --progress restores progress in quiet mode.
            "--progress",
            "--retries", str(network.retries if network else 10),
            "--fragment-retries", str(network.fragment_retries if network else 10),
            "--socket-timeout", str(network.timeout if network else 30),
        ]
        proxy = options.proxy or (network.proxy if network else "")
        if proxy:
            args += ["--proxy", proxy]
        cookies_browser = options.cookies_from_browser or (
            network.cookies_from_browser if network else ""
        )
        if cookies_browser:
            args += ["--cookies-from-browser", cookies_browser]
        if options.cookies_file:
            args += ["--cookies", options.cookies_file]
        connections = options.connections or (network.connections if network else 0)
        if connections:
            args += ["--concurrent-fragments", str(connections)]
        if network and not network.chunked:
            args += ["--no-chunked-download"]
        rate = options.rate_limit or (network.rate_limit if network else "")
        if rate:
            args += ["--limit-rate", rate]
        if network and network.user_agent:
            args += ["--user-agent", network.user_agent]
        if ctx.ffmpeg:
            args += ["--ffmpeg-location", str(Path(ctx.ffmpeg).parent)]
        return args

    def _format_args(self, ctx: DownloadContext) -> list[str]:
        options = ctx.item.options
        quality = ctx.config.quality if ctx.config else None
        audio_only = options.audio_only or (quality.audio_only if quality else False)
        args: list[str] = []

        if audio_only:
            audio_format = options.audio_format or (
                quality.preferred_audio_format if quality else "m4a"
            )
            if quality and quality.enforce_mp3:
                audio_format = "mp3"
            args += ["-f", "ba/b", "-x", "--audio-format", audio_format,
                     "--audio-quality", "0"]
            return args

        prefer_mp4 = options.prefer_mp4
        if prefer_mp4 is None and quality:
            prefer_mp4 = quality.prefer_mp4
        force_mp4 = options.force_mp4
        if force_mp4 is None and quality:
            force_mp4 = quality.force_mp4

        if force_mp4:
            args += ["-f", "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/bv*+ba/b"]
        else:
            args += ["-f", "bv*+ba/b"]

        cap = options.max_resolution
        if not cap and quality:
            cap = quality.max_resolution
        target = QUALITY_HEIGHTS.get(options.quality or "best", 0)

        sort_fields: list[str] = []
        if cap:
            sort_fields.append(f"res:{cap}")
        elif target:
            sort_fields.append(f"res:{target}")
        if prefer_mp4:
            sort_fields.append("ext:mp4:m4a")
        if sort_fields:
            args += ["-S", ",".join(sort_fields)]
        if prefer_mp4 or force_mp4:
            args += ["--merge-output-format", "mp4"]
        return args

    def _subtitle_args(self, ctx: DownloadContext) -> tuple[list[str], bool]:
        options = ctx.item.options
        subs_cfg = ctx.config.subtitles if ctx.config else None
        enabled = options.subtitles
        if enabled is None:
            enabled = subs_cfg.enabled if subs_cfg else False
        if not enabled and not options.subs_only:
            return ["--no-write-subs"], False

        langs = options.subtitle_langs or (subs_cfg.languages if subs_cfg else ["en"])
        mode = options.subtitle_mode or (subs_cfg.mode if subs_cfg else "single")
        include_generated = options.include_generated_subs
        if include_generated is None:
            include_generated = subs_cfg.include_generated if subs_cfg else True
        embed = options.embed_subs
        if embed is None:
            embed = subs_cfg.embed if subs_cfg else True

        args = ["--write-subs", "--sub-langs", ",".join(langs)]
        if include_generated:
            args.append("--write-auto-subs")
        if mode == "single":
            args.append("--sub-langs")
            args.append(",".join(langs) + ":1")
        if subs_cfg and subs_cfg.convert_to:
            args += ["--convert-subs", subs_cfg.convert_to]
        if embed and not options.subs_only:
            args.append("--embed-subs")
        return args, True

    def _output_args(self, ctx: DownloadContext) -> list[str]:
        options = ctx.item.options
        template = options.name_format or (
            ctx.config.general.file_name_format if ctx.config else naming.DEFAULT_FORMAT
        )
        playlists = ctx.config.general.group_playlists if ctx.config else False
        if playlists and ctx.item.playlist_title:
            template = "{playlist}/" + template
        ytdlp_template = naming.to_ytdlp_template(template)
        args = ["-o", ytdlp_template, "--paths", str(ctx.workdir)]
        if ctx.item.options.write_metadata is not False:
            args += ["--embed-metadata"]
        if ctx.item.options.write_thumbnail is not False:
            args += ["--embed-thumbnail"]
        return args

    def _playlist_args(self, ctx: DownloadContext) -> list[str]:
        options = ctx.item.options
        if options.playlist is True:
            args = ["--yes-playlist"]
        elif options.playlist is False:
            args = ["--no-playlist"]
        else:
            args = ["--no-playlist"]
        if options.playlist_items:
            args += ["--playlist-items", options.playlist_items]
        return args

    def build_command(self, ctx: DownloadContext) -> list[str]:
        args = self._base_args(ctx)
        args += self._playlist_args(ctx)
        args += self._format_args(ctx)
        subtitle_args, _ = self._subtitle_args(ctx)
        args += subtitle_args
        args += self._output_args(ctx)
        if ctx.item.options.subs_only:
            args.append("--skip-download")
        args += ["--", ctx.item.url]
        return args

    # ------------------------------------------------------------------- probing
    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        args = [
            str(self.binary), "--ignore-config", "-J", "--flat-playlist",
            "--no-warnings", "--no-progress", "--playlist-end", "50",
        ]
        network = ctx.config.network if ctx.config else None
        if network and network.proxy:
            args += ["--proxy", network.proxy]
        if network and network.cookies_from_browser:
            args += ["--cookies-from-browser", network.cookies_from_browser]
        if network and network.user_agent:
            args += ["--user-agent", network.user_agent]
        args += ["--", url]

        try:
            proc = popen(args)
            stdout, _ = proc.communicate(timeout=120)
        except Exception as exc:  # noqa: BLE001
            raise ProbeError(f"yt-dlp 探测失败: {exc}") from exc
        if proc.returncode != 0 or not (stdout or "").strip():
            raise ProbeError(f"yt-dlp 无法解析该链接 (exit {proc.returncode})")

        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise ProbeError(f"yt-dlp 返回内容无法解析: {exc}") from exc

        if payload.get("_type") == "playlist" or payload.get("entries"):
            entries = [e for e in (payload.get("entries") or []) if e]
            info = MediaInfo(
                url=url,
                title=payload.get("title") or "playlist",
                id=str(payload.get("id") or ""),
                uploader=payload.get("uploader") or payload.get("channel") or "",
                extractor=payload.get("extractor") or payload.get("ie_key") or "",
                webpage_url=payload.get("webpage_url") or url,
                is_playlist=True,
                playlist_title=payload.get("title") or "",
                playlist_count=len(entries) or payload.get("playlist_count"),
                entries=[self._entry_to_info(e) for e in entries],
                raw=payload,
            )
            return info
        return self._entry_to_info(payload)

    def _entry_to_info(self, data: dict) -> MediaInfo:
        formats = []
        for item in data.get("formats") or []:
            formats.append(MediaFormat(
                format_id=str(item.get("format_id") or ""),
                ext=item.get("ext") or "",
                resolution=item.get("resolution") or "",
                height=int(item.get("height") or 0),
                width=int(item.get("width") or 0),
                fps=float(item.get("fps") or 0.0),
                vcodec=item.get("vcodec") or "none",
                acodec=item.get("acodec") or "none",
                filesize=item.get("filesize") or item.get("filesize_approx"),
                tbr=item.get("tbr"),
                abr=item.get("abr"),
                protocol=item.get("protocol") or "",
                url=item.get("url") or "",
                note=item.get("format_note") or "",
                language=item.get("language") or "",
            ))
        return MediaInfo(
            url=data.get("webpage_url") or data.get("url") or "",
            title=data.get("title") or "",
            id=str(data.get("id") or ""),
            uploader=data.get("uploader") or data.get("channel") or "",
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail") or "",
            extractor=data.get("extractor") or data.get("extractor_key") or "",
            webpage_url=data.get("webpage_url") or "",
            description=data.get("description") or "",
            playlist_title=data.get("playlist_title") or "",
            playlist_index=data.get("playlist_index"),
            playlist_count=data.get("playlist_count"),
            is_live=bool(data.get("is_live")),
            formats=formats,
            width=int(data.get("width") or 0),
            height=int(data.get("height") or 0),
            raw=data,
        )

    # ------------------------------------------------------------------ download
    def download(self, ctx: DownloadContext, progress: ProgressCb) -> Path:
        command = self.build_command(ctx)
        token = ctx.options.get("cancel")
        started = time.monotonic()
        final_path: Path | None = None
        errors: list[str] = []
        last_emit = 0.0

        proc = popen(command, cwd=ctx.workdir)
        if token is not None:
            token.bind(proc)

        def handle(line: str) -> None:
            nonlocal final_path, last_emit
            if line.startswith(PROGRESS_PREFIX):
                fields = line[len(PROGRESS_PREFIX):].split("\t")
                if len(fields) < 7:
                    return
                status = fields[0]
                downloaded = _num(fields[1]) or 0
                total = _num(fields[2]) or _num(fields[3])
                speed = _num(fields[4])
                eta = _num(fields[5])
                frag_index = _num(fields[6])
                frag_count = _num(fields[7]) if len(fields) > 7 else None
                now = time.monotonic()
                if now - last_emit < 0.15 and status != "finished":
                    return
                last_emit = now
                total = int(total) if total else None
                message = ""
                if frag_index and frag_count:
                    message = f"{int(frag_index)}/{int(frag_count)} 分片"
                self.emit(
                    progress,
                    stage="downloading",
                    downloaded=int(downloaded),
                    total=total,
                    speed=speed,
                    eta=eta,
                    started=started,
                    message=message,
                )
            elif line.startswith(OUTPUT_PREFIX):
                final_path = Path(line[len(OUTPUT_PREFIX):].strip())
            elif any(marker in line for marker in POSTPROCESS_MARKERS):
                progress(_pp_progress(line))
            elif line.startswith("ERROR:"):
                errors.append(line.strip())

        reader = LineReader(proc.stdout, handle)
        reader.start()
        try:
            proc.wait()
        except KeyboardInterrupt:
            from ..util import terminate
            terminate(proc)
            raise
        reader.join(timeout=5)
        if token is not None:
            token.release(proc)
        if token is not None and token.cancelled:
            from .base import Cancelled
            raise Cancelled("cancelled by user")

        if proc.returncode != 0:
            detail = errors[-1] if errors else (reader.tail[-1] if reader.tail else "")
            raise BackendError(detail or f"yt-dlp 退出码 {proc.returncode}")

        if final_path is None:
            final_path = self._guess_output(ctx)
        if final_path is None or not final_path.exists():
            found = self._scan_outputs(ctx)
            if not found:
                raise BackendError("下载结束但未找到输出文件")
            final_path = max(found, key=lambda p: p.stat().st_size)
        return final_path

    def _guess_output(self, ctx: DownloadContext) -> Path | None:
        template = ctx.item.options.name_format or (
            ctx.config.general.file_name_format if ctx.config else naming.DEFAULT_FORMAT
        )
        meta = dict(ctx.item.meta)
        meta.setdefault("title", ctx.item.title)
        meta.setdefault("id", "")
        meta.setdefault("extractor", ctx.item.extractor)
        meta.setdefault("playlist", ctx.item.playlist_title)
        meta.setdefault("playlist_index", ctx.item.playlist_index)
        for ext in (ctx.item.meta.get("ext") or "mp4", "mkv", "webm", "mp3", "m4a"):
            try:
                relative = naming.render(template, {**meta, "ext": ext})
            except naming.NameSchemeError:
                return None
            candidate = ctx.workdir / relative
            if candidate.exists():
                return candidate
        return None

    @staticmethod
    def _scan_outputs(ctx: DownloadContext) -> list[Path]:
        media = {".mp4", ".mkv", ".webm", ".mov", ".flv", ".ts", ".m4a", ".mp3",
                 ".opus", ".flac", ".wav", ".aac", ".ogg", ".srt", ".vtt", ".ass"}
        return [
            p for p in ctx.workdir.rglob("*")
            if p.is_file() and p.suffix.lower() in media and not p.name.endswith(".part")
        ]


def _num(value: str):
    if not value or value in ("NA", "None", "N/A"):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _pp_progress(line: str):
    from .base import Progress
    label = line.strip()
    if label.startswith("["):
        end = label.find("]")
        if end != -1:
            label = label[1:end]
    return Progress(stage="postprocessing", fraction=None, message=label)
