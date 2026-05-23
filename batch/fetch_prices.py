"""Fetch daily close prices for the chosen market's universe via yfinance.

Usage:
    python batch/fetch_prices.py us       # default
    python batch/fetch_prices.py jp

Input:  data/universe_{market}.json
Output: data/prices_{market}.csv (long-format: ticker, date, close)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent

BATCH_SIZE = 100
PERIOD = "14mo"


def load_universe(market: str) -> list[str]:
    path = REPO_ROOT / "data" / f"universe_{market}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [row["ticker"] for row in data]


def fetch_batch(tickers: list[str]) -> pd.DataFrame:
    df = yf.download(
        tickers=tickers,
        period=PERIOD,
        interval="1d",
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    if df.empty:
        return pd.DataFrame(columns=["ticker", "date", "close"])

    records: list[dict] = []
    if isinstance(df.columns, pd.MultiIndex):
        for ticker in tickers:
            if ticker not in df.columns.get_level_values(0):
                continue
            sub = df[ticker].dropna(subset=["Close"])
            for date, row in sub.iterrows():
                records.append(
                    {"ticker": ticker, "date": date.date().isoformat(), "close": float(row["Close"])}
                )
    else:
        sub = df.dropna(subset=["Close"])
        ticker = tickers[0]
        for date, row in sub.iterrows():
            records.append(
                {"ticker": ticker, "date": date.date().isoformat(), "close": float(row["Close"])}
            )
    return pd.DataFrame.from_records(records)


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "us").lower()
    if market not in ("us", "jp"):
        print(f"ERROR: market must be 'us' or 'jp', got '{market}'", file=sys.stderr)
        return 1

    output_path = REPO_ROOT / "data" / f"prices_{market}.csv"
    tickers = load_universe(market)
    print(f"[{market.upper()}] Loaded {len(tickers)} tickers", file=sys.stderr)

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
        time.sleep(0.5)

    if not all_frames:
        print("ERROR: no price data fetched", file=sys.stderr)
        return 1

    out = pd.concat(all_frames, ignore_index=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_path, index=False)
    print(
        f"[{market.upper()}] Wrote {len(out):,} rows ({out['ticker'].nunique()} tickers) to {output_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
