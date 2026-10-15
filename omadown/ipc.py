"""Tiny Unix-socket control channel.

Lets ``omadown add <url>`` talk to a running daemon (started by the systemd user
unit or ``omadown daemon``), which is what makes the clipboard watcher / Waybar /
Hyprland keybind workflow feel instant instead of spawning a second process that
fights over the SQLite file.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

from .util import control_socket_path

MAX_MESSAGE = 1 << 20


class ControlServer:
    def __init__(self, engine, path: Path | None = None) -> None:
        self.engine = engine
        self.path = Path(path) if path else control_socket_path()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = threading.Event()
        self.handlers: dict[str, Callable[[dict], Any]] = {}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            if self._probe_existing():
                raise RuntimeError(f"another omadown daemon already listens on {self.path}")
            self.path.unlink(missing_ok=True)

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(str(self.path))
        os.chmod(self.path, 0o600)
        self._sock.listen(16)
        self._running.set()
        self._thread = threading.Thread(target=self._serve, name="omadown-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running.clear()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self.path.unlink(missing_ok=True)

    def _probe_existing(self) -> bool:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.4)
                probe.connect(str(self.path))
                probe.sendall(b'{"cmd": "ping"}\n')
                return bool(probe.recv(1024))
        except OSError:
            return False

    # --------------------------------------------------------------------- serving
    def _serve(self) -> None:
        assert self._sock is not None
        while self._running.is_set():
            try:
                conn, _ = self._sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            buffer = b""
            conn.settimeout(30)
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                if len(buffer) > MAX_MESSAGE:
                    return
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    response = self._dispatch(line)
                    try:
                        conn.sendall(json.dumps(response, ensure_ascii=False).encode() + b"\n")
                    except OSError:
                        return

    def _dispatch(self, raw: bytes) -> dict:
        try:
            request = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            return {"ok": False, "error": f"bad request: {exc}"}
        command = request.get("cmd", "")
        try:
            if command in self.handlers:
                return {"ok": True, "result": self.handlers[command](request)}
            return self._default(command, request)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}

    def _default(self, command: str, request: dict) -> dict:
        from .models import DownloadOptions

        engine = self.engine
        if command == "ping":
            return {"ok": True, "result": {"pong": True, "pid": os.getpid()}}
        if command == "status":
            return {"ok": True, "result": engine.snapshot()}
        if command == "add":
            options = DownloadOptions.from_dict(request.get("options"))
            result = engine.submit(
                request.get("urls") or [],
                options,
                allow_duplicates=bool(request.get("force")),
            )
            engine.write_state()
            return {"ok": True, "result": {
                "added": [{"id": i.id, "url": i.url} for i in result.added],
                "skipped": result.skipped,
            }}
        if command == "retry":
            return {"ok": True, "result": {"retried": engine.retry(request.get("ids"))}}
        if command == "cancel":
            ids = request.get("ids") or []
            if not ids:
                return {"ok": True, "result": {"cancelled": engine.cancel_all()}}
            return {"ok": True, "result": {
                "cancelled": sum(1 for i in ids if engine.cancel(int(i)))
            }}
        if command == "remove":
            return {"ok": True, "result": {"removed": engine.remove(request.get("ids") or [])}}
        if command == "pause":
            return {"ok": True, "result": {
                "paused": sum(1 for i in request.get("ids") or [] if engine.pause(int(i)))
            }}
        if command == "resume":
            return {"ok": True, "result": {
                "resumed": sum(1 for i in request.get("ids") or [] if engine.resume(int(i)))
            }}
        if command == "clear":
            return {"ok": True, "result": {"cleared": engine.clear_completed()}}
        if command == "shutdown":
            engine.shutdown()
            return {"ok": True, "result": {"shutting_down": True}}
        return {"ok": False, "error": f"unknown command: {command}"}


# ------------------------------------------------------------------------ client

def send_command(command: str, timeout: float = 5.0, path: Path | None = None, **payload) -> dict | None:
    """Return the daemon's reply, or ``None`` when no daemon is reachable."""
    target = Path(path) if path else control_socket_path()
    if not target.exists():
        return None
    request = {"cmd": command, **payload}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(target))
            client.sendall(json.dumps(request, ensure_ascii=False).encode() + b"\n")
            buffer = b""
            while b"\n" not in buffer:
                chunk = client.recv(65536)
                if not chunk:
                    break
                buffer += chunk
            if not buffer:
                return None
            return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def daemon_running(path: Path | None = None) -> bool:
    reply = send_command("ping", timeout=1.0, path=path)
    return bool(reply and reply.get("ok"))
