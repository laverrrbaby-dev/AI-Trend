from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_PORT = 8501
DEFAULT_HEARTBEAT_PORT = 8765
IDLE_TIMEOUT_SECONDS = 12
READY_TIMEOUT_SECONDS = 45


class HeartbeatState:
    def __init__(self) -> None:
        self.last_seen = 0.0
        self.seen_once = False
        self.lock = threading.Lock()

    def mark_seen(self) -> None:
        with self.lock:
            self.last_seen = time.time()
            self.seen_once = True

    def is_idle(self, timeout_seconds: int) -> bool:
        with self.lock:
            return self.seen_once and time.time() - self.last_seen > timeout_seconds


class HeartbeatHandler(BaseHTTPRequestHandler):
    state: HeartbeatState

    def do_GET(self) -> None:
        if self.path.startswith("/ping"):
            self.state.mark_seen()
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return

        self.send_response(404)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the local ETF radar site and stop it when the page closes.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TREND_RADAR_PORT", DEFAULT_SITE_PORT)))
    parser.add_argument(
        "--heartbeat-port",
        type=int,
        default=int(os.environ.get("TREND_RADAR_HEARTBEAT_PORT", DEFAULT_HEARTBEAT_PORT)),
    )
    parser.add_argument("--no-browser", action="store_true", help="Start the site without opening a browser.")
    args = parser.parse_args()

    venv_python = _ensure_environment()
    site_port = _first_free_port(args.port)
    heartbeat_port = _first_free_port(args.heartbeat_port)
    heartbeat_url = f"http://127.0.0.1:{heartbeat_port}/ping"

    state = HeartbeatState()
    heartbeat_server = _start_heartbeat_server(heartbeat_port, state)
    process = _start_streamlit(venv_python, site_port, heartbeat_url)
    site_url = f"http://localhost:{site_port}"
    ready_url = f"http://127.0.0.1:{site_port}"

    try:
        if _wait_until_ready(ready_url, process):
            print(f"网站已启动: {site_url}", flush=True)
            print("关闭浏览器页面后，服务会在约 12 秒内自动停止。", flush=True)
            if not args.no_browser:
                webbrowser.open(site_url)
        else:
            return process.returncode or 1

        while process.poll() is None:
            if state.is_idle(IDLE_TIMEOUT_SECONDS):
                print("页面已关闭，正在停止服务...", flush=True)
                break
            time.sleep(1)
        return process.returncode or 0
    except KeyboardInterrupt:
        print("收到停止信号，正在关闭服务...", flush=True)
        return 130
    finally:
        heartbeat_server.shutdown()
        heartbeat_server.server_close()
        _stop_process(process)


def _ensure_environment() -> Path:
    venv_python = ROOT / ".venv" / "bin" / "python"
    if not venv_python.exists():
        print("首次启动：正在创建 .venv...", flush=True)
        subprocess.check_call([sys.executable, "-m", "venv", str(ROOT / ".venv")], cwd=ROOT)

    if not _can_import_streamlit(venv_python):
        print("正在安装项目依赖...", flush=True)
        subprocess.check_call([str(venv_python), "-m", "pip", "install", "-r", "requirements.txt"], cwd=ROOT)
        subprocess.check_call([str(venv_python), "-m", "pip", "install", "-e", "."], cwd=ROOT)

    return venv_python


def _can_import_streamlit(venv_python: Path) -> bool:
    result = subprocess.run(
        [str(venv_python), "-c", "import streamlit, pandas, plotly"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def _first_free_port(start: int) -> int:
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No free port found from {start} to {start + 99}.")


def _start_heartbeat_server(port: int, state: HeartbeatState) -> ThreadingHTTPServer:
    HeartbeatHandler.state = state
    server = ThreadingHTTPServer(("127.0.0.1", port), HeartbeatHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def _start_streamlit(venv_python: Path, port: int, heartbeat_url: str) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env["TREND_RADAR_HEARTBEAT_URL"] = heartbeat_url
    env.setdefault("BROWSER", "none")

    command = [
        str(venv_python),
        "-m",
        "streamlit",
        "run",
        "app.py",
        "--server.headless",
        "true",
        "--server.port",
        str(port),
        "--browser.gatherUsageStats",
        "false",
    ]
    return subprocess.Popen(command, cwd=ROOT, env=env, start_new_session=True)


def _wait_until_ready(url: str, process: subprocess.Popen[bytes]) -> bool:
    deadline = time.time() + READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            print("Streamlit 启动失败。", flush=True)
            return False
        try:
            with urllib.request.urlopen(url, timeout=1):
                return True
        except OSError:
            time.sleep(0.5)

    print(f"等待 {READY_TIMEOUT_SECONDS} 秒后仍未能打开 {url}。", flush=True)
    return False


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=3)


if __name__ == "__main__":
    raise SystemExit(main())
