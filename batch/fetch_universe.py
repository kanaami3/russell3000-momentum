"""Fetch US Russell 3000 proxy universe from NASDAQ stock screener.

Russell 3000 is effectively the top ~3000 US-listed common stocks by market cap.
We fetch NASDAQ + NYSE + AMEX listings via NASDAQ.com's public screener API,
filter to common stocks, sort by market cap, and take the top N.

Output: data/universe_us.json
    [{ticker, name, market_cap, exchange}, ...]
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "universe_us.json"

SCREENER_URL = "https://api.nasdaq.com/api/screener/stocks"
EXCHANGES = ["NASDAQ", "NYSE", "AMEX"]
TARGET_COUNT = 3000

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/market-activity/stocks/screener",
}


def fetch_exchange(exchange: str) -> list[dict]:
    params = {"tableonly": "true", "limit": "10000", "exchange": exchange}
    resp = requests.get(SCREENER_URL, headers=HEADERS, params=params, timeout=60)
    resp.raise_for_status()
    j = resp.json()
    rows = (j.get("data") or {}).get("table", {}).get("rows") or []
    for row in rows:
        row["_exchange"] = exchange
    return rows


def parse_market_cap(raw: str | None) -> float:
    if not raw:
        return 0.0
    cleaned = re.sub(r"[,$\s]", "", raw)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def is_common_stock(row: dict) -> bool:
    """Keep ordinary common shares; drop ETFs, ADRs of unusual classes, units, etc."""
    name = (row.get("name") or "").lower()
    symbol = (row.get("symbol") or "").strip()

    if not symbol:
        return False
    if any(ch in symbol for ch in (" ", "/", "^")):
        return False
    drop_keywords = (
        "etf", "etn", "fund", "trust common",
        "warrant", "warrants",
        "right ", "rights", " unit", " units",
        "preferred", "depositary",
        "notes due", "note due",
    )
    for kw in drop_keywords:
        if kw in name:
            return False
    return True


def main() -> int:
    all_rows: list[dict] = []
    for ex in EXCHANGES:
        try:
            rows = fetch_exchange(ex)
            print(f"  {ex}: {len(rows)} rows", file=sys.stderr)
            all_rows.extend(rows)
        except Exception as e:
            print(f"  {ex}: ERROR {e}", file=sys.stderr)

    by_symbol: dict[str, dict] = {}
    for row in all_rows:
        symbol = (row.get("symbol") or "").strip()
        if not symbol or not is_common_stock(row):
            continue
        mcap = parse_market_cap(row.get("marketCap"))
        if mcap <= 0:
            continue
        existing = by_symbol.get(symbol)
        if existing is None or mcap > parse_market_cap(existing.get("marketCap")):
            by_symbol[symbol] = row

    ranked = sorted(
        by_symbol.values(),
        key=lambda r: parse_market_cap(r.get("marketCap")),
        reverse=True,
    )[:TARGET_COUNT]

    universe = [
        {
            "ticker": r["symbol"],
            "name": (r.get("name") or "").replace(" Common Stock", "").strip(),
            "market_cap": parse_market_cap(r.get("marketCap")),
            "exchange": r.get("_exchange", ""),
        }
        for r in ranked
    ]

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(universe)} tickers to {OUTPUT_PATH}", file=sys.stderr)
    print(f"  top: {universe[0]['ticker']} ${universe[0]['market_cap']:,.0f}", file=sys.stderr)
    print(f"  bottom: {universe[-1]['ticker']} ${universe[-1]['market_cap']:,.0f}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
