"""On-demand, cached Chinese news overviews for selected US stocks."""

from __future__ import annotations

import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import certifi


OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_OPENROUTER_MODEL = "openrouter/free"


def load_cached_news(symbol: str, data_date: str, *, root: Path) -> dict[str, Any] | None:
    return _read_json(_news_path(root, data_date, symbol))


def load_or_fetch_news(
    symbol: str,
    company_name: str,
    data_date: str,
    *,
    root: Path,
    fetch_json: Any = None,
) -> dict[str, Any]:
    path = _news_path(root, data_date, symbol)
    cached = load_cached_news(symbol, data_date, root=root)
    if cached:
        return cached
    api_key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        return {"status": "missing_key", "articles": [], "message": "未配置 OPENROUTER_API_KEY"}
    loader = fetch_json or _fetch_json
    try:
        payload = loader(
            OPENROUTER_CHAT_URL,
            {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            _request_payload(symbol, company_name, data_date),
        )
        result = _normalize_response(payload)
    except Exception as exc:  # noqa: BLE001 - news must never block anomaly details
        result = {"status": "failed", "articles": [], "message": f"新闻检索失败：{exc}"}
    result["symbol"] = symbol
    result["data_date"] = data_date
    result["generated_at"] = datetime.now().isoformat(timespec="seconds")
    if result.get("status") == "ok":
        _news_path(root, data_date, symbol).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def _request_payload(symbol: str, company_name: str, data_date: str) -> dict[str, Any]:
    model = os.getenv("OPENROUTER_MODEL", DEFAULT_OPENROUTER_MODEL)
    return {
        "model": model,
        "plugins": [{"id": "web", "max_results": 5}],
        "temperature": 0.1,
        "max_tokens": 700,
        "messages": [
            {
                "role": "system",
                "content": "你是财经信息整理助手。只根据公开新闻检索结果，输出中性事实，不做交易建议、趋势判断或因果推断。",
            },
            {
                "role": "user",
                "content": (
                    f"请检索 {data_date} 当天关于美股 {symbol}（{company_name}）的公开新闻。"
                    "最多返回2条。只输出JSON数组，每项包含 title_zh、summary_zh、source、url、published_at；"
                    "没有相关报道时输出空数组。summary_zh用一到两句中文概览，不能编造事实。"
                ),
            },
        ],
    }


def _normalize_response(payload: dict[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices") or []
    content = ((choices[0].get("message") or {}).get("content") if choices else "") or "[]"
    if isinstance(content, list):
        content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
    text = str(content).strip()
    if text.startswith("```"):
        text = text.strip("`").replace("json", "", 1).strip()
    try:
        articles = json.loads(text)
    except json.JSONDecodeError:
        articles = []
    if not isinstance(articles, list):
        articles = []
    normalized = []
    for article in articles[:2]:
        if not isinstance(article, dict):
            continue
        url = str(article.get("url") or "").strip()
        source = str(article.get("source") or "").strip()
        if not _is_http_url(url) or not source:
            continue
        normalized.append(
            {
                "title_zh": str(article.get("title_zh") or "").strip(),
                "summary_zh": str(article.get("summary_zh") or "").strip(),
                "source": source,
                "url": url,
                "published_at": str(article.get("published_at") or "").strip(),
            }
        )
    return {"status": "ok", "articles": normalized, "message": ""}


def _is_http_url(value: str) -> bool:
    parsed = urllib.parse.urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _news_path(root: Path, data_date: str, symbol: str) -> Path:
    path = root / "data" / "us_stocks" / "news" / data_date / f"{symbol.upper()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _fetch_json(url: str, headers: dict[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=30, context=context) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not isinstance(result, dict):
        raise RuntimeError("OpenRouter 返回格式异常")
    return result
