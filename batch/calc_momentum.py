"""Compute momentum metrics from daily prices.

Inputs:
- data/universe.json (ticker, name, market_cap, exchange)
- data/prices.csv (ticker, date, close — long format)

Outputs:
- data/momentum.json: {
    asof, ticker_count, summary_stats,
    yesterday_top10, yesterday_worst10,
    mom_1w_top10, mom_1m_top10, mom_3m_top10, mom_12_1_top10,
    all_tickers: [ {ticker, name, market_cap, daily, w1, m1, m3, m12_1}, ... ]
  }
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "universe.json"
PRICES_PATH = REPO_ROOT / "data" / "prices.csv"
# Web frontend reads this path directly
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "momentum.json"

# Trading-day lookback periods
WINDOWS = {
    "daily": 1,
    "w1": 5,
    "m1": 21,
    "m3": 63,
    "m12": 252,
}


def compute_returns(prices: pd.DataFrame) -> pd.DataFrame:
    """For each ticker, compute return over each window using its own price history.

    Uses the most recent close as the endpoint and the N-th most recent close (from
    the end) as the start. This correctly handles tickers with missing days.
    """
    prices = prices.sort_values(["ticker", "date"])
    # latest close per ticker
    latest = prices.groupby("ticker").tail(1).set_index("ticker")["close"].rename("close_latest")

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
        # 12-1 momentum = 12m return minus 1m return (in percentage points)
        if record.get("ret_m12") is not None and record.get("ret_m1") is not None:
            record["ret_12_1"] = record["ret_m12"] - record["ret_m1"]
        else:
            record["ret_12_1"] = None
        rows.append(record)

    return pd.DataFrame(rows)


def top_n(df: pd.DataFrame, col: str, n: int = 10, ascending: bool = False) -> list[dict]:
    sub = df.dropna(subset=[col]).sort_values(col, ascending=ascending).head(n)
    return [
        {
            "ticker": r["ticker"],
            "name": r["name"],
            "value": round(float(r[col]), 2),
            "market_cap": r["market_cap"],
        }
        for _, r in sub.iterrows()
    ]


def main() -> int:
    universe = pd.DataFrame(json.loads(UNIVERSE_PATH.read_text(encoding="utf-8")))
    prices = pd.read_csv(PRICES_PATH, dtype={"ticker": str, "date": str, "close": float})

    print(f"Loaded universe: {len(universe)} tickers", file=sys.stderr)
    print(f"Loaded prices:   {len(prices):,} rows, {prices['ticker'].nunique()} tickers", file=sys.stderr)

    returns = compute_returns(prices)
    merged = returns.merge(universe, on="ticker", how="left")
    # Drop rows where we lost the name (universe drift)
    merged["name"] = merged["name"].fillna(merged["ticker"])
    merged["market_cap"] = merged["market_cap"].fillna(0)

    asof = prices["date"].max()
    print(f"As of: {asof}", file=sys.stderr)

    result = {
        "asof": asof,
        "ticker_count": int(merged["ticker"].nunique()),
        "universe_target": int(len(universe)),
        "yesterday_top10": top_n(merged, "ret_daily", 10, ascending=False),
        "yesterday_worst10": top_n(merged, "ret_daily", 10, ascending=True),
        "mom_1w_top10": top_n(merged, "ret_w1", 10, ascending=False),
        "mom_1m_top10": top_n(merged, "ret_m1", 10, ascending=False),
        "mom_3m_top10": top_n(merged, "ret_m3", 10, ascending=False),
        "mom_12_1_top10": top_n(merged, "ret_12_1", 10, ascending=False),
        "all_tickers": [
            {
                "ticker": r["ticker"],
                "name": r["name"],
                "market_cap": int(r["market_cap"]) if pd.notna(r["market_cap"]) else 0,
                "exchange": r.get("exchange", ""),
                "close": round(float(r["close"]), 2) if pd.notna(r["close"]) else None,
                "daily": _round(r.get("ret_daily")),
                "w1": _round(r.get("ret_w1")),
                "m1": _round(r.get("ret_m1")),
                "m3": _round(r.get("ret_m3")),
                "m12_1": _round(r.get("ret_12_1")),
            }
            for _, r in merged.iterrows()
        ],
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)", file=sys.stderr)
    return 0


def _round(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return round(float(v), 2)


if __name__ == "__main__":
    sys.exit(main())
