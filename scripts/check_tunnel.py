"""Check that a cloudflared quick tunnel works from this network, before demo day.

    .venv\\Scripts\\python.exe scripts\\check_tunnel.py [--port 8106]

Serves a throwaway page with `python -m http.server`, opens a quick tunnel to it, finds the
public URL with the same parser run.py uses, fetches the page through the internet, then stops
everything. Exit code 0 means phones will be able to reach the demo.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
from run import CLOUDFLARED, parse_tunnel_url, wait_for_public_dns  # noqa: E402

MARKER = "floodline-tunnel-check"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8106)
    parser.add_argument("--log", type=Path, help="also save cloudflared's raw output to this file")
    args = parser.parse_args()

    if not CLOUDFLARED.exists():
        print("cloudflared missing: run scripts\\get_cloudflared.ps1 first.")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "index.html").write_text(MARKER, encoding="utf-8")
        server = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(args.port), "--bind", "127.0.0.1"],
            cwd=tmp, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        tunnel = subprocess.Popen(
            [str(CLOUDFLARED), "tunnel", "--url", f"http://127.0.0.1:{args.port}", "--no-autoupdate"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        lines: list[str] = []
        found = threading.Event()
        urls: list[str] = []

        def read() -> None:
            assert tunnel.stdout is not None
            for raw in tunnel.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip()
                lines.append(line)
                url = parse_tunnel_url(line)
                if url:
                    urls.append(url)
                    found.set()

        threading.Thread(target=read, daemon=True).start()
        try:
            started = time.monotonic()
            if not found.wait(40):
                print("FAIL: cloudflared printed no trycloudflare.com URL in 40 s. Last lines:")
                print("\n".join(lines[-15:]))
                return 1
            url = urls[0]
            print(f"Tunnel URL after {time.monotonic() - started:.1f} s: {url}")

            # Asking the local resolver before the name exists caches "no such host" for ~60 s,
            # so wait until public DNS has it (same logic run.py uses before showing the QR code).
            live = wait_for_public_dns(url)
            print(f"Public DNS {'has' if live else 'did not confirm'} the name after {time.monotonic() - started:.1f} s")
            deadline = time.monotonic() + 30
            while True:
                try:
                    response = httpx.get(url + "/", timeout=8)
                    if response.status_code == 200 and MARKER in response.text:
                        print(f"OK: fetched the page through the tunnel ({time.monotonic() - started:.1f} s total).")
                        return 0
                    print(f"  got HTTP {response.status_code}, retrying...")
                except httpx.HTTPError as exc:
                    print(f"  not reachable yet ({type(exc).__name__}: {exc}), retrying...")
                if time.monotonic() > deadline:
                    print("FAIL: the tunnel URL never answered. Try `ipconfig /flushdns`, or another network.")
                    return 1
                time.sleep(2)
        finally:
            tunnel.terminate()
            server.terminate()
            tunnel.wait(10)
            server.wait(10)
            time.sleep(0.3)  # let the reader thread drain the last lines
            if args.log:
                args.log.write_text("\n".join(lines) + "\n", encoding="utf-8")
            distinct = sorted(set(urls))
            print(f"URL lines parsed: {len(urls)}; distinct URLs: {distinct}")


if __name__ == "__main__":
    sys.exit(main())
