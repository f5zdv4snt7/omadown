"""Smoke tests that need a real TTY / a long-running daemon.

Run directly (not collected by ``unittest discover``, which only picks up
``test*.py``):

    python tests/smoke_pty.py

Covers two things unittest cannot reach: the curses UI rendered into a PTY
(via ``pty.fork`` so the child actually owns a controlling terminal - curses will
silently fail to read keys without one), and the daemon's unix-socket control
channel driven by the real CLI.
"""
import fcntl, os, pty, select, signal, struct, subprocess, sys, tempfile, termios, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
tmp = Path(tempfile.mkdtemp(prefix="omadown-smoke-"))
env = dict(os.environ)
env.update(
    OMADOWN_DATA_HOME=str(tmp / "data"),
    OMADOWN_CONFIG_HOME=str(tmp / "config"),
    XDG_RUNTIME_DIR=str(tmp / "run"),
    PYTHONPATH=str(ROOT),
    TERM="xterm-256color",
)

# ------------------------------------------------------------------ 1. TUI in a pty
print("== TUI smoke ==")
# forkpty() gives the child a *controlling* terminal, which curses needs in order
# to read keys; a plain subprocess with a pty on stdin is not enough.
pid, master = pty.fork()
if pid == 0:
    os.environ.update(env)
    os.chdir(str(ROOT))
    os.execv(PY, [PY, "-m", "omadown", "tui"])

fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 30, 100, 0, 0))

captured = b""


def pump(seconds: float) -> None:
    global captured
    end = time.time() + seconds
    while time.time() < end:
        ready, _, _ = select.select([master], [], [], 0.2)
        if not ready:
            continue
        try:
            chunk = os.read(master, 65536)
        except OSError:
            return
        if not chunk:
            return
        captured += chunk


pump(1.8)

deadline = time.time() + 10
code = None
sent = 0
while time.time() < deadline:
    done, status = os.waitpid(pid, os.WNOHANG)
    if done:
        code = os.waitstatus_to_exitcode(status)
        break
    if not sent:
        try:
            os.write(master, b"q")
        except OSError as exc:
            print(f"  (write to pty failed: {exc})")
        sent = 1
    pump(0.3)
if code is None:
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    code = -9
pump(0.3)
os.close(master)

screen = captured.decode("utf-8", "replace")
print(f"exit={code}  bytes_captured={len(captured)}")
assert "Traceback" not in screen, screen[-2000:]
assert code == 0, f"tui exited with {code}"
assert "omadown" in screen and "\u961f\u5217\u4e3a\u7a7a" in screen, screen[:400]
print("TUI 渲染正常，标题栏与空队列提示都在，q 能正常退出\n")

# ------------------------------------------------------------- 2. daemon + IPC
print("== daemon + IPC smoke ==")
sock = tmp / "run" / "omadown" / "control.sock"
daemon = subprocess.Popen([PY, "-m", "omadown", "daemon"], env=env, cwd=str(ROOT),
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

def wait_for(predicate, timeout=10.0, what=""):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.15)
    raise AssertionError(f"timeout waiting for {what}")

try:
    wait_for(sock.exists, what="control socket")
    print(f"control socket: {sock}")

    def cli(*args, expect=0):
        p = subprocess.run([PY, "-m", "omadown", *args], capture_output=True,
                           text=True, env=env, cwd=str(ROOT), timeout=60)
        print(f"  $ omadown {' '.join(args)}\n    {p.stdout.strip().splitlines()[0] if p.stdout.strip() else ''}")
        assert p.returncode == expect, (p.returncode, p.stdout, p.stderr)
        return p

    # No real download: the URL is deliberately unresolvable, we only care that
    # the CLI reached the daemon instead of opening a second database.
    cli("add", "local://smoke-test", expect=0)
    cli("status", "--json")
    cli("cancel")

    state = tmp / "run" / "omadown" / "state.json"
    wait_for(state.exists, what="state.json")
    print(f"  state.json: {state.stat().st_size} bytes")

    def daemon_alive():
        return daemon.poll() is None
    assert daemon_alive(), "daemon died"
    print("daemon 存活，CLI 通过 unix socket 投递任务成功\n")
finally:
    daemon.send_signal(signal.SIGTERM)
    try:
        out, _ = daemon.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        daemon.kill()
        out, _ = daemon.communicate()
    print("== daemon log ==")
    print("\n".join(out.strip().splitlines()[:6]))
    assert not sock.exists(), "socket should be cleaned up on shutdown"
    print("\nsocket 已清理，退出码", daemon.returncode)

print("\nSMOKE OK")
