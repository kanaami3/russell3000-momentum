"""Compute momentum metrics from daily prices for the chosen market.

Usage:
    python batch/calc_momentum.py us       # default
    python batch/calc_momentum.py jp

Inputs:
- data/universe_{market}.json
- data/prices_{market}.csv

Output:
- web/data/momentum_{market}.json: {
    asof, market, ticker_count, currency,
    yesterday_top10, yesterday_worst10,
    mom_1w_top10, mom_1m_top10, mom_3m_top10, mom_12_1_top10,
    all_tickers: [ ... ]
  }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

WINDOWS = {
    "daily": 1,
    "w1": 5,
    "m1": 21,
    "m3": 63,
    "m12": 252,
}

# Sparkline = last N trading days of closes (~1 month), shown as a mini chart
# so users can spot recent pullbacks in uptrending stocks at a glance.
SPARKLINE_DAYS = 21

# Chart history = OHLCV+date series used for the in-modal chart rendering.
# Required for JP where TradingView's free embed widget lacks data licensing.
# 126 = ~6 months — balance between coverage (supports 1M/3M/6M zoom buttons)
# and payload size for mobile users.
CHART_HISTORY_DAYS = 126

MARKET_META = {
    "us": {"currency": "USD", "symbol": "$"},
    "jp": {"currency": "JPY", "symbol": "¥"},
}


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    prices = prices.sort_values(["ticker", "date"])
    rows: list[dict] = []
    for ticker, group in prices.groupby("ticker", sort=False):
        closes = group["close"].to_numpy()
        if len(closes) < 2:
            continue
        record: dict = {"ticker": ticker, "close": float(closes[-1])}
        for name, lookback in WINDOWS.items():
            if len(closes) > lookback:
                start = closes[-lookback - 1]
                end = closes[-1]
                if start > 0:
                    record[f"ret_{name}"] = (end / start - 1.0) * 100.0
                else:
                    record[f"ret_{name}"] = None
            else:
                record[f"ret_{name}"] = None
        if record.get("ret_m12") is not None and record.get("ret_m1") is not None:
            record["ret_12_1"] = record["ret_m12"] - record["ret_m1"]
        else:
            record["ret_12_1"] = None
        # Sparkline: last SPARKLINE_DAYS closes, rounded for compactness
        sparkline_tail = closes[-SPARKLINE_DAYS:] if len(closes) >= 2 else closes
        record["sparkline"] = [round(float(c), 2) for c in sparkline_tail]
        rows.append(record)

    return pd.DataFrame(rows)


def top_n(df: pd.DataFrame, col: str, n: int = 10, ascending: bool = False) -> list[dict]:
    sub = df.dropna(subset=[col]).sort_values(col, ascending=ascending).head(n)
    return [
        {
            "ticker": r["ticker"],
            "name": r["name"],
            "value": round(float(r[col]), 2),
            "market_cap": r.get("market_cap", 0),
            "sparkline": r.get("sparkline", []),
        }
        for _, r in sub.iterrows()
    ]


def _round(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return round(float(v), 2)


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "us").lower()
    if market not in ("us", "jp"):
        print(f"ERROR: market must be 'us' or 'jp', got '{market}'", file=sys.stderr)
        return 1

    universe_path = REPO_ROOT / "data" / f"universe_{market}.json"
    prices_path = REPO_ROOT / "data" / f"prices_{market}.csv"
    output_path = REPO_ROOT / "web" / "data" / f"momentum_{market}.json"

    universe = pd.DataFrame(json.loads(universe_path.read_text(encoding="utf-8")))
    prices = pd.read_csv(prices_path, dtype={"ticker": str, "date": str, "close": float})

    print(f"[{market.upper()}] Universe: {len(universe)} tickers", file=sys.stderr)
    print(f"[{market.upper()}] Prices:   {len(prices):,} rows, {prices['ticker'].nunique()} tickers", file=sys.stderr)

    returns = compute_returns(prices)
    merged = returns.merge(universe, on="ticker", how="left")
    merged["name"] = merged["name"].fillna(merged["ticker"])
    if "market_cap" not in merged.columns:
        merged["market_cap"] = 0
    merged["market_cap"] = merged["market_cap"].fillna(0)

    asof = prices["date"].max()
    print(f"[{market.upper()}] As of: {asof}", file=sys.stderr)

    # Build per-ticker rows; market-specific columns differ
    def make_row(r: pd.Series) -> dict:
        base = {
            "ticker": r["ticker"],
            "name": r["name"],
            "market_cap": int(r["market_cap"]) if pd.notna(r["market_cap"]) else 0,
            "close": round(float(r["close"]), 2) if pd.notna(r["close"]) else None,
            "daily": _round(r.get("ret_daily")),
            "w1": _round(r.get("ret_w1")),
            "m1": _round(r.get("ret_m1")),
            "m3": _round(r.get("ret_m3")),
            "m12_1": _round(r.get("ret_12_1")),
            "sparkline": r.get("sparkline", []) if isinstance(r.get("sparkline"), list) else [],
        }
        if market == "us":
            base["exchange"] = r.get("exchange", "")
        else:
            # JP-specific metadata
            base["code"] = r.get("code", "")
            base["sector17"] = r.get("sector17", "")
            base["sector33"] = r.get("sector33", "")
            base["size_cat"] = r.get("size_cat", "")
        return base

    result = {
        "asof": asof,
        "market": market,
        "currency": MARKET_META[market]["currency"],
        "currency_symbol": MARKET_META[market]["symbol"],
        "ticker_count": int(merged["ticker"].nunique()),
        "universe_target": int(len(universe)),
        "yesterday_top10": top_n(merged, "ret_daily", 10, ascending=False),
        "yesterday_worst10": top_n(merged, "ret_daily", 10, ascending=True),
        "mom_1w_top10": top_n(merged, "ret_w1", 10, ascending=False),
        "mom_1m_top10": top_n(merged, "ret_m1", 10, ascending=False),
        "mom_3m_top10": top_n(merged, "ret_m3", 10, ascending=False),
        "mom_12_1_top10": top_n(merged, "ret_12_1", 10, ascending=False),
        "all_tickers": [make_row(r) for _, r in merged.iterrows()],
    }

    # Preserve previously-generated market summary if asof hasn't advanced.
    # This avoids losing the LLM-generated commentary when re-running calc
    # without rerunning generate_summary.py (e.g. during local iteration).
    if output_path.exists():
        try:
            prev = json.loads(output_path.read_text(encoding="utf-8"))
            if prev.get("asof") == asof and prev.get("market_summary"):
                result["market_summary"] = prev["market_summary"]
                if prev.get("market_summary_model"):
                    result["market_summary_model"] = prev["market_summary_model"]
        except Exception:
            pass

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    size_kb = output_path.stat().st_size / 1024
    print(f"[{market.upper()}] Wrote {output_path} ({size_kb:.1f} KB)", file=sys.stderr)

    # Always emit a companion chart_data file with OHLCV history per ticker.
    # The frontend lazy-loads it only when a user opens a chart modal, so it
    # doesn't bloat the initial page load.
    write_chart_data(market, prices)
    return 0


def write_chart_data(market: str, prices: pd.DataFrame) -> None:
    """Emit web/data/chart_data_{market}.json with last ~1 year OHLCV per ticker.

    Compact tuple format per row: [date, open, high, low, close, volume]
    """
    out_path = REPO_ROOT / "web" / "data" / f"chart_data_{market}.json"
    has_ohlc = all(c in prices.columns for c in ("open", "high", "low", "close", "volume"))

    prices = prices.sort_values(["ticker", "date"])
    chart_data: dict[str, list] = {}
    for ticker, group in prices.groupby("ticker", sort=False):
        tail = group.tail(CHART_HISTORY_DAYS)
        rows: list = []
        for _, r in tail.iterrows():
            close = float(r["close"])
            if has_ohlc:
                rows.append([
                    str(r["date"]),
                    round(float(r.get("open", close)), 2),
                    round(float(r.get("high", close)), 2),
                    round(float(r.get("low", close)), 2),
                    round(close, 2),
                    int(float(r.get("volume", 0) or 0)),
                ])
            else:
                rows.append([str(r["date"]), round(close, 2)])
        chart_data[ticker] = rows

    out_path.write_text(json.dumps(chart_data, ensure_ascii=False), encoding="utf-8")
    size_kb = out_path.stat().st_size / 1024
    print(
        f"[{market.upper()}] Wrote {out_path} ({size_kb:.1f} KB, {len(chart_data)} tickers, "
        f"format={'OHLCV' if has_ohlc else 'close-only'})",
        file=sys.stderr,
    )


if __name__ == "__main__":
    sys.exit(main())
