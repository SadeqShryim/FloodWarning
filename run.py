"""Start FloodLine for a demo: build the frontend if needed, run the server, open a public tunnel.

    .venv\\Scripts\\python.exe run.py            # server on :8000 + https tunnel for phones
    .venv\\Scripts\\python.exe run.py --open     # ...and open the dashboard in the browser
    .venv\\Scripts\\python.exe run.py --no-tunnel --port 8106

Two child processes: uvicorn (the whole app: API + built frontend) and cloudflared (a free
"quick tunnel" that gives phones an https:// address; browsers only allow the microphone on
https). Ctrl+C stops both. If cloudflared is missing or fails, the app still runs locally.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
BACKEND_DIR = REPO_ROOT / "backend"
FRONTEND_DIR = REPO_ROOT / "frontend"
FRONTEND_DIST = FRONTEND_DIR / "dist"
CLOUDFLARED = REPO_ROOT / "tools" / "cloudflared.exe"

HEALTH_TIMEOUT_S = 60  # first start under x64 emulation on ARM can take a while (imports + seeding)
TUNNEL_URL_TIMEOUT_S = 40  # cloudflared usually prints its URL in 3-5 s
STOP_GRACE_S = 8  # uvicorn gets --timeout-graceful-shutdown 3, so this is plenty

# Quick tunnels print "https://<random-words>.trycloudflare.com" inside a box. The same domain also
# appears in error lines as the API endpoint ("https://api.trycloudflare.com/tunnel"), so skip that.
_TUNNEL_URL_RE = re.compile(r"https://([a-z0-9-]+)\.trycloudflare\.com\b")

# Windows: start children in their own process group. Then a Ctrl+C in this console reaches only
# run.py, which shuts the children down in order (instead of all three racing to exit at once).
_NEW_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

# Our console may not be UTF-8 (cp1252 on many Windows setups) and the server logs Arabic text.
# Replace what the console cannot show instead of crashing on it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

_print_lock = threading.Lock()


def say(message: str = "") -> None:
    with _print_lock:
        print(message, flush=True)


def parse_tunnel_url(line: str) -> str | None:
    """Return the public https URL if this cloudflared output line announces it, else None."""
    for match in _TUNNEL_URL_RE.finditer(line):
        if match.group(1) != "api":
            return match.group(0)
    return None


# ---------------------------------------------------------------------------------------------
# Small helpers


def data_dir() -> Path:
    """The backend's data directory, resolved exactly the way the backend does it (env + .env)."""
    sys.path.insert(0, str(BACKEND_DIR))
    try:
        from app import config  # noqa: PLC0415  (imported late: only needs python-dotenv)
    finally:
        sys.path.pop(0)
    return Path(config.DATA_DIR)


def python_for_child() -> tuple[str, dict[str, str]]:
    """Interpreter + environment for the server process.

    On Windows, .venv\\Scripts\\python.exe is a small redirector that starts the real interpreter
    as a *second* process. Killing the redirector can leave the real server running and holding
    the port. So, like CPython's own multiprocessing module, start the base interpreter directly
    and point it at the venv with __PYVENV_LAUNCHER__: one process, which we can stop for sure.
    """
    env = dict(os.environ)
    base = getattr(sys, "_base_executable", sys.executable)
    if os.name == "nt" and sys.prefix != sys.base_prefix and base and Path(base).exists():
        env["__PYVENV_LAUNCHER__"] = sys.executable
        return base, env
    return sys.executable, env


class _ChildJob:
    """Windows Job Object with "kill on close": if run.py dies in any way (even killed from Task
    Manager), Windows ends the server and cloudflared too, so no orphan keeps the port busy."""

    def __init__(self) -> None:
        self.handle = None
        if os.name != "nt":
            return
        try:
            import ctypes
            from ctypes import wintypes

            class IoCounters(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BasicLimits(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class ExtendedLimits(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            job = kernel32.CreateJobObjectW(None, None)
            limits = ExtendedLimits()
            limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if job and kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                self.handle = job  # never closed on purpose: Windows closes it when run.py exits
                self._assign = kernel32.AssignProcessToJobObject
        except (OSError, AttributeError):
            self.handle = None  # best effort; the normal shutdown path still stops the children

    def add(self, proc: subprocess.Popen) -> None:
        if self.handle is not None:
            self._assign(self.handle, int(proc._handle))  # type: ignore[attr-defined]


_job = _ChildJob()


def enable_ctrl_c() -> None:
    """Make sure Ctrl+C reaches us. A process started with Ctrl+C disabled (some IDEs and task
    runners do this) passes that on to its children; then Ctrl+C would do nothing at all."""
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(None, False)
        except (OSError, AttributeError):
            pass


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def lan_ip() -> str | None:
    """This laptop's address on the local network (no packet is sent; UDP connect only picks a route)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


# Talk to our own server without going through any system proxy.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url: str, payload: dict | None = None, timeout: float = 3.0) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="GET" if data is None else "POST")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with _opener.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def pump_output(proc: subprocess.Popen, on_line) -> threading.Thread:
    """Read a child's merged stdout/stderr line by line on a background thread."""

    def run() -> None:
        assert proc.stdout is not None
        for raw in proc.stdout:
            on_line(raw.decode("utf-8", errors="replace").rstrip("\r\n"))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def stop_process(proc: subprocess.Popen | None, name: str, graceful: bool) -> None:
    """Stop a child: Ctrl+Break first if asked (uvicorn treats it like Ctrl+C), then terminate."""
    if proc is None or proc.poll() is not None:
        return
    if graceful and os.name == "nt":
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        except OSError:
            pass  # no shared console (e.g. started from a service); fall through to terminate
    elif graceful:
        proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=STOP_GRACE_S if graceful else 0.1)
        return
    except subprocess.TimeoutExpired:
        pass
    proc.terminate()  # TerminateProcess on Windows
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        say(f"[run] {name} did not exit; killing it")
        proc.kill()


# ---------------------------------------------------------------------------------------------
# Steps


def build_frontend(force: bool) -> None:
    if FRONTEND_DIST.joinpath("index.html").exists() and not force:
        return
    npm = shutil.which("npm")
    if npm is None:
        fail_or_warn_build("npm was not found on PATH (install Node.js 20+).")
        return
    if not (FRONTEND_DIR / "node_modules").exists():
        say("[run] Installing frontend dependencies (npm install)...")
        if subprocess.call([npm, "install"], cwd=FRONTEND_DIR) != 0:
            fail_or_warn_build("npm install failed.")
            return
    say("[run] Building the frontend (npm run build)...")
    if subprocess.call([npm, "run", "build"], cwd=FRONTEND_DIR) != 0:
        fail_or_warn_build("npm run build failed (see the output above).")


def fail_or_warn_build(reason: str) -> None:
    # An older build is better than nothing in a demo; with no build at all, stop here.
    if FRONTEND_DIST.joinpath("index.html").exists():
        say(f"[run] WARNING: {reason} Serving the previous build from frontend/dist.")
        return
    say(f"[run] ERROR: {reason} There is no frontend build to serve.")
    sys.exit(1)


def reset_data(path: Path) -> None:
    if not path.exists():
        return
    # Guard against a FLOODLINE_DATA_DIR that points somewhere unexpected: only delete a folder
    # inside this repo, or one that clearly holds FloodLine data.
    inside_repo = REPO_ROOT in path.parents
    if not (inside_repo or (path / "floodline.db").exists()):
        say(f"[run] ERROR: --reset refused: {path} does not look like a FloodLine data folder.")
        sys.exit(1)
    shutil.rmtree(path)
    say(f"[run] Deleted {path} (fresh database and demo reports on start).")


def start_backend(port: int) -> subprocess.Popen:
    python, env = python_for_child()
    env["PYTHONUNBUFFERED"] = "1"  # log lines show up immediately, not in 4 KB chunks
    env["PYTHONIOENCODING"] = "utf-8"  # Arabic in logs must not crash the server's print/logging
    cmd = [
        python, "-m", "uvicorn", "app.main:app",
        "--app-dir", "backend",
        "--host", "0.0.0.0",
        "--port", str(port),
        "--timeout-graceful-shutdown", "3",  # SSE streams would otherwise hold shutdown open
    ]
    proc = subprocess.Popen(
        cmd, cwd=REPO_ROOT, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        creationflags=_NEW_GROUP,
    )
    _job.add(proc)
    pump_output(proc, lambda line: say(f"[server] {line}"))
    return proc


def wait_for_health(proc: subprocess.Popen, base: str) -> dict:
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            time.sleep(0.3)  # let the reader thread print the traceback first
            raise SystemExit(
                f"[run] ERROR: the backend exited during startup (exit code {code}). "
                "See the [server] lines above for the reason."
            )
        try:
            health = http_json(f"{base}/api/health", timeout=2)
            if health.get("ok"):
                return health
        except (urllib.error.URLError, OSError, ValueError):
            pass
        time.sleep(0.4)
    raise SystemExit(f"[run] ERROR: the backend did not answer {base}/api/health within {HEALTH_TIMEOUT_S} s.")


def tunnel_command(exe: Path, port: int) -> list[str]:
    # 127.0.0.1 rather than "localhost": on Windows localhost may resolve to ::1 first,
    # and uvicorn listens on IPv4 only.
    return [str(exe), "tunnel", "--url", f"http://127.0.0.1:{port}", "--no-autoupdate"]


# cloudflared logs an ERR line every time a browser closes a request early. The dashboard does that
# on purpose through a quick tunnel (they do not carry Server-Sent Events, so it drops the stalled
# stream and polls), which would fill the presenter's console with scary-looking noise.
_BENIGN_TUNNEL_ERRORS = ("canceled by remote", "context canceled", "Incoming request ended abruptly")


def is_tunnel_problem(line: str) -> bool:
    """True for cloudflared output worth showing: real errors, not clients hanging up."""
    return " ERR " in line and not any(text in line for text in _BENIGN_TUNNEL_ERRORS)


class Tunnel:
    """cloudflared quick tunnel: start it, find the public URL in its output, keep a short log."""

    def __init__(self, cmd: list[str]) -> None:
        self.url: str | None = None
        self.found = threading.Event()
        self.recent: collections.deque[str] = collections.deque(maxlen=25)
        self.proc = subprocess.Popen(
            cmd,
            cwd=REPO_ROOT,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_NEW_GROUP,
        )
        _job.add(self.proc)
        pump_output(self.proc, self._on_line)

    def _on_line(self, line: str) -> None:
        self.recent.append(line)
        if self.url is None:
            url = parse_tunnel_url(line)
            if url:
                self.url = url
                self.found.set()
                return
        # cloudflared is chatty; only surface its real errors (e.g. the connection dropped).
        if is_tunnel_problem(line):
            say(f"[tunnel] {line}")

    def wait_for_url(self, timeout: float) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.found.wait(0.25):
                return self.url
            if self.proc.poll() is not None:
                return None
        return None


# Public DNS-over-HTTPS resolvers, asked in turn. Measured on 2026-10-04: a new quick-tunnel name
# showed up on Google's in ~2.5 s and on Cloudflare's in 6-8 s, but one Cloudflare resolver node
# kept answering "no such host" for over a minute (it had cached the early miss), so one is not enough.
_DOH_ENDPOINTS = ("https://dns.google/resolve", "https://cloudflare-dns.com/dns-query")


def doh_has_a_record(endpoint: str, host: str) -> bool:
    """Ask one DoH resolver whether `host` has an A record. Raises on network errors."""
    request = urllib.request.Request(f"{endpoint}?name={host}&type=A", headers={"accept": "application/dns-json"})
    with urllib.request.urlopen(request, timeout=4) as response:
        answer = json.loads(response.read().decode("utf-8")).get("Answer") or []
    return any(record.get("type") == 1 for record in answer)


def wait_for_public_dns(public_url: str, timeout: float = 45) -> bool:
    """Wait until the tunnel's hostname resolves on public DNS. True when it does.

    A new quick-tunnel name takes a few seconds to exist in DNS. Resolvers that are asked before
    that cache "no such host" for about a minute (the zone's negative TTL is 60 s). On venue Wi-Fi
    every phone shares one resolver, so if this laptop asked it too early, judges' phones would fail
    too. So we ask public DNS-over-HTTPS resolvers instead, which touch no local cache, and only
    publish the URL (the QR code) once one of them answers. If DoH is blocked, use a fixed wait.
    """
    host = public_url.split("://", 1)[1]
    started = time.monotonic()
    doh_works = False
    while time.monotonic() - started < timeout:
        for endpoint in _DOH_ENDPOINTS:
            try:
                found = doh_has_a_record(endpoint, host)
            except (urllib.error.URLError, OSError, ValueError):
                continue
            doh_works = True
            if found:
                time.sleep(2)  # small margin for the other DNS locations
                return True
        if not doh_works and time.monotonic() - started > 8:
            break  # DoH unreachable on this network; use the fixed wait below
        time.sleep(1.5)
    remaining = 20 - (time.monotonic() - started)
    if remaining > 0:
        time.sleep(remaining)
    return False


def check_tunnel_reachable(public_url: str) -> None:
    """Confirm phones can reach us through the tunnel, on a background thread (after DNS is live)."""

    def run() -> None:
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                if http_json(f"{public_url}/api/health", timeout=8).get("ok"):
                    say(f"[tunnel] Verified: {public_url} answers from the internet.")
                    return
            except (urllib.error.URLError, OSError, ValueError):
                pass
            time.sleep(3)
        say(
            "[tunnel] WARNING: could not reach the tunnel URL from this laptop yet. Try it on a phone; "
            "if this laptop's browser says the site cannot be found, run `ipconfig /flushdns`."
        )

    threading.Thread(target=run, daemon=True).start()


def set_public_url(base: str, public_url: str | None) -> bool:
    """Tell the server which address phones use. The dashboard's QR code follows it live."""
    try:
        http_json(f"{base}/api/config/public-url", {"public_url": public_url})
        return True
    except (urllib.error.URLError, OSError, ValueError) as exc:
        if public_url:
            say(f"[run] WARNING: could not tell the server its public URL ({exc}). "
                "Paste it into the dashboard's QR panel instead.")
        return False


class TunnelKeeper:
    """Keeps a quick tunnel up for the whole demo.

    If cloudflared exits (a network change, a crash, someone closed it), phones lose the server.
    Instead of leaving the QR code dead until someone notices, open a new tunnel. A quick tunnel's
    address cannot be kept, so the new one has a new URL: the server gets it, and the dashboard's
    QR code updates by itself. Failed attempts are retried with a growing pause.
    """

    RETRY_DELAYS_S = (10, 30, 60)

    def __init__(self, cmd: list[str], base: str) -> None:
        self.cmd = cmd
        self.base = base
        self.tunnel: Tunnel | None = None
        self.url: str | None = None
        self.failures = 0
        self.next_try: float | None = None  # monotonic time of the next attempt, None = not needed

    def open(self) -> str | None:
        """Start cloudflared and publish its URL once it works. Blocks up to about a minute."""
        say("[run] Opening a public https tunnel (cloudflared quick tunnel)...")
        # Assigned before any waiting, so a Ctrl+C in the middle still stops this cloudflared.
        self.tunnel = Tunnel(self.cmd)
        url = self.tunnel.wait_for_url(TUNNEL_URL_TIMEOUT_S)
        if url is None:
            say("[run] WARNING: cloudflared did not give a public URL. Its last lines:")
            for line in self.tunnel.recent:
                say(f"    {line}")
            stop_process(self.tunnel.proc, "cloudflared", graceful=False)
            self.tunnel = None
            self._schedule_retry()
            return None
        say(f"[run] Tunnel {url} created; waiting for its address to go live in DNS...")
        if not wait_for_public_dns(url):
            say("[run] (could not confirm DNS through cloudflare-dns.com; continuing anyway)")
        set_public_url(self.base, url)
        check_tunnel_reachable(url)
        self.url = url
        self.failures = 0
        self.next_try = None
        return url

    def _schedule_retry(self) -> None:
        delay = self.RETRY_DELAYS_S[min(self.failures, len(self.RETRY_DELAYS_S) - 1)]
        self.failures += 1
        self.next_try = time.monotonic() + delay
        say(f"[run] Will try to open a tunnel again in {delay} s (the local dashboard keeps working).")

    def tick(self) -> None:
        """Called from the main loop: notice a dead tunnel and replace it."""
        if self.tunnel is not None and self.tunnel.proc.poll() is not None:
            say("[run] WARNING: cloudflared exited; phones cannot reach the server. Its last lines:")
            for line in list(self.tunnel.recent)[-5:]:
                say(f"    {line}")
            self.tunnel = None
            self.url = None
            # Take the dead link off the dashboard's QR code right away (it then shows a warning).
            set_public_url(self.base, None)
            self.next_try = time.monotonic()  # replace it now
        if self.tunnel is None and self.next_try is not None and time.monotonic() >= self.next_try:
            url = self.open()
            if url:
                say("")
                say("=" * 72)
                say(f"  NEW phone link (the dashboard QR code has updated): {url}/report")
                say("  Phones that had the old page open must scan the QR code again.")
                say("=" * 72)
                say("")

    def stop(self) -> None:
        if self.tunnel is not None:
            stop_process(self.tunnel.proc, "cloudflared", graceful=False)
            self.tunnel = None


def banner(port: int, public_url: str | None, health: dict, tunnel_note: str | None) -> None:
    local = f"http://localhost:{port}"
    lines = [
        "",
        "=" * 72,
        "  FloodLine is running",
        "",
        f"  Dashboard (this laptop):  {local}/dashboard",
    ]
    if public_url:
        lines += [
            f"  Phones (scan the QR):     {public_url}/report",
            f"  Public dashboard:         {public_url}/dashboard",
        ]
    else:
        lines.append(f"  Phones:                   no tunnel ({tunnel_note})")
        ip = lan_ip()
        if ip:
            lines.append(f"  Same Wi-Fi (typed only):  http://{ip}:{port}/report   (voice needs https)")
    ai = f"Gemini ({health.get('ai_model')})" if health.get("ai_enabled") else "off: no GEMINI_API_KEY, keyword fallback"
    lines += [
        "",
        f"  Reports loaded: {health.get('reports', '?')}    AI: {ai}",
        "  Press Ctrl+C to stop.",
        "=" * 72,
        "",
    ]
    say("\n".join(lines))


# ---------------------------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the FloodLine demo (server + public tunnel).")
    parser.add_argument("--port", type=int, default=8000, help="local port (default 8000)")
    parser.add_argument("--no-tunnel", action="store_true", help="local only, no cloudflared")
    parser.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    parser.add_argument("--reset", action="store_true", help="delete the data folder first (fresh demo data)")
    parser.add_argument("--build", action="store_true", help="rebuild the frontend even if frontend/dist exists")
    parser.add_argument("--skip-build", action="store_true", help="never build the frontend (API only if no build)")
    args = parser.parse_args()

    # Ctrl+Break should stop things exactly like Ctrl+C.
    enable_ctrl_c()
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)

    if port_in_use(args.port):
        say(f"[run] ERROR: port {args.port} is already in use. Stop the other server or pass --port.")
        return 1

    if args.reset:
        reset_data(data_dir())
    if not args.skip_build:
        build_frontend(force=args.build)

    base = f"http://127.0.0.1:{args.port}"
    backend: subprocess.Popen | None = None
    keeper: TunnelKeeper | None = None
    try:
        say(f"[run] Starting the server on port {args.port}...")
        backend = start_backend(args.port)
        health = wait_for_health(backend, base)

        public_url: str | None = None
        tunnel_note: str | None = None
        if args.no_tunnel:
            tunnel_note = "--no-tunnel"
        elif not CLOUDFLARED.exists() and shutil.which("cloudflared") is None:
            tunnel_note = "cloudflared missing"
            say("[run] WARNING: cloudflared not found. Run scripts\\get_cloudflared.ps1 to let phones connect.")
        else:
            exe = CLOUDFLARED if CLOUDFLARED.exists() else Path(shutil.which("cloudflared") or "cloudflared")
            keeper = TunnelKeeper(tunnel_command(exe, args.port), base)
            public_url = keeper.open()
            if public_url is None:
                tunnel_note = "tunnel failed; retrying in the background"

        banner(args.port, public_url, health, tunnel_note)
        if args.open:
            webbrowser.open(f"http://localhost:{args.port}/dashboard")

        # Watch the children. Short waits keep Ctrl+C responsive on Windows.
        while True:
            code = backend.poll()
            if code is not None:
                time.sleep(0.3)
                say(f"[run] ERROR: the server stopped unexpectedly (exit code {code}).")
                return code or 1
            if keeper is not None:
                keeper.tick()
            time.sleep(0.5)
    except KeyboardInterrupt:
        say("\n[run] Stopping...")
        return 0
    except SystemExit as exc:
        if isinstance(exc.code, str):
            say(exc.code)
            return 1
        raise
    finally:
        # A second impatient Ctrl+C must not abort the cleanup and orphan a child holding the port.
        # Every step below has its own timeout, so ignoring it cannot hang.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        if hasattr(signal, "SIGBREAK"):
            signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        # Tunnel first so phones stop hitting a server that is shutting down.
        if keeper is not None:
            keeper.stop()
        stop_process(backend, "server", graceful=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:  # Ctrl+C before the server started (e.g. during the build)
        say("\n[run] Cancelled.")
        sys.exit(130)
