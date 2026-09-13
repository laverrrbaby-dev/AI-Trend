from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SITE_PORT = 8501
READY_TIMEOUT_SECONDS = 45


def main() -> int:
    parser = argparse.ArgumentParser(description="Open the local ETF radar site.")
    parser.add_argument("--port", type=int, default=int(os.environ.get("TREND_RADAR_PORT", DEFAULT_SITE_PORT)))
    parser.add_argument("--no-browser", action="store_true", help="Start the site without opening a browser.")
    args = parser.parse_args()

    site_url = f"http://localhost:{args.port}"
    ready_url = f"http://127.0.0.1:{args.port}"
    if _is_site_running(ready_url):
        print(f"网站已在运行: {site_url}", flush=True)
        if not args.no_browser:
            webbrowser.open(site_url)
        return 0

    venv_python = _ensure_environment()
    process = _start_streamlit(venv_python, args.port)
    stop_requested = False

    try:
        if _wait_until_ready(ready_url, process):
            print(f"网站已启动: {site_url}", flush=True)
            print("关闭页面不会停止服务；在启动终端按 Ctrl-C 可主动停止。", flush=True)
            if not args.no_browser:
                webbrowser.open(site_url)
        else:
            return process.returncode or 1

        while process.poll() is None:
            time.sleep(1)
        return process.returncode or 0
    except KeyboardInterrupt:
        print("收到停止信号，正在关闭服务...", flush=True)
        stop_requested = True
        return 130
    finally:
        if stop_requested:
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


def _is_site_running(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1):
            return True
    except OSError:
        return False


def _start_streamlit(venv_python: Path, port: int) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
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
