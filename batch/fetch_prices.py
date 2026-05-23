"""Fetch daily close prices for the universe via yfinance.

Strategy:
- Read tickers from data/universe.json
- Batch download in chunks of 100 tickers, ~14 months of history
- Persist long-format CSV: ticker, date, close
- Resilient: skip failed tickers, retry once per batch

Output: data/prices.csv (long format)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "universe.json"
OUTPUT_PATH = REPO_ROOT / "data" / "prices.csv"

BATCH_SIZE = 100
PERIOD = "14mo"  # enough for 12-month + 1-month lookback with buffer


def load_universe() -> list[str]:
    data = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    return [row["ticker"] for row in data]


def fetch_batch(tickers: list[str]) -> pd.DataFrame:
    """Download a chunk of tickers and return long-format (ticker, date, close)."""
    df = yf.download(
        tickers=tickers,
        period=PERIOD,
        interval="1d",
        auto_adjust=True,  # use split/dividend-adjusted closes
        progress=False,
        group_by="ticker",
        threads=True,
    )
    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "close"])

    records: list[dict] = []
    if isinstance(df.columns, pd.MultiIndex):
        # Multi-ticker case: top level is ticker
        for ticker in tickers:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna(subset=["Close"])
            for date, row in sub.iterrows():
                records.append(
                    {"ticker": ticker, "date": date.date().isoformat(), "close": float(row["Close"])}
                )
    else:
        # Single-ticker case: flat columns
        sub = df.dropna(subset=["Close"])
        ticker = tickers[0]
        for date, row in sub.iterrows():
            records.append(
                {"ticker": ticker, "date": date.date().isoformat(), "close": float(row["Close"])}
            )
    return pd.DataFrame.from_records(records)


def main() -> int:
    tickers = load_universe()
    print(f"Loaded {len(tickers)} tickers", file=sys.stderr)

    all_frames: list[pd.DataFrame] = []
    n_batches = (len(tickers) + BATCH_SIZE - 1) // BATCH_SIZE
    for i in range(0, len(tickers), BATCH_SIZE):
        batch = tickers[i : i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        try:
            t0 = time.time()
            df = fetch_batch(batch)
            elapsed = time.time() - t0
            print(
                f"  [{batch_idx}/{n_batches}] {batch[0]}..{batch[-1]}: "
                f"{df['ticker'].nunique() if not df.empty else 0}/{len(batch)} tickers, "
                f"{len(df)} rows ({elapsed:.1f}s)",
                file=sys.stderr,
            )
            if not df.empty:
                all_frames.append(df)
        except Exception as e:
            print(f"  [{batch_idx}/{n_batches}] ERROR: {e}", file=sys.stderr)
        time.sleep(0.5)  # gentle pacing

    if not all_frames:
        print("ERROR: no price data fetched", file=sys.stderr)
        return 1

    out = pd.concat(all_frames, ignore_index=True)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUTPUT_PATH, index=False)
    print(
        f"Wrote {len(out):,} rows ({out['ticker'].nunique()} tickers) to {OUTPUT_PATH}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
