"""``omadown`` command line."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, ipc, naming, notify
from .backends import ProbeError, build_registry, find_ytdlp
from .config import Config
from .db import Store
from .engine import Engine
from .models import DownloadOptions, Priority, QUALITY_HEIGHTS, State
from .postproc import detect_hardware_accel, find_ffmpeg
from .util import config_dir, data_dir, human_bytes, human_duration, human_speed, shorten, which

QUALITY_CHOICES = ["best", "4k", "1440p", "1080p", "720p", "480p", "360p", "240p", "144p",
                   "worst", "audio"]


# --------------------------------------------------------------------------- utils

def _add_options(parser: argparse.ArgumentParser) -> None:
    group = parser.add_argument_group("下载选项")
    group.add_argument("-q", "--quality", choices=QUALITY_CHOICES, default="best",
                       help="画质上限（4k/1080p/720p...），best 表示不限制")
    group.add_argument("-x", "--audio-only", action="store_true", help="只提取音频")
    group.add_argument("--audio-format", default="", help="音频格式：m4a/mp3/flac/opus/wav")
    group.add_argument("--max-resolution", type=int, default=0, metavar="N",
                       help="最大分辨率（像素高度），0 表示不限制")
    group.add_argument("--prefer-mp4", dest="prefer_mp4", action="store_true", default=None,
                       help="在同等分辨率下优先选择 MP4")
    group.add_argument("--no-prefer-mp4", dest="prefer_mp4", action="store_false",
                       help="不特别偏好 MP4")
    group.add_argument("--force-mp4", dest="force_mp4", action="store_true", default=None,
                       help="强制选择 MP4，忽略更高分辨率的其它容器")
    group.add_argument("--subtitles", dest="subtitles", action="store_true", default=None,
                       help="下载字幕")
    group.add_argument("--no-subtitles", dest="subtitles", action="store_false",
                       help="不下载字幕")
    group.add_argument("--sub-langs", default="", help="字幕语言，逗号分隔，如 zh-Hans,en")
    group.add_argument("--subs-only", action="store_true", help="只下载字幕，不下载视频")
    group.add_argument("--embed-subs", dest="embed_subs", action="store_true", default=None,
                       help="把字幕封装进视频")
    group.add_argument("--playlist", dest="playlist", action="store_true", default=None,
                       help="下载整个播放列表/频道")
    group.add_argument("--no-playlist", dest="playlist", action="store_false",
                       help="只下载当前视频")
    group.add_argument("--playlist-items", default="", metavar="RANGE",
                       help="播放列表范围，如 1-20")
    group.add_argument("--rate-limit", default="", metavar="RATE", help="限速，如 5M")
    group.add_argument("--proxy", default="", help="代理，如 socks5://127.0.0.1:1080")
    group.add_argument("--cookies-from-browser", default="", metavar="BROWSER",
                       help="从浏览器读取 Cookie：firefox/chrome/brave/edge")
    group.add_argument("--cookies", dest="cookies_file", default="", metavar="FILE",
                       help="使用 Netscape 格式的 cookie 文件")
    group.add_argument("--connections", type=int, default=0, help="并发分片数")
    group.add_argument("-o", "--output-dir", default="", help="输出目录（覆盖配置）")
    group.add_argument("--name-format", default="", help="文件名方案，如 {uploader}/{title}.{ext}")
    group.add_argument("--no-postprocess", dest="postprocess", action="store_false", default=None,
                       help="跳过后期处理")
    group.add_argument("--postprocess", dest="postprocess", action="store_true",
                       help="强制执行后期处理")
    group.add_argument("--hls-mode", choices=["remux", "reencode", "none"], default="",
                       help="HLS/DASH 流的处理方式")
    group.add_argument("--transcode", choices=["", "h264", "h265"], default="",
                       help="转码为 H.264 / H.265")
    group.add_argument("--priority", choices=["high", "normal", "low"], default="normal")
    group.add_argument("--backend", choices=["", "ytdlp", "hls", "uge", "direct"], default="",
                       help="强制使用某个下载引擎（默认自动选择）")
    group.add_argument("--tag", default="", help="给任务打标签，便于检索")


def _options_from_args(args: argparse.Namespace) -> DownloadOptions:
    priority = {"high": Priority.HIGH.value, "normal": Priority.NORMAL.value,
                "low": Priority.LOW.value}[getattr(args, "priority", "normal")]
    quality = args.quality
    audio_only = bool(args.audio_only) or quality == "audio"
    if quality == "audio":
        quality = "best"
    langs = [s.strip() for s in (args.sub_langs or "").split(",") if s.strip()]
    return DownloadOptions(
        quality=quality,
        audio_only=audio_only,
        audio_format=args.audio_format,
        max_resolution=args.max_resolution,
        prefer_mp4=args.prefer_mp4,
        force_mp4=args.force_mp4,
        subtitles=args.subtitles,
        subtitle_langs=langs or None,
        subs_only=bool(args.subs_only),
        embed_subs=args.embed_subs,
        playlist=args.playlist,
        playlist_items=args.playlist_items,
        rate_limit=args.rate_limit,
        proxy=args.proxy,
        cookies_from_browser=args.cookies_from_browser,
        cookies_file=args.cookies_file,
        connections=args.connections,
        output_dir=args.output_dir,
        name_format=args.name_format,
        postprocess=args.postprocess,
        hls_mode=args.hls_mode,
        transcode=args.transcode,
        priority=priority,
        tag=args.tag,
        backend=getattr(args, "backend", "") or "",
    )


def _load(args: argparse.Namespace) -> tuple[Config, Store]:
    config = Config.load(getattr(args, "config", None))
    if getattr(args, "output_dir", ""):
        config.general.download_dir = args.output_dir
    if getattr(args, "max_concurrent", None):
        config.general.max_concurrent = args.max_concurrent
    store = Store()
    return config, store


def _open_engine(config: Config, store: Store, args: argparse.Namespace) -> Engine:
    return Engine(config, store, render=bool(getattr(args, "render", False)))


def _print_json(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ------------------------------------------------------------------ subcommands

def cmd_add(args: argparse.Namespace) -> int:
    urls = list(args.urls)
    if args.stdin:
        urls += sys.stdin.read().split()
    if not urls:
        print("没有提供链接。用法: omadown add <URL>...", file=sys.stderr)
        return 2

    from .util import dedupe, split_urls
    flat: list[str] = []
    for url in urls:
        flat.extend(split_urls(url) or [url])
    flat = dedupe(flat)

    options = _options_from_args(args)

    daemon = ipc.send_command("add", urls=flat, options=options.to_dict(),
                              force=args.force)
    if daemon is not None and daemon.get("ok"):
        result = daemon["result"]
        for entry in result["added"]:
            print(f"已加入队列 #{entry['id']}  {entry['url']}")
        for url, reason in result["skipped"]:
            print(f"跳过 {url} ({reason})")
        if not args.quiet and result["added"]:
            print("守护进程正在处理，查看进度: omadown status")
        return 0

    config, store = _load(args)
    engine = _open_engine(config, store, args)
    result = engine.submit(flat, options, allow_duplicates=args.force)
    for item in result.added:
        print(f"已加入队列 #{item.id}  {item.url}")
    for url, reason in result.skipped:
        print(f"跳过 {url} ({reason})")
    if not result.added:
        return 1
    if args.quiet:
        return 0
    if args.foreground:
        return _process(engine, args, exit_when_done=True)
    print("提示: 运行 `omadown run` 开始下载，或启动守护进程 `omadown daemon`。")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    config, store = _load(args)
    engine = _open_engine(config, store, args)
    return _process(engine, args, exit_when_done=not args.watch)


def _process(engine: Engine, args: argparse.Namespace, *, exit_when_done: bool) -> int:
    stop = False

    def on_signal(_signum, _frame):
        nonlocal stop
        stop = True
        engine.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    def on_event(kind: str, item) -> None:
        if args.quiet or not item:
            return
        if kind == "done":
            print(f"\u2713 完成  {item.title}  ->  {item.output_path}")
        elif kind == "failed":
            print(f"\u2717 失败  {item.title}  {item.error}", file=sys.stderr)
        elif kind == "start":
            print(f"\u2913 开始  {item.title} [{item.backend}]")

    engine.observe(on_event)
    engine.run(exit_when_done=exit_when_done)
    stats = engine.store.stats()
    if not args.quiet:
        print(f"\n完成 {stats.get('done', 0)} · 失败 {stats.get('failed', 0)}")
    return 0 if stats.get("failed", 0) == 0 else 1


def cmd_daemon(args: argparse.Namespace) -> int:
    config, store = _load(args)
    engine = _open_engine(config, store, args)

    server = ipc.ControlServer(engine)
    try:
        server.start()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    watcher = None
    if args.watch_clipboard or (config.general.clipboard_watch and not args.no_clipboard):
        from .watch import ClipboardWatcher
        options = _options_from_args(args)

        def on_urls(urls: list[str]) -> None:
            result = engine.submit(urls, options)
            for item in result.added:
                print(f"[剪贴板] 已加入 #{item.id}  {item.url}", flush=True)
            engine.write_state()

        watcher = ClipboardWatcher(
            on_urls,
            options=options,
            notify_enabled=config.general.notify,
            accept_all=bool(args.accept_all),
        )
        watcher.start()

    writer_stop = False

    def on_event(_kind: str, _item) -> None:
        engine.write_state()

    engine.observe(on_event)
    engine.write_state()

    notify.daemon_ready(getattr(args, "host", "") or os.uname().nodename,
                        config.general.notify)
    print(f"omadown 守护进程已启动 (pid {os.getpid()})")
    print(f"  控制套接字: {server.path}")
    print(f"  状态快照:   {ipc.control_socket_path().parent / 'state.json'}")
    print(f"  下载目录:   {config.output_dir}")

    def on_signal(_signum, _frame):
        nonlocal writer_stop
        writer_stop = True
        engine.shutdown()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, on_signal)
        except (ValueError, OSError):
            pass

    try:
        engine.run(on_idle=engine.write_state)
    finally:
        engine.write_state()
        if watcher:
            watcher.stop()
        server.stop()
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    config, store = _load(args)
    engine = _open_engine(config, store, args)
    from . import tui
    tui.run(engine, config, auto_start=not args.attach)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from . import status
    if args.waybar:
        print(json.dumps(status.waybar(args.compact), ensure_ascii=False))
    elif args.json:
        reply = ipc.send_command("status")
        if reply and reply.get("ok"):
            _print_json(reply["result"])
        else:
            _print_json(status._snapshot())
    else:
        print(status.plain(args.limit, args.all))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    config, store = _load(args)
    items = store.history(args.limit) if args.history else store.queue()
    if args.json:
        _print_json([Engine._serialize(i) for i in items])
        return 0
    if not items:
        print("(空)")
        return 0
    for item in items:
        pct = int(round((item.progress or 0) * 100))
        title = shorten(item.title or item.url, 60)
        print(f"#{item.id:<5} {item.state:<14} {pct:3d}%  {title}")
    return 0


def cmd_control(args: argparse.Namespace) -> int:
    ids = [int(i) for i in args.ids] if args.ids else []
    command = {"pause": "pause", "resume": "resume", "cancel": "cancel",
               "retry": "retry", "remove": "remove", "clear": "clear"}[args.action]
    reply = ipc.send_command(command, ids=ids)
    if reply is not None and reply.get("ok"):
        _print_json(reply["result"])
        return 0

    config, store = _load(args)
    engine = _open_engine(config, store, args)
    if args.action == "pause":
        count = sum(1 for i in ids if engine.pause(i))
    elif args.action == "resume":
        count = sum(1 for i in ids if engine.resume(i))
    elif args.action == "cancel":
        count = sum(1 for i in ids if engine.cancel(i)) if ids else engine.cancel_all()
    elif args.action == "retry":
        count = engine.retry(ids or None)
    elif args.action == "remove":
        count = engine.remove(ids)
    else:
        count = engine.clear_completed()
    print(f"{args.action}: {count}")
    return 0


def cmd_paste(args: argparse.Namespace) -> int:
    from .watch import ClipboardWatcher, watch_once
    urls = watch_once()
    if not urls:
        print("剪贴板中没有链接。", file=sys.stderr)
        return 1
    filtered = ClipboardWatcher.filter(ClipboardWatcher(lambda _u: None), urls) \
        if not args.accept_all else urls
    if not filtered:
        print(f"剪贴板里有 {len(urls)} 个链接，但都不像媒体地址（用 --accept-all 强制）。")
        if not args.force:
            return 1
        filtered = urls
    return cmd_add(argparse.Namespace(**{**vars(args), "urls": filtered, "stdin": False}))


def cmd_probe(args: argparse.Namespace) -> int:
    config, store = _load(args)
    engine = _open_engine(config, store, args)
    backend = engine.registry.for_url(args.url)
    options = _options_from_args(args)
    from .backends import CancelToken, DownloadContext
    from .models import DownloadItem
    item = DownloadItem(url=args.url, options=options)
    ctx = DownloadContext(item=item, workdir=data_dir() / "tmp" / "probe",
                          options={"cancel": CancelToken()}, ffmpeg=engine.ffmpeg,
                          config=config)
    try:
        info = backend.probe(args.url, ctx)
    except ProbeError as exc:
        print(f"无法解析: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = {
            "backend": backend.name, "title": info.title, "id": info.id,
            "uploader": info.uploader, "duration": info.duration,
            "extractor": info.extractor, "is_playlist": info.is_playlist,
            "is_live": info.is_live, "entry_count": len(info.entries),
            "formats": [f.__dict__ | {"label": f.label} for f in info.formats],
        }
        _print_json(payload)
        return 0

    print(f"后端      : {backend.name}")
    print(f"标题      : {info.title}")
    print(f"来源      : {info.extractor}  {info.uploader}")
    print(f"时长      : {human_duration(info.duration)}   直播: {'是' if info.is_live else '否'}")
    if info.is_playlist:
        print(f"播放列表  : {info.playlist_title}  共 {len(info.entries)} 项")
        for entry in info.entries[:10]:
            print(f"   - {shorten(entry.title, 70)}")
        if len(info.entries) > 10:
            print(f"   ... 另有 {len(info.entries) - 10} 项")
    if info.formats:
        print("可选格式  :")
        for fmt in sorted(info.formats, key=lambda f: (-(f.height or 0), f.ext))[:15]:
            size = human_bytes(fmt.filesize) if fmt.filesize else "-"
            print(f"   {fmt.format_id:<14} {fmt.label:<36} {size}")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    path = Config.path()
    if args.edit:
        editor = os.environ.get("EDITOR") or which("nvim") or which("vim") or "vi"
        if not path.exists():
            config.save()
        return subprocess.call([editor, str(path)])
    if args.set:
        key, _, value = args.set.partition("=")
        if not _:
            print("用法: omadown config --set section.key=value", file=sys.stderr)
            return 2
        try:
            config.set(key.strip(), value.strip())
        except KeyError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        config.validate()
        saved = config.save()
        print(f"已写入 {saved}: {key} = {value}")
        return 0
    if args.get:
        print(config.get(args.get))
        return 0
    if args.path_only:
        print(path)
        return 0
    print(config.to_toml())
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    config = Config.load(args.config)
    rows: list[tuple[str, str, str]] = []

    ytdlp = find_ytdlp()
    ytdlp_version = ""
    if ytdlp:
        try:
            ytdlp_version = subprocess.run(
                [ytdlp, "--version"], capture_output=True, text=True, timeout=20
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            ytdlp_version = "?"
    rows.append(("yt-dlp", "ok" if ytdlp else "missing", f"{ytdlp or '-'} {ytdlp_version}"))

    ffmpeg = find_ffmpeg()
    rows.append(("ffmpeg", "ok" if ffmpeg else "missing", ffmpeg or "后期处理不可用"))
    rows.append(("硬件加速", "ok", detect_hardware_accel(ffmpeg)))

    wl_paste = which("wl-paste")
    rows.append(("wl-clipboard", "ok" if wl_paste else "missing",
                 wl_paste or "剪贴板监听不可用 (omarchy-pkg-install wl-clipboard)"))

    notify_runner = notify.available()
    rows.append(("通知", "ok" if notify_runner else "missing", notify_runner or "缺少 libnotify"))

    try:
        import curses  # noqa: F401
        rows.append(("curses", "ok", "TUI 可用"))
    except ImportError:
        rows.append(("curses", "missing", "TUI 不可用"))

    playwright = False
    try:
        import playwright  # noqa: F401
        playwright = True
    except ImportError:
        pass
    rows.append(("playwright", "ok" if playwright else "optional",
                 "渲染模式可用" if playwright else "未安装（仅影响 --render 的网页嗅探）"))

    daemon = ipc.daemon_running()
    rows.append(("守护进程", "ok" if daemon else "stopped",
                 "运行中" if daemon else "omadown daemon 可启动"))

    print("omadown 环境检查\n")
    for name, status, detail in rows:
        print(f"  {name:<16} {status:<10} {detail}")
    print()
    print(f"  配置        {Config.path()}")
    print(f"  数据        {data_dir()}")
    print(f"  下载目录    {config.output_dir}")
    print(f"  并发数      {config.general.max_concurrent}")
    print(f"  文件名方案  {config.general.file_name_format}")
    return 0


def cmd_backends(args: argparse.Namespace) -> int:
    registry = build_registry(render=args.render)
    for backend in registry.all():
        status = "可用" if backend.available() else "不可用"
        print(f"{backend.priority:>3}  {backend.name:<8} {status}")
    return 0


def cmd_notify_test(args: argparse.Namespace) -> int:
    ok = notify.notify("omadown 测试通知", "如果你看到这条消息，通知链路正常。",
                       enabled=True)
    print("已发送" if ok else "发送失败（检查 libnotify / 通知守护进程）")
    return 0 if ok else 1


# ------------------------------------------------------------------------ parser

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="omadown",
        description="omadown - Downie 4 的 Omarchy/Linux 等价实现（队列式多媒体下载器）",
    )
    parser.add_argument("--version", action="version", version=f"omadown {__version__}")
    parser.add_argument("--config", default=os.environ.get("OMADOWN_CONFIG", ""),
                        help="指定配置文件路径")
    parser.add_argument("--render", action="store_true",
                        help="网页嗅探时使用无头浏览器渲染（需 playwright）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add", help="把链接加入队列")
    p.add_argument("urls", nargs="*")
    p.add_argument("--stdin", action="store_true", help="从标准输入读取链接")
    p.add_argument("--force", action="store_true", help="允许重复链接")
    p.add_argument("-f", "--foreground", action="store_true", help="立即在前台开始下载")
    p.add_argument("--quiet", action="store_true")
    _add_options(p)
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("run", help="处理队列（前台）")
    p.add_argument("--watch", action="store_true", help="队列清空后继续等待新任务")
    p.add_argument("--max-concurrent", type=int)
    p.add_argument("--quiet", action="store_true")
    _add_options(p)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("daemon", help="启动守护进程（队列 + 剪贴板监听 + 控制套接字）")
    p.add_argument("--host", default="", help="通知里显示的主机名")
    p.add_argument("--watch-clipboard", action="store_true", help="强制开启剪贴板监听")
    p.add_argument("--no-clipboard", action="store_true", help="关闭剪贴板监听")
    p.add_argument("--accept-all", action="store_true", help="接受剪贴板中的任意链接")
    p.add_argument("--max-concurrent", type=int)
    p.add_argument("--quiet", action="store_true")
    _add_options(p)
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("tui", help="打开队列界面")
    p.add_argument("--attach", action="store_true", help="只查看，不接管下载线程")
    p.add_argument("--max-concurrent", type=int)
    _add_options(p)
    p.set_defaults(func=cmd_tui)

    p = sub.add_parser("status", help="查看队列状态")
    p.add_argument("--waybar", action="store_true", help="输出 Waybar 模块 JSON")
    p.add_argument("--compact", action="store_true", help="空闲时输出空内容")
    p.add_argument("--json", action="store_true")
    p.add_argument("--limit", type=int, default=15)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("list", help="列出队列")
    p.add_argument("--history", action="store_true", help="列出历史记录")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pause", help="暂停排队中的任务")
    p.add_argument("ids", nargs="*")
    p.set_defaults(func=cmd_control, action="pause")

    p = sub.add_parser("resume", help="恢复已暂停的任务")
    p.add_argument("ids", nargs="*")
    p.set_defaults(func=cmd_control, action="resume")

    p = sub.add_parser("cancel", help="取消任务（不带 id 表示全部）")
    p.add_argument("ids", nargs="*")
    p.set_defaults(func=cmd_control, action="cancel")

    p = sub.add_parser("retry", help="重试失败的任务")
    p.add_argument("ids", nargs="*")
    p.set_defaults(func=cmd_control, action="retry")

    p = sub.add_parser("remove", help="从队列中删除任务")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_control, action="remove")

    p = sub.add_parser("clear", help="清理已完成/失败记录")
    p.set_defaults(func=cmd_control, action="clear", ids=[])

    p = sub.add_parser("paste", help="从剪贴板读取链接并加入队列")
    p.add_argument("--force", action="store_true", help="即使是重复链接也加入")
    p.add_argument("--accept-all", action="store_true", help="不做过媒体地址过滤")
    p.add_argument("urls", nargs="*")
    p.add_argument("--quiet", action="store_true")
    _add_options(p)
    p.set_defaults(func=cmd_paste, stdin=False)

    p = sub.add_parser("probe", help="解析链接，列出可用格式")
    p.add_argument("url")
    p.add_argument("--json", action="store_true")
    _add_options(p)
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("config", help="查看/修改配置")
    p.add_argument("--edit", action="store_true", help="用 $EDITOR 打开配置文件")
    p.add_argument("--path", dest="path_only", action="store_true", help="只打印配置文件路径")
    p.add_argument("--get", metavar="SECTION.KEY", help="读取某一项")
    p.add_argument("--set", metavar="SECTION.KEY=VALUE", help="写入某一项")
    p.set_defaults(func=cmd_config)

    p = sub.add_parser("doctor", help="检查依赖与环境")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("backends", help="列出下载后端及其可用状态")
    p.set_defaults(func=cmd_backends)

    p = sub.add_parser("notify-test", help="发送一条测试通知")
    p.set_defaults(func=cmd_notify_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except ProbeError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
