"""Waybar / status output.

``waybar`` custom modules read a single line of JSON. Omarchy ships Waybar with
Hyprland, so this is how the queue shows up in the bar.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from .db import Store
from .util import human_bytes, human_duration, human_speed, progress_bar, shorten

ICONS = {
    "downloading": "\u2913",   # download arrow
    "preparing": "\u22ef",
    "postprocessing": "\u2699",
    "queued": "\u2261",
    "paused": "\u23f8",
    "done": "\u2713",
    "failed": "\u2717",
    "cancelled": "\u2298",
}


def _snapshot() -> dict[str, Any]:
    from .util import state_path
    path = state_path()
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("items") is not None:
                return payload
        except (OSError, json.JSONDecodeError):
            pass
    store = Store()
    try:
        items = [_row(i) for i in store.list(limit=50)]
        return {"updated": 0, "stats": store.stats(), "items": items,
                "active": [i for i in items if i["state"] in
                           ("preparing", "downloading", "postprocessing")]}
    finally:
        store.close()


def _row(item) -> dict[str, Any]:
    return {
        "id": item.id, "url": item.url, "title": item.title, "state": item.state,
        "progress": item.progress, "downloaded": item.downloaded, "total": item.total,
        "speed": None, "eta": None, "stage": item.stage, "error": item.error,
    }


def waybar(compact: bool = False) -> dict[str, Any]:
    snapshot = _snapshot()
    active = snapshot.get("active") or []
    stats = snapshot.get("stats", {})
    items = snapshot.get("items", [])

    if active:
        head = active[0]
        count = len(active)
        pct = int(round((head.get("progress") or 0) * 100))
        text = f"{ICONS.get(head['state'], '?')} {pct}%"
        if count > 1:
            text += f" +{count - 1}"
        lines = []
        for item in active[:6]:
            bar = progress_bar(item.get("progress"), width=12)
            speed = human_speed(item.get("speed"))
            eta = human_duration(item.get("eta"))
            lines.append(f"{bar} {int((item.get('progress') or 0) * 100):3d}%  {speed}  ETA {eta}")
            lines.append(f"   {shorten(item.get('title') or item.get('url') or '', 60)}")
        if count > len(active[:6]):
            lines.append(f"... 以及 {count - 6} 个任务")
        queued = stats.get("queued", 0)
        if queued:
            lines.append(f"排队中: {queued}")
        return {
            "text": text,
            "alt": "active",
            "tooltip": "\n".join(lines),
            "class": "downloading" if any(i["state"] == "downloading" for i in active) else "active",
            "percentage": pct,
        }

    failed = stats.get("failed", 0)
    if failed:
        return {
            "text": f"{ICONS['failed']} {failed}",
            "alt": "failed",
            "tooltip": f"{failed} 个下载失败\n点击查看详情",
            "class": "failed",
            "percentage": 0,
        }

    total_done = stats.get("done", 0)
    size = human_bytes(stats.get("bytes"))
    if compact:
        return {"text": "", "alt": "idle", "tooltip": "", "class": "idle", "percentage": 0}
    return {
        "text": "",
        "alt": "idle",
        "tooltip": f"omadown 空闲\n已完成 {total_done} 个 · 共 {size}",
        "class": "idle",
        "percentage": 0,
    }


def plain(limit: int = 15, show_all: bool = False) -> str:
    snapshot = _snapshot()
    lines: list[str] = []
    items = snapshot.get("items") or []
    if not show_all:
        items = [i for i in items if i["state"] not in ("done", "cancelled")] or items[:limit]
    for item in items[:limit]:
        state = item["state"]
        icon = ICONS.get(state, "?")
        pct = int(round((item.get("progress") or 0) * 100))
        bar = progress_bar(item.get("progress"), width=18)
        title = shorten(item.get("title") or item.get("url") or "", 52)
        tail = ""
        if state == "downloading":
            tail = f"{human_speed(item.get('speed'))}  {human_duration(item.get('eta'))}"
        elif state == "failed":
            tail = shorten(item.get("error") or "", 40)
        elif state == "done":
            tail = item.get("output_path") or ""
        lines.append(f"{icon} {bar} {pct:3d}%  {title:<52}  {tail}".rstrip())
    stats = snapshot.get("stats", {})
    if not lines:
        lines.append("队列为空。添加任务: omadown add <URL>")
    summary = (
        f"共 {stats.get('total', 0)} · 进行中 {stats.get('active', 0)} · "
        f"排队 {stats.get('queued', 0)} · 完成 {stats.get('done', 0)} · "
        f"失败 {stats.get('failed', 0)} · {human_bytes(stats.get('bytes'))}"
    )
    return "\n".join(lines + ["", summary])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omadown-status", description="omadown 状态输出")
    parser.add_argument("--waybar", action="store_true", help="输出 Waybar 模块 JSON")
    parser.add_argument("--compact", action="store_true", help="空闲时不显示任何内容")
    parser.add_argument("--json", action="store_true", help="输出原始 JSON 快照")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--all", action="store_true", help="包含已完成任务")
    args = parser.parse_args(argv)

    if args.waybar:
        print(json.dumps(waybar(args.compact), ensure_ascii=False))
    elif args.json:
        print(json.dumps(_snapshot(), ensure_ascii=False, indent=2))
    else:
        print(plain(args.limit, args.all))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
