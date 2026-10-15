import json
import os
import tempfile
import unittest
from pathlib import Path

from omadown import ipc
from omadown.backends import Backend, BackendRegistry, DownloadContext
from omadown.config import Config
from omadown.db import Store
from omadown.engine import Engine
from omadown.models import MediaInfo


class LocalBackend(Backend):
    name = "local"
    priority = 1

    @classmethod
    def available(cls) -> bool:
        return True

    def claim(self, url: str) -> bool:
        return url.startswith("local://")

    def probe(self, url: str, ctx: DownloadContext) -> MediaInfo:
        return MediaInfo(url=url, title="Local", extractor="local")

    def download(self, ctx: DownloadContext, progress) -> Path:
        target = ctx.workdir / "out.mp4"
        target.write_bytes(b"x")
        return target


class ControlChannelTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        os.environ["OMADOWN_DATA_HOME"] = str(self.tmp / "data")
        os.environ["XDG_RUNTIME_DIR"] = str(self.tmp / "run")
        self.config = Config()
        self.config.general.download_dir = str(self.tmp / "out")
        self.store = Store(self.tmp / "db.sqlite")
        self.engine = Engine(self.config, self.store,
                             registry=BackendRegistry([LocalBackend()]))
        self.server = ipc.ControlServer(self.engine, self.tmp / "control.sock")
        self.server.start()

    def tearDown(self):
        self.server.stop()
        self.store.close()
        self._tmp.cleanup()

    def call(self, command: str, **payload) -> dict:
        reply = ipc.send_command(command, path=self.tmp / "control.sock", **payload)
        self.assertIsNotNone(reply, f"no reply for {command}")
        return reply

    def test_ping(self):
        reply = self.call("ping")
        self.assertTrue(reply["ok"])
        self.assertEqual(reply["result"]["pid"], os.getpid())

    def test_daemon_running(self):
        self.assertTrue(ipc.daemon_running(self.tmp / "control.sock"))

    def test_add_then_status(self):
        reply = self.call("add", urls=["local://a", "local://b"])
        self.assertTrue(reply["ok"], reply)
        self.assertEqual(len(reply["result"]["added"]), 2)

        status = self.call("status")
        self.assertTrue(status["ok"])
        self.assertEqual(status["result"]["stats"]["total"], 2)

    def test_add_is_idempotent_unless_forced(self):
        self.call("add", urls=["local://dup"])
        again = self.call("add", urls=["local://dup"])
        self.assertEqual(len(again["result"]["added"]), 0)
        self.assertEqual(len(again["result"]["skipped"]), 1)
        forced = self.call("add", urls=["local://dup"], force=True)
        self.assertEqual(len(forced["result"]["added"]), 1)

    def test_cancel_and_clear(self):
        self.call("add", urls=["local://x"])
        self.assertTrue(self.call("cancel")["result"]["cancelled"] >= 1)
        self.assertTrue(self.call("clear")["ok"])

    def test_retry_reports_count(self):
        self.assertEqual(self.call("retry")["result"]["retried"], 0)

    def test_unknown_command_is_rejected(self):
        reply = self.call("teleport")
        self.assertFalse(reply["ok"])
        self.assertIn("unknown command", reply["error"])

    def test_malformed_request_does_not_crash_the_server(self):
        import socket
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as raw:
            raw.settimeout(2)
            raw.connect(str(self.tmp / "control.sock"))
            raw.sendall(b"this is not json\n")
            data = raw.recv(4096)
        self.assertFalse(json.loads(data.decode())["ok"])
        # server still alive
        self.assertTrue(self.call("ping")["ok"])

    def test_missing_socket_returns_none(self):
        self.assertIsNone(ipc.send_command("ping", path=self.tmp / "nope.sock"))


if __name__ == "__main__":
    unittest.main()
