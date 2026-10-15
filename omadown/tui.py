"""Terminal UI for the queue - built on stdlib ``curses`` so it has zero deps.

Omarchy is a keyboard-first Hyprland setup, so this is the primary face of the
app: vim-style navigation, instant actions, and a layout that fits a floating
terminal window bound to Super+D.
"""

from __future__ import annotations

import curses
import locale
import time
from typing import Any

from .config import Config
from .engine import Engine
from .models import DownloadOptions, State
from .util import (display_width, fit, human_bytes, human_duration, human_speed, pad,
                   progress_bar, shorten_wide)

STATE_STYLE = {
    "downloading": ("downloading", 1),
    "preparing": ("preparing", 2),
    "postprocessing": ("post-processing", 2),
    "queued": ("queued", 0),
    "paused": ("paused", 0),
    "done": ("done", 3),
    "failed": ("failed", 4),
    "cancelled": ("cancelled", 0),
}

STATE_ICON = {
    "downloading": "\u2913",
    "preparing": "\u22ef",
    "postprocessing": "\u2699",
    "queued": "\u2261",
    "paused": "\u23f8",
    "done": "\u2713",
    "failed": "\u2717",
    "cancelled": "\u2298",
}

HELP = (
    " j/k 移动   a 添加   p 暂停/继续   x 取消   r 重试   d 删除   "
    "c 清理已完成   o 打开文件   O 打开目录   g/G 首/尾   q 退出 "
)


class QueueTUI:
    def __init__(self, engine: Engine, config: Config, *, auto_start: bool = True) -> None:
        self.engine = engine
        self.config = config
        self.auto_start = auto_start
        self.cursor = 0
        self.offset = 0
        self.message = ""
        self.message_until = 0.0
        self.items: list[dict[str, Any]] = []
        self._running = True
        self._worker: Any = None

    # -------------------------------------------------------------------- plumbing
    def _flash(self, text: str, seconds: float = 3.0) -> None:
        self.message = text
        self.message_until = time.monotonic() + seconds

    def _current(self) -> dict[str, Any] | None:
        if not self.items:
            return None
        self.cursor = max(0, min(self.cursor, len(self.items) - 1))
        return self.items[self.cursor]

    # ------------------------------------------------------------------------ run
    def run(self) -> None:
        locale.setlocale(locale.LC_ALL, "")
        if self.auto_start:
            import threading
            self._worker = threading.Thread(
                target=self.engine.run, name="omadown-worker", daemon=True,
            )
            self._worker.start()
        curses.wrapper(self._loop)

    def _init_colors(self) -> None:
        if not curses.has_colors():
            return
        curses.start_color()
        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK
        curses.init_pair(1, curses.COLOR_CYAN, bg)
        curses.init_pair(2, curses.COLOR_YELLOW, bg)
        curses.init_pair(3, curses.COLOR_GREEN, bg)
        curses.init_pair(4, curses.COLOR_RED, bg)
        curses.init_pair(5, curses.COLOR_WHITE, bg)

    def _loop(self, screen) -> None:
        curses.curs_set(0)
        screen.nodelay(True)
        screen.timeout(self.config.tui.refresh_ms)
        self._init_colors()
        while self._running:
            self.items = self._visible_items()
            self._draw(screen)
            key = screen.getch()
            if key != -1:
                self._handle_key(screen, key)
        if self._worker is not None:
            self.engine.shutdown()

    def _visible_items(self) -> list[dict[str, Any]]:
        snapshot = self.engine.snapshot()
        items = snapshot.get("items", [])
        return [i for i in items if i["state"] not in ("cancelled",)] or items

    # --------------------------------------------------------------------- render
    @staticmethod
    def _put(screen, row: int, text: str, width: int, attribute: int = 0) -> None:
        """Write one padded line, clipped to the terminal in *display cells*.

        Curses refuses to draw over the bottom-right cell, so we always stop one
        column short of the edge.
        """
        available = max(0, width - 1)
        if available == 0:
            return
        line = pad(text, available)
        for attempt in (line, line[:-1] if line else ""):
            try:
                screen.addstr(row, 0, attempt, attribute)
                return
            except curses.error:
                continue

    def _draw(self, screen) -> None:
        screen.erase()
        height, width = screen.getmaxyx()
        stats = self.engine.store.stats()

        self._put(screen, 0, " omadown \u00b7 Downie for Omarchy ",
                  width, curses.A_REVERSE | curses.A_BOLD)

        summary = (
            f" 进行中 {stats.get('active', 0)} \u00b7 排队 {stats.get('queued', 0)} \u00b7 "
            f"完成 {stats.get('done', 0)} \u00b7 失败 {stats.get('failed', 0)} \u00b7 "
            f"{human_bytes(stats.get('bytes'))} "
        )
        self._put(screen, 1, summary, width, curses.color_pair(5) if curses.has_colors() else 0)

        # list area
        list_top = 2
        list_height = max(1, height - list_top - 3)
        self._clamp_scroll(list_height)

        if not self.items:
            hint = "  队列为空 \u2014 按 a 添加链接，或直接复制链接（守护进程会捕获）"
            self._put(screen, list_top + 1, hint, width)
        else:
            for row, item in enumerate(self.items[self.offset:self.offset + list_height]):
                line_index = list_top + row
                selected = (self.offset + row) == self.cursor
                self._draw_row(screen, line_index, width, item, selected)

        # footer
        if self.message and time.monotonic() < self.message_until:
            self._put(screen, height - 2, " " + self.message, width,
                      curses.color_pair(2) if curses.has_colors() else 0)
        else:
            self._put(screen, height - 2, HELP, width, curses.A_DIM)
            keyhint = " a 添加  空格 暂停/继续  x 取消   r 重试   ? 帮助 "
            self._put(screen, height - 1, keyhint, width,
                      curses.A_REVERSE if not self.items else curses.A_DIM)
        screen.refresh()

    def _draw_row(self, screen, line: int, width: int, item: dict[str, Any], selected: bool) -> None:
        state = item["state"]
        label, pair = STATE_STYLE.get(state, (state, 0))
        icon = STATE_ICON.get(state, "?")
        pct = int(round((item.get("progress") or 0) * 100))

        bar_width = max(8, min(24, width // 4))
        bar = progress_bar(item.get("progress"), width=bar_width)

        reserved = bar_width + 12
        title_width = max(12, width - reserved)
        title = shorten_wide(item.get("title") or item.get("url") or "", title_width)

        tail = ""
        if state == "downloading":
            tail = f"{human_speed(item.get('speed'))}  {human_duration(item.get('eta'))}"
        elif state == "postprocessing":
            tail = shorten_wide(item.get("stage") or "", 24)
        elif state == "failed":
            tail = shorten_wide(item.get("error") or "", 32)
        elif state == "done":
            tail = shorten_wide(item.get("output_path") or "", 40)
        elif state == "queued":
            tail = shorten_wide(item.get("extractor") or "", 16)

        text = f" {icon} {bar} {pct:3d}%  {title}  {tail}"
        attribute = curses.A_REVERSE if selected else 0
        if pair and curses.has_colors():
            attribute |= curses.color_pair(pair)
        self._put(screen, line, text, width, attribute)

    def _clamp_scroll(self, list_height: int) -> None:
        if self.cursor < self.offset:
            self.offset = self.cursor
        elif self.cursor >= self.offset + list_height:
            self.offset = self.cursor - list_height + 1
        self.offset = max(0, min(self.offset, max(0, len(self.items) - list_height)))

    # ----------------------------------------------------------------- actions
    def _handle_key(self, screen, key: int) -> None:
        current = self._current()
        if key in (ord("q"), 27):
            self._running = False
            return
        if key in (ord("j"), curses.KEY_DOWN):
            self.cursor += 1
        elif key in (ord("k"), curses.KEY_UP):
            self.cursor -= 1
        elif key == ord("g"):
            self.cursor = 0
        elif key == ord("G"):
            self.cursor = max(0, len(self.items) - 1)
        elif key == ord("a"):
            self._prompt_add(screen)
        elif key == ord(" ") and current:
            self._toggle(current)
        elif key == ord("x") and current:
            self.engine.cancel(int(current["id"]))
            self._flash("已取消")
        elif key == ord("r"):
            count = self.engine.retry([int(current["id"])] if current else None)
            self._flash(f"已重新排队 {count} 个任务")
        elif key == ord("d") and current:
            item_id = int(current["id"])
            if current["state"] in ("downloading", "preparing", "postprocessing"):
                self.engine.cancel(item_id)
            self.engine.remove([item_id])
            self._flash("已删除")
        elif key == ord("c"):
            self._flash(f"已清理 {self.engine.clear_completed()} 条记录")
        elif key == ord("o") and current and current.get("output_path"):
            self._open(current["output_path"])
        elif key == ord("O"):
            self._open(str(self.config.output_dir))
        elif key == ord("?"):
            self._flash("a 添加 · 空格 暂停/继续 · x 取消 · r 重试 · d 删除 · c 清理 · o 打开", 8.0)

    def _toggle(self, item: dict[str, Any]) -> None:
        item_id = int(item["id"])
        if item["state"] == "paused":
            self.engine.resume(item_id)
            self._flash("已继续")
        else:
            if not self.engine.pause(item_id):
                self.engine.cancel(item_id)
                self._flash("正在进行的任务已取消")
            else:
                self._flash("已暂停")

    def _open(self, target: str) -> None:
        import subprocess
        from .util import which
        opener = which("xdg-open") or which("open")
        if opener:
            subprocess.Popen([opener, target], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)

    def _prompt_add(self, screen) -> None:
        height, width = screen.getmaxyx()
        curses.curs_set(1)
        screen.nodelay(False)
        prompt = " 链接（可多个，空格分隔）: "
        offset = display_width(prompt)
        # NOTE: never use screen.getstr() here. Its 3rd argument caps the
        # bytes read and can never exceed the terminal width, so pasted URLs
        # longer than the line were silently truncated (a 50-char m3u8 link
        # entered as "https://iuewgnbvk.c" -> "无法解析"). Read keys manually;
        # clip only the *display*, never the buffer.
        buf: list[str] = []
        max_chars = 4096
        while True:
            text_all = "".join(buf)
            room = max(1, width - offset - 1)
            visible = text_all[-room:] if len(text_all) > room else text_all
            self._put(screen, height - 1, prompt + visible, width, curses.A_REVERSE)
            try:
                ch = screen.get_wch()
            except curses.error:
                break
            if ch in ("\n", "\r"):
                break
            if ch == "\x1b":  # ESC cancels
                buf = []
                break
            if ch in ("\b", "\x7f") or (isinstance(ch, int) and ch == curses.KEY_BACKSPACE):
                if buf:
                    buf.pop()
            elif isinstance(ch, int) and ch == curses.KEY_RESIZE:
                height, width = screen.getmaxyx()
            elif isinstance(ch, str) and len(buf) < max_chars and ch.isprintable():
                buf.append(ch)
        curses.curs_set(0)
        screen.nodelay(True)
        text = "".join(buf)
        if not text.strip():
            return
        result = self.engine.submit_text(text, DownloadOptions())
        parts = [f"已加入 {len(result.added)} 个任务"]
        if result.skipped:
            parts.append(f"跳过 {len(result.skipped)} 个重复")
        self._flash("，".join(parts))


def run(engine: Engine, config: Config, *, auto_start: bool = True) -> None:
    QueueTUI(engine, config, auto_start=auto_start).run()
