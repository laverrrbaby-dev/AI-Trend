from __future__ import annotations

import argparse
import json
import os
import ssl
import subprocess
import sys
import time
import urllib.parse
import urllib.request


TEST_CODE = "510050"
TEST_START = "20260522"
TEST_END = "20260529"
EASTMONEY_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0 Safari/537.36"


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose Eastmoney/AkShare history connectivity.")
    parser.add_argument("--code", default=TEST_CODE, help="ETF code to test")
    parser.add_argument("--start", default=TEST_START, help="Start date, YYYYMMDD")
    parser.add_argument("--end", default=TEST_END, help="End date, YYYYMMDD")
    parser.add_argument("--proxy", default=None, help="Optional proxy for curl, e.g. http://127.0.0.1:7890")
    parser.add_argument("--akshare", action="store_true", help="Also call akshare.fund_etf_hist_em")
    args = parser.parse_args()

    url = _eastmoney_url(args.code, args.start, args.end)
    print("=" * 60)
    print("push2his.eastmoney.com network diagnostics")
    print(f"Target: {args.code} {args.start} ~ {args.end}")
    print(f"Python: {sys.version.split()[0]}")
    print()

    _print_dns()
    _print_proxy_env()
    ok = [
        _try_urllib(url),
        _try_requests(url),
        _try_curl(url, args.proxy),
    ]
    if args.akshare:
        ok.append(_try_akshare(args.code, args.start, args.end))

    print("=" * 60)
    if any(ok):
        print("At least one method returned kline data.")
        return 0
    print("All tested methods failed. Check network, firewall, VPN, or proxy settings.")
    return 1


def _eastmoney_url(code: str, start_date: str, end_date: str) -> str:
    market_id = 1 if str(code).zfill(6).startswith("5") else 0
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": "101",
        "fqt": "0",
        "beg": start_date,
        "end": end_date,
        "secid": f"{market_id}.{str(code).zfill(6)}",
    }
    return f"https://push2his.eastmoney.com/api/qt/stock/kline/get?{urllib.parse.urlencode(params)}"


def _print_dns() -> None:
    print("--- DNS ---")
    try:
        import socket

        ip = socket.getaddrinfo("push2his.eastmoney.com", 443)[0][4][0]
        print(f"  push2his.eastmoney.com -> {ip}")
    except Exception as exc:  # noqa: BLE001 - diagnostic output should keep going
        print(f"  FAIL: {type(exc).__name__}: {exc}")


def _print_proxy_env() -> None:
    print("--- Proxy Environment ---")
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY", "NO_PROXY"):
        print(f"  {key}: {os.environ.get(key, 'NOT SET')}")


def _try_urllib(url: str) -> bool:
    print("--- urllib.request ---")
    try:
        import certifi

        context = ssl.create_default_context(cafile=certifi.where())
        context.set_alpn_protocols(["http/1.1"])
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        req = urllib.request.Request(url, headers={"User-Agent": EASTMONEY_UA})
        start = time.time()
        with urllib.request.urlopen(req, timeout=15, context=context) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return _print_kline_result(data, start)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False


def _try_requests(url: str) -> bool:
    print("--- requests Session trust_env=False ---")
    try:
        import certifi
        import requests

        session = requests.Session()
        session.trust_env = False
        session.verify = certifi.where()
        start = time.time()
        response = session.get(url, timeout=15, headers={"User-Agent": EASTMONEY_UA})
        response.raise_for_status()
        return _print_kline_result(response.json(), start)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False


def _try_curl(url: str, proxy: str | None) -> bool:
    print("--- curl ---")
    command = ["curl", "-sS", "-L", "-m", "15", "-A", EASTMONEY_UA]
    if proxy:
        command.extend(["-x", proxy])
    command.append(url)
    try:
        start = time.time()
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  FAIL: curl exited {result.returncode}: {result.stderr.strip()}")
            return False
        return _print_kline_result(json.loads(result.stdout), start)
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False


def _try_akshare(code: str, start_date: str, end_date: str) -> bool:
    print("--- AkShare fund_etf_hist_em ---")
    try:
        import akshare

        start = time.time()
        frame = akshare.fund_etf_hist_em(symbol=str(code).zfill(6), period="daily", start_date=start_date, end_date=end_date, adjust="")
        rows = len(frame)
        print(f"  {'OK' if rows else 'EMPTY'} {rows} rows {time.time() - start:.1f}s")
        return rows > 0
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL: {type(exc).__name__}: {exc}")
        return False


def _print_kline_result(data: dict[str, object], start: float) -> bool:
    payload = data.get("data") if isinstance(data, dict) else None
    klines = payload.get("klines") if isinstance(payload, dict) else None
    rows = len(klines or [])
    print(f"  {'OK' if rows else 'EMPTY'} {rows} klines {time.time() - start:.1f}s")
    if rows:
        print(f"  first: {klines[0]}")
        print(f"  last:  {klines[-1]}")
    return rows > 0


if __name__ == "__main__":
    raise SystemExit(main())
