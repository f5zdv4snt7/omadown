"""The queue engine: probing, scheduling, downloading, post-processing, history."""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from . import naming, notify
from .backends import (
    BackendError,
    BackendRegistry,
    CancelToken,
    Cancelled,
    DownloadContext,
    ProbeError,
    Progress,
    build_registry,
)
from .config import Config
from .db import Store
from .models import DownloadItem, DownloadOptions, MediaInfo, Priority, QUALITY_HEIGHTS, State
from .postproc import PostProcessor, find_ffmpeg
from .util import data_dir, ensure_dir, human_bytes, shorten, state_path


@dataclass
class SubmitResult:
    added: list[DownloadItem]
    skipped: list[tuple[str, str]]


class Engine:
    def __init__(
        self,
        config: Config,
        store: Store | None = None,
        *,
        registry: BackendRegistry | None = None,
        render: bool = False,
        enable_uge: bool = True,
    ) -> None:
        self.config = config
        self.store = store or Store()
        self.registry = registry or build_registry(render=render, enable_uge=enable_uge)
        self.ffmpeg = find_ffmpeg()

        self._lock = threading.RLock()
        self._tokens: dict[int, CancelToken] = {}
        self._view: dict[int, DownloadItem] = {}
        self._observers: list[Callable[[str, DownloadItem | None], None]] = []
        self._last_publish = 0.0
        self._completed_since_start = 0
        self.stopping = threading.Event()
        self._work_root = ensure_dir(data_dir() / "tmp")

        self.store.reset_interrupted()

    # ------------------------------------------------------------------ observers
    def observe(self, callback: Callable[[str, DownloadItem | None], None]) -> None:
        self._observers.append(callback)

    def _publish(self, kind: str = "update", item: DownloadItem | None = None, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_publish < 0.4:
            return
        self._last_publish = now
        for callback in list(self._observers):
            try:
                callback(kind, item)
            except Exception:
                pass

    # -------------------------------------------------------------------- intake
    def submit(
        self,
        urls: Sequence[str],
        options: DownloadOptions | None = None,
        *,
        allow_duplicates: bool = False,
    ) -> SubmitResult:
        added: list[DownloadItem] = []
        skipped: list[tuple[str, str]] = []
        active = [State.QUEUED.value, State.PREPARING.value, State.DOWNLOADING.value,
                  State.POSTPROCESSING.value, State.PAUSED.value]

        for url in urls:
            url = url.strip()
            if not url:
                continue
            if not allow_duplicates and self.store.has_url(url, active):
                skipped.append((url, "已在队列中"))
                continue
            item = DownloadItem(url=url, options=options or DownloadOptions(),
                                priority=(options.priority if options else Priority.NORMAL.value))
            self.store.add(item)
            self.store.log_event("add", item.id, url)
            with self._lock:
                self._view[item.id] = item
            added.append(item)

        if added:
            self._publish("add", added[-1], force=True)
        return SubmitResult(added=added, skipped=skipped)

    def submit_text(self, text: str, options: DownloadOptions | None = None) -> SubmitResult:
        from .util import dedupe, split_urls
        return self.submit(dedupe(split_urls(text)), options)

    # ------------------------------------------------------------------ scheduler
    def run(
        self,
        *,
        exit_when_done: bool = False,
        poll_interval: float = 0.6,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        concurrency = max(1, int(self.config.general.max_concurrent))
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="omadown") as pool:
            futures: dict[Any, int] = {}
            while not self.stopping.is_set():
                self._reap(futures)
                free = concurrency - len(futures)
                if free > 0:
                    for item in self._next_queued(free):
                        futures[pool.submit(self._run_item, item)] = item.id
                if futures:
                    wait(list(futures), timeout=poll_interval, return_when=FIRST_COMPLETED)
                else:
                    if on_idle:
                        on_idle()
                    if exit_when_done:
                        break
                    time.sleep(poll_interval)
            for future in list(futures):
                future.cancel()

    def _reap(self, futures: dict) -> None:
        for future in [f for f in futures if f.done()]:
            item_id = futures.pop(future)
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001
                item = self.store.get(item_id)
                if item:
                    item.mark(State.FAILED, error=str(exc), finished_at=time.time())
                    self.store.update(item)
                    self._publish("failed", item, force=True)

    def _next_queued(self, count: int) -> list[DownloadItem]:
        items = self.store.list(states=[State.QUEUED.value], limit=count,
                               order="priority ASC, created_at ASC")
        return items

    # --------------------------------------------------------------- the job body
    def _run_item(self, item: DownloadItem) -> None:
        assert item.id is not None
        token = CancelToken()
        with self._lock:
            self._tokens[item.id] = token
            self._view[item.id] = item

        try:
            item.mark(State.PREPARING, started_at=item.started_at or time.time(),
                      attempts=item.attempts + 1, error="")
            self.store.update(item)
            self._publish("prepare", item, force=True)

            expanded = self._prepare(item)
            if expanded:
                return

            backend = self._backend_for(item)
            item.backend = backend.name
            workdir = self._workdir(item)
            ctx = self._context(item, workdir, token)

            item.mark(State.DOWNLOADING)
            self.store.update(item)
            self._publish("start", item, force=True)

            produced = backend.download(ctx, self._progress_cb(item, token))

            if self.config.postprocessing.enabled:
                item.mark(State.POSTPROCESSING)
                self.store.update(item)
                self._publish("postprocess", item, force=True)
                processor = PostProcessor(
                    self.config,
                    on_step=lambda step: self._progress_cb(item, token)(
                        Progress(stage="postprocessing", message=step)),
                    cancel=token,
                )
                result = processor.run_for(item, produced, workdir)
                produced = result.path
                if result.warnings:
                    item.meta.setdefault("warnings", []).extend(result.warnings)

            destination = self._install(item, produced, workdir)

            item.mark(
                State.DONE, output_path=str(destination), progress=1.0,
                finished_at=time.time(), error="",
                stage=f"{human_bytes(destination.stat().st_size)}",
            )
            self.store.update(item)
            self.store.log_event("done", item.id, str(destination))
            self._completed_since_start += 1
            notify.download_finished(item.title, str(destination), self.config.general.notify)
            self._publish("done", item, force=True)

            if self.config.general.remove_completed:
                self._schedule_removal(item)

        except Cancelled:
            item.mark(State.CANCELLED, finished_at=time.time(), cancelled=True,
                      error=item.error or "已取消")
            self.store.update(item)
            self._publish("cancelled", item, force=True)
        except (BackendError, ProbeError) as exc:
            self._fail(item, str(exc))
        except Exception as exc:  # noqa: BLE001
            self._fail(item, f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._tokens.pop(item.id, None)
            token.cancel()  # make sure nothing keeps running
            self._cleanup_workdir(item)

    # ------------------------------------------------------------------ preparing
    def _prepare(self, item: DownloadItem) -> bool:
        """Probe the URL. Returns True when the item was replaced by playlist children."""
        assert item.id is not None
        workdir = self._workdir(item)
        token = self._tokens.get(item.id) or CancelToken()
        ctx = self._context(item, workdir, token)

        backend = self._backend_for(item)
        info = backend.probe(item.url, ctx)

        playlist_choice = item.options.playlist
        if playlist_choice is None:
            playlist_choice = False

        if info.is_playlist and info.entries:
            if playlist_choice:
                children: list[DownloadItem] = []
                for entry in info.entries:
                    child = DownloadItem(
                        url=entry.url or entry.webpage_url,
                        title=entry.title,
                        extractor=entry.extractor or info.extractor,
                        duration=entry.duration,
                        playlist_title=info.playlist_title or info.title,
                        playlist_index=entry.playlist_index,
                        priority=item.priority,
                        options=item.options,
                        meta={"parent": item.id},
                    )
                    if not child.url:
                        continue
                    self.store.add(child)
                    with self._lock:
                        self._view[child.id] = child
                    children.append(child)
                item.mark(State.CANCELLED, cancelled=True, finished_at=time.time(),
                          error=f"已展开为 {len(children)} 个条目")
                self.store.update(item)
                self.store.log_event("expand", item.id, f"{len(children)} entries")
                self._publish("expand", item, force=True)
                return True

            entry = info.entries[0]
            if entry.url or entry.webpage_url:
                item.url = entry.url or entry.webpage_url
                info = entry

        self._apply_info(item, info)
        self.store.update(item)
        self._publish("prepared", item, force=True)
        return False

    def _backend_for(self, item: DownloadItem):
        """Honour a forced engine, otherwise let the registry pick by priority."""
        forced = (item.options.backend or "").strip()
        if forced:
            try:
                backend = self.registry.by_name(forced)
            except KeyError as exc:
                raise ProbeError(f"未知的下载后端: {forced}") from exc
            if not backend.available():
                raise ProbeError(f"后端 {forced} 当前不可用")
            return backend
        return self.registry.for_url(item.url)

    @staticmethod
    def _apply_info(item: DownloadItem, info: MediaInfo) -> None:
        item.title = info.title or item.title or info.id or "untitled"
        item.extractor = info.extractor or item.extractor
        item.duration = info.duration or item.duration
        item.playlist_title = info.playlist_title or item.playlist_title
        item.playlist_index = info.playlist_index or item.playlist_index
        item.meta.update({
            "title": item.title,
            "id": info.id,
            "uploader": info.uploader,
            "extractor": item.extractor,
            "thumbnail": info.thumbnail,
            "duration": item.duration,
            "playlist": item.playlist_title,
            "playlist_index": item.playlist_index,
            "is_live": info.is_live,
            "webpage_url": info.webpage_url or item.url,
        })
        if info.formats:
            item.meta["formats"] = [
                f.label for f in sorted(
                    info.formats, key=lambda f: (-(f.height or 0), f.ext)
                )[:12]
            ]
            heights = [f.height for f in info.formats if f.height]
            if heights:
                # The {resolution} token must describe what we will actually get,
                # not the largest rendition the site happens to offer.
                cap = item.options.max_resolution or QUALITY_HEIGHTS.get(item.options.quality, 0)
                if cap:
                    within = [h for h in heights if h <= cap]
                    chosen = max(within) if within else min(heights)
                else:
                    chosen = max(heights)
                item.meta["resolution"] = chosen
                item.meta["quality"] = f"{chosen}p"
        if info.raw and "id" in info.raw:
            item.meta.setdefault("extractor_id", info.raw.get("id"))

    # ------------------------------------------------------------------- progress
    def _progress_cb(self, item: DownloadItem, token: CancelToken) -> Callable[[Progress], None]:
        last = {"at": 0.0}

        def callback(progress: Progress) -> None:
            if token.cancelled:
                raise Cancelled("cancelled by user")
            if progress.stage == "postprocessing":
                item.state = State.POSTPROCESSING.value
            elif progress.stage in ("downloading", "preparing"):
                if item.state_enum != State.POSTPROCESSING or progress.stage == "downloading":
                    item.state = State.DOWNLOADING.value
            if progress.fraction is not None:
                item.progress = max(item.progress, progress.fraction)
            if progress.downloaded:
                item.downloaded = progress.downloaded
            if progress.total:
                item.total = progress.total
            item.speed = progress.speed
            item.eta = progress.eta
            if progress.message:
                item.stage = progress.message
            now = time.monotonic()
            if now - last["at"] >= 0.5:
                last["at"] = now
                self._persist_progress(item)
                self._publish("progress", item)

        return callback

    def _persist_progress(self, item: DownloadItem) -> None:
        if item.id is None:
            return
        self.store.set_state(
            item.id, item.state,
            progress=item.progress, downloaded=item.downloaded, total=item.total,
        )

    # -------------------------------------------------------------------- outputs
    def _install(self, item: DownloadItem, produced: Path, workdir: Path) -> Path:
        """Move the finished file into the download folder under its final name."""
        template = item.options.name_format or self.config.general.file_name_format
        meta = dict(item.meta)
        meta.update({
            "title": item.title,
            "playlist": item.playlist_title,
            "playlist_index": item.playlist_index,
            "extractor": item.extractor,
        })
        ext = produced.suffix.lstrip(".") or "mp4"
        try:
            relative = naming.render(template, meta, ext=ext)
        except naming.NameSchemeError:
            relative = naming.render(naming.DEFAULT_FORMAT, meta, ext=ext)

        if not Path(relative).suffix:
            relative += f".{ext}"

        target_dir = self.config.output_dir
        if self.config.general.group_playlists and item.playlist_title:
            target_dir = ensure_dir(target_dir / naming.sanitize_component(item.playlist_title))
        destination = target_dir / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination = naming.unique_path(destination)

        if self.config.postprocessing.keep_original and produced.stat().st_size > 0:
            sidecar = destination.with_suffix(f".original{produced.suffix}")
            shutil.copy2(produced, naming.unique_path(sidecar))

        shutil.move(str(produced), str(destination))
        self._relocate_subtitles(produced, destination)
        return destination

    @staticmethod
    def _relocate_subtitles(produced: Path, destination: Path) -> None:
        for extra in produced.parent.glob(f"{produced.stem}.*"):
            if extra == produced or extra.is_dir():
                continue
            if extra.suffix.lower() in (".srt", ".vtt", ".ass", ".json", ".jpg", ".webp", ".png"):
                shutil.move(str(extra), str(destination.with_suffix(extra.suffix)))

    def _fail(self, item: DownloadItem, message: str) -> None:
        retries = self.config.general.max_retries
        if item.attempts <= retries and not item.cancelled:
            item.mark(State.QUEUED, error=f"{message} (第 {item.attempts} 次失败，将重试)")
            self.store.update(item)
            self._publish("retry", item, force=True)
            return
        item.mark(State.FAILED, error=message, finished_at=time.time())
        self.store.update(item)
        self.store.log_event("failed", item.id, message)
        notify.download_failed(item.title or item.url, message, self.config.general.notify)
        self._publish("failed", item, force=True)

    def _schedule_removal(self, item: DownloadItem) -> None:
        def remove_later() -> None:
            time.sleep(3)
            if item.id:
                self.store.remove([item.id])
                with self._lock:
                    self._view.pop(item.id, None)
                self._publish("removed", item, force=True)
        threading.Thread(target=remove_later, daemon=True).start()

    def _workdir(self, item: DownloadItem) -> Path:
        # The slug is captured once: the title is only known after probing, and
        # the work directory must not move between preparation and download.
        slug = item.meta.get("slug")
        if not slug:
            slug = naming.sanitize_component(shorten(item.title or "item", 40)).replace("/", "_")
            item.meta["slug"] = slug
        path = self._work_root / f"{item.id or 0}-{slug}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _cleanup_workdir(self, item: DownloadItem) -> None:
        if os.environ.get("OMADOWN_KEEP_TEMP"):
            return
        try:
            path = self._workdir(item)
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

    def _context(self, item: DownloadItem, workdir: Path, token: CancelToken) -> DownloadContext:
        return DownloadContext(
            item=item, workdir=workdir, options={"cancel": token},
            net=None, ffmpeg=self.ffmpeg, config=self.config,
        )

    # ------------------------------------------------------------ queue controls
    def cancel(self, item_id: int) -> bool:
        with self._lock:
            token = self._tokens.get(item_id)
        item = self.store.get(item_id)
        if item is None:
            return False
        item.cancelled = True
        if token is not None:
            token.cancel()
            return True
        if item.state_enum == State.QUEUED:
            item.mark(State.CANCELLED, finished_at=time.time())
            self.store.update(item)
            self._publish("cancelled", item, force=True)
        return True

    def cancel_all(self) -> int:
        count = 0
        for item in self.store.queue():
            if item.id and self.cancel(item.id):
                count += 1
        return count

    def pause(self, item_id: int) -> bool:
        item = self.store.get(item_id)
        if item is None or item.state_enum != State.QUEUED:
            return False
        item.mark(State.PAUSED)
        self.store.update(item)
        self._publish("paused", item, force=True)
        return True

    def resume(self, item_id: int) -> bool:
        item = self.store.get(item_id)
        if item is None or item.state_enum != State.PAUSED:
            return False
        item.mark(State.QUEUED)
        self.store.update(item)
        self._publish("resumed", item, force=True)
        return True

    def retry(self, ids: Iterable[int] | None = None) -> int:
        if ids is None:
            items = self.store.list(states=[State.FAILED.value, State.CANCELLED.value])
            ids = [i.id for i in items if i.id]
        count = 0
        for item_id in ids:
            item = self.store.get(item_id)
            if item is None:
                continue
            item.attempts = 0
            item.reset_for_retry()
            self.store.update(item)
            self._publish("requeued", item, force=True)
            count += 1
        return count

    def remove(self, ids: Iterable[int]) -> int:
        removed = self.store.remove(ids)
        with self._lock:
            for item_id in ids:
                self._view.pop(item_id, None)
        self._publish("removed", None, force=True)
        return removed

    def clear_completed(self) -> int:
        count = self.store.clear(states=[State.DONE.value, State.FAILED.value, State.CANCELLED.value])
        self._publish("cleared", None, force=True)
        return count

    def shutdown(self) -> None:
        self.stopping.set()
        self.cancel_all()

    # ---------------------------------------------------------------- observation
    def snapshot(self, *, limit: int = 200) -> dict[str, Any]:
        items = self.store.list(order="CASE state WHEN 'downloading' THEN 0 "
                                      "WHEN 'preparing' THEN 1 WHEN 'postprocessing' THEN 2 "
                                      "WHEN 'queued' THEN 3 WHEN 'paused' THEN 4 ELSE 5 END, "
                                      "created_at ASC", limit=limit)
        with self._lock:
            for item_id, live in self._view.items():
                for index, stored in enumerate(items):
                    if stored.id == item_id and live.state_enum.is_active:
                        items[index] = live
        payload = [self._serialize(i) for i in items]
        stats = self.store.stats()
        return {
            "updated": time.time(),
            "stats": stats,
            "items": payload,
            "active": [i for i in payload if i["state"] in
                       ("preparing", "downloading", "postprocessing")],
        }

    @staticmethod
    def _serialize(item: DownloadItem) -> dict[str, Any]:
        return {
            "id": item.id,
            "url": item.url,
            "title": item.title or item.url,
            "state": item.state,
            "backend": item.backend,
            "progress": round(item.progress or 0.0, 4),
            "downloaded": item.downloaded,
            "total": item.total,
            "speed": item.speed,
            "eta": item.eta,
            "stage": item.stage,
            "error": item.error,
            "output_path": item.output_path,
            "extractor": item.extractor,
            "playlist": item.playlist_title,
            "playlist_index": item.playlist_index,
            "duration": item.duration,
            "attempts": item.attempts,
            "meta": item.meta,
        }

    def write_state(self) -> None:
        try:
            payload = self.snapshot()
            target = state_path()
            tmp = target.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(target)
        except OSError:
            pass
