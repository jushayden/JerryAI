"""Live, read-only stock research tools for Jerry.

The tools rank names for further research; they never claim certainty, provide
personalized financial advice, or place trades. Data comes from live Yahoo
Finance market endpoints and official SEC filing metadata, with every source
retained on the task and raw results exported as artifacts.
"""
import asyncio
import csv
import json
import math
import statistics
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import config
import state


_task: state.TaskRecord | None = None
_quote_cache: dict[str, dict] = {}
_sec_tickers: dict[str, dict] | None = None

YAHOO_SCREEN = (
    "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?"
    "formatted=false&scrIds={screen}&count={count}&start=0"
)
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}?range=1y&interval=1d"
YAHOO_SEARCH = (
    "https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&quotesCount=1&newsCount={count}"
)
SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"


def configure(task=None) -> None:
    global _task
    _task = task


def _source(url: str) -> None:
    if _task is not None:
        state.add_source(_task, url)


def _artifact(path) -> None:
    if _task is not None:
        state.add_artifact(_task, path)


def _get_json(url: str, *, sec: bool = False) -> dict:
    user_agent = (
        "JerryPocketAgent/1.0 local-stock-research contact@example.com"
        if sec else "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JerryPocketAgent/1.0"
    )
    req = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "application/json"},
    )
    last = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=18) as response:
                return json.load(response)
        except Exception as exc:
            last = exc
            if attempt == 0:
                time.sleep(0.4)
    raise RuntimeError(f"live source failed: {url}: {last}")


def _num(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value) -> float | None:
    return round(value * 100, 2) if value is not None else None


def _at(values: list[float], sessions: int) -> float | None:
    if len(values) <= sessions:
        return None
    return values[-sessions - 1]


def _chart_metrics(ticker: str) -> tuple[dict, str]:
    url = YAHOO_CHART.format(ticker=urllib.parse.quote(ticker))
    data = _get_json(url)
    result = (data.get("chart", {}).get("result") or [None])[0]
    if not result:
        raise RuntimeError(data.get("chart", {}).get("error") or "no chart data")
    quote = (result.get("indicators", {}).get("quote") or [{}])[0]
    closes_raw = quote.get("close") or []
    volumes_raw = quote.get("volume") or []
    pairs = [(float(close), int(volumes_raw[i] or 0))
             for i, close in enumerate(closes_raw)
             if close is not None and i < len(volumes_raw)]
    if len(pairs) < 2:
        raise RuntimeError("not enough price history")
    closes = [x[0] for x in pairs]
    volumes = [x[1] for x in pairs]
    last = closes[-1]

    def change(sessions: int) -> float | None:
        start = _at(closes, sessions)
        return (last / start - 1) if start else None

    daily = [(closes[i] / closes[i - 1] - 1) for i in range(1, len(closes))
             if closes[i - 1]]
    recent_daily = daily[-60:]
    annual_vol = (statistics.stdev(recent_daily) * math.sqrt(252)
                  if len(recent_daily) >= 10 else None)
    baseline_volumes = [v for v in volumes[-21:-1] if v > 0]
    volume_ratio = (volumes[-1] / statistics.mean(baseline_volumes)
                    if baseline_volumes and volumes[-1] else None)
    high = max(closes)
    meta = result.get("meta") or {}
    metrics = {
        "ticker": ticker,
        "name": meta.get("longName") or meta.get("shortName") or ticker,
        "currency": meta.get("currency"),
        "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
        "price": round(last, 4),
        "return_1d_pct": _pct(change(1)),
        "return_5d_pct": _pct(change(5)),
        "return_1m_pct": _pct(change(21)),
        "return_3m_pct": _pct(change(63)),
        "return_1y_pct": _pct(last / closes[0] - 1 if closes[0] else None),
        "annualized_volatility_pct": _pct(annual_vol),
        "volume": volumes[-1],
        "volume_vs_20d": round(volume_ratio, 2) if volume_ratio is not None else None,
        "drawdown_from_1y_high_pct": round((last / high - 1) * 100, 2) if high else None,
        "market_time": meta.get("regularMarketTime"),
    }
    return metrics, url


def _score(row: dict) -> tuple[int, list[str]]:
    """Transparent research-priority score, not a buy recommendation."""
    day = _num(row.get("return_1d_pct"), _num(row.get("day_change_pct")))
    month = _num(row.get("return_1m_pct"))
    volume_ratio = max(0.01, _num(row.get("volume_vs_20d"), 1.0))
    market_cap = _num(row.get("market_cap"))
    volatility = _num(row.get("annualized_volatility_pct"))
    drawdown = abs(min(0, _num(row.get("drawdown_from_1y_high_pct"))))
    score = 45.0
    score += max(-12, min(12, day)) * 1.1
    score += max(-8, min(8, month / 3))
    score += max(-5, min(12, math.log2(volume_ratio) * 4))
    if market_cap >= 10_000_000_000:
        score += 6
    elif market_cap >= 1_000_000_000:
        score += 3
    elif market_cap and market_cap < 300_000_000:
        score -= 15
    if volatility > 100:
        score -= 12
    elif volatility > 70:
        score -= 6
    if drawdown > 60:
        score -= 8
    if abs(day) > 25:
        score -= 8
    score = int(round(max(0, min(100, score))))

    risks = []
    if market_cap and market_cap < 300_000_000:
        risks.append("micro-cap / manipulation and liquidity risk")
    if abs(day) > 20:
        risks.append("extreme one-day move; reversal risk")
    if volume_ratio > 5:
        risks.append("abnormal volume; verify the catalyst")
    if volatility > 80:
        risks.append("very high realized volatility")
    if drawdown > 50:
        risks.append("more than 50% below its one-year high")
    if not risks:
        risks.append("normal equity and company-specific risk")
    return score, risks


def _screen_sync(screen: str, count: int) -> tuple[list[dict], str]:
    url = YAHOO_SCREEN.format(screen=screen, count=count)
    data = _get_json(url)
    result = data.get("finance", {}).get("result") or []
    quotes = result[0].get("quotes", []) if result else []
    return quotes, url


def _scan_sync(mode: str, limit: int, min_market_cap: float) -> dict:
    screens = ["day_gainers", "most_actives"] if mode == "both" else [mode]
    merged: dict[str, dict] = {}
    sources = []
    for screen in screens:
        quotes, url = _screen_sync(screen, max(limit * 3, 20))
        sources.append(url)
        for quote in quotes:
            ticker = str(quote.get("symbol") or "").upper()
            if (not ticker or quote.get("quoteType") != "EQUITY"
                    or _num(quote.get("marketCap")) < min_market_cap):
                continue
            merged.setdefault(ticker, quote)
            _quote_cache[ticker] = quote
    rows = []
    for ticker, quote in list(merged.items())[: max(limit * 2, limit)]:
        try:
            metrics, chart_url = _chart_metrics(ticker)
        except Exception:
            continue
        row = {
            **metrics,
            "name": quote.get("longName") or quote.get("shortName") or metrics["name"],
            "market_cap": quote.get("marketCap"),
            "day_change_pct": quote.get("regularMarketChangePercent"),
            "average_volume_3m": quote.get("averageDailyVolume3Month"),
            "trailing_pe": quote.get("trailingPE"),
            "forward_pe": quote.get("forwardPE"),
            "eps_ttm": quote.get("epsTrailingTwelveMonths"),
            "analyst_rating_vendor_field": quote.get("averageAnalystRating"),
            "data_sources": [chart_url],
        }
        row["research_priority_score"], row["risk_flags"] = _score(row)
        rows.append(row)
    rows.sort(key=lambda x: (x["research_priority_score"], x.get("volume", 0)), reverse=True)
    return {"as_of_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
            "method": "Transparent research-priority ranking; not a buy recommendation.",
            "stocks": rows[:limit], "sources": sources}


def _sec_map() -> dict[str, dict]:
    global _sec_tickers
    if _sec_tickers is None:
        raw = _get_json(SEC_TICKERS, sec=True)
        _sec_tickers = {str(item["ticker"]).upper(): item for item in raw.values()}
    return _sec_tickers


def _sec_recent(ticker: str, limit: int = 5) -> tuple[list[dict], list[str]]:
    item = _sec_map().get(ticker)
    if not item:
        return [], [SEC_TICKERS]
    cik = int(item["cik_str"])
    submissions_url = f"https://data.sec.gov/submissions/CIK{cik:010d}.json"
    data = _get_json(submissions_url, sec=True)
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    filings = []
    for i, form in enumerate(forms):
        if form not in {"8-K", "10-Q", "10-K", "6-K", "20-F"}:
            continue
        accession = recent.get("accessionNumber", [])[i]
        primary = recent.get("primaryDocument", [])[i]
        archive = (f"https://www.sec.gov/Archives/edgar/data/{cik}/"
                   f"{accession.replace('-', '')}/{primary}")
        filings.append({"form": form, "filing_date": recent.get("filingDate", [])[i],
                        "report_date": recent.get("reportDate", [])[i], "url": archive})
        if len(filings) >= limit:
            break
    return filings, [SEC_TICKERS, submissions_url, *[x["url"] for x in filings]]


def _ticker_research_sync(ticker: str, news_count: int) -> dict:
    ticker = ticker.upper().strip()
    metrics, chart_url = _chart_metrics(ticker)
    search_url = YAHOO_SEARCH.format(ticker=urllib.parse.quote(ticker), count=news_count)
    search = _get_json(search_url)
    quote = _quote_cache.get(ticker) or ((search.get("quotes") or [{}])[0])
    metrics.update({
        "name": quote.get("longname") or quote.get("shortname") or metrics["name"],
        "market_cap": quote.get("marketCap"),
        "day_change_pct": quote.get("regularMarketChangePercent"),
    })
    news = []
    for item in (search.get("news") or [])[:news_count]:
        published = item.get("providerPublishTime")
        news.append({
            "title": item.get("title"), "publisher": item.get("publisher"),
            "published_utc": (datetime.fromtimestamp(published, timezone.utc).isoformat()
                              if published else None),
            "url": item.get("link"),
        })
    try:
        filings, sec_sources = _sec_recent(ticker)
    except Exception as exc:
        filings, sec_sources = [], [f"SEC lookup unavailable: {exc}"]
    metrics["research_priority_score"], metrics["risk_flags"] = _score(metrics)
    return {
        "ticker": ticker, "snapshot": metrics,
        "possible_news_catalysts": news,
        "recent_sec_filings": filings,
        "interpretation_boundary": (
            "Headlines are possible catalysts, not proof that they caused the price move. "
            "The score prioritizes further research and is not a buy/sell recommendation."
        ),
        "sources": [chart_url, search_url, *[n["url"] for n in news if n.get("url")],
                    *[s for s in sec_sources if s.startswith("http")]],
    }


def _write_scan_artifacts(payload: dict) -> list[str]:
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"task_{_task.id}" if _task is not None else f"stock_scan_{int(time.time())}"
    json_path = config.ARTIFACT_DIR / f"{stem}_stock_scan.json"
    csv_path = config.ARTIFACT_DIR / f"{stem}_stock_scan.csv"
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    rows = payload.get("stocks") or []
    fields = ["ticker", "name", "price", "day_change_pct", "return_5d_pct",
              "return_1m_pct", "return_3m_pct", "return_1y_pct", "volume",
              "volume_vs_20d", "market_cap", "annualized_volatility_pct",
              "drawdown_from_1y_high_pct", "research_priority_score", "risk_flags"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "risk_flags": "; ".join(row.get("risk_flags", []))})
    for path in (json_path, csv_path):
        _artifact(path)
    return [str(json_path), str(csv_path)]


def _write_research_artifact(payload: dict) -> str:
    config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"task_{_task.id}" if _task is not None else f"stock_research_{int(time.time())}"
    path = config.ARTIFACT_DIR / f"{stem}_stock_research.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    _artifact(path)
    return str(path)


async def scan_stock_movers(args: dict) -> str:
    try:
        mode = str(args.get("mode") or "both").lower()
        if mode not in {"day_gainers", "most_actives", "both"}:
            return "Error: mode must be day_gainers, most_actives, or both."
        limit = max(1, min(15, int(args.get("limit") or 8)))
        min_market_cap = max(0, float(args.get("min_market_cap") or 1_000_000_000))
        payload = await asyncio.to_thread(_scan_sync, mode, limit, min_market_cap)
        payload["artifacts"] = _write_scan_artifacts(payload)
        for url in payload.get("sources", []):
            _source(url)
        for row in payload.get("stocks", []):
            for url in row.get("data_sources", []):
                _source(url)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: live stock-mover scan failed: {exc}"


async def research_stocks(args: dict) -> str:
    try:
        raw = args.get("tickers") or []
        if isinstance(raw, str):
            raw = re_split_tickers(raw)
        tickers = list(dict.fromkeys(str(x).upper().strip() for x in raw if str(x).strip()))
        if not tickers:
            return "Error: provide at least one ticker from a live scan or the user."
        tickers = tickers[:8]
        news_count = max(1, min(8, int(args.get("news_per_stock") or 5)))
        results = []
        errors = []
        for ticker in tickers:
            try:
                results.append(await asyncio.to_thread(_ticker_research_sync, ticker, news_count))
            except Exception as exc:
                errors.append({"ticker": ticker, "error": str(exc)})
        results.sort(key=lambda x: x["snapshot"]["research_priority_score"], reverse=True)
        payload = {
            "as_of_utc": datetime.now(timezone.utc).isoformat(),
            "disclaimer": (
                "Research support only, not personalized financial advice. A high score means "
                "the stock merits deeper review; it does not mean it is safe or certain to rise."
            ),
            "results": results, "errors": errors,
        }
        payload["artifact"] = _write_research_artifact(payload)
        for result in results:
            for url in result.get("sources", []):
                _source(url)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as exc:
        return f"Error: stock research failed: {exc}"


def re_split_tickers(value: str) -> list[str]:
    return [x for x in value.replace(",", " ").split() if x]


def _tool(name: str, description: str, properties: dict, required: list[str], fn) -> dict:
    return {
        "schema": {"type": "function", "function": {
            "name": name, "description": description,
            "parameters": {"type": "object", "properties": properties,
                           "required": required},
        }},
        "fn": fn,
    }


TOOLS = {
    "scan_stock_movers": _tool(
        "scan_stock_movers",
        "Scan live US day gainers and/or most-active stocks, calculate price/volume/volatility metrics, filter tiny companies, and rank names for further research. The score is transparent and is NOT a buy recommendation. Returns CSV and JSON artifacts.",
        {"mode": {"type": "string", "enum": ["day_gainers", "most_actives", "both"]},
         "limit": {"type": "integer", "description": "1 to 15, default 8"},
         "min_market_cap": {"type": "number", "description": "Default 1000000000 USD"}},
        [], scan_stock_movers),
    "research_stocks": _tool(
        "research_stocks",
        "Deep-research up to 8 stock tickers using live one-year price/volume history, current headlines, and official recent SEC filings. Distinguishes possible catalysts from proven causes and returns sourced JSON.",
        {"tickers": {"type": "array", "items": {"type": "string"}},
         "news_per_stock": {"type": "integer", "description": "1 to 8, default 5"}},
        ["tickers"], research_stocks),
}
