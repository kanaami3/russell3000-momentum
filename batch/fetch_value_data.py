"""Fetch fundamental data for JP TSE Prime stocks for value-investing screen.

Pulls per-ticker info via yfinance:
  - dividendYield, payoutRatio
  - trailingPE, priceToBook
  - returnOnEquity, returnOnAssets
  - revenueGrowth, earningsGrowth, operatingMargins, profitMargins
  - debtToEquity (used as proxy for 自己資本比率)
  - marketCap, currentPrice

Output: data/value_data_jp.csv (raw, for calc step)
"""

from __future__ import annotations

import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "universe_jp.json"
OUTPUT_PATH = REPO_ROOT / "data" / "value_data_jp.csv"

FIELDS = [
    "ticker",
    "name",
    "sector17",
    "market_cap",
    "current_price",
    "dividend_yield",       # %
    "payout_ratio",         # %
    "trailing_pe",
    "price_to_book",
    "return_on_equity",     # %
    "return_on_assets",     # %
    "revenue_growth",       # % YoY
    "earnings_growth",      # % YoY
    "operating_margins",    # %
    "profit_margins",       # %
    "debt_to_equity",       # ratio
    "beta",
    "trailing_eps",         # 実績EPS
    "forward_eps",          # 予想EPS(改定方向の検知に使う)
]

# Concurrency settings
MAX_WORKERS = 8        # yfinance is OK with light parallelism
PROGRESS_EVERY = 100


def _safe_num(v) -> float | None:
    """Convert yfinance value to plain float or None."""
    if v is None:
        return None
    try:
        f = float(v)
        if f != f:  # NaN check
            return None
        return f
    except (TypeError, ValueError):
        return None


def fetch_one(spec: dict) -> dict:
    """Fetch info for a single ticker. Always returns a dict (with nones on failure)."""
    ticker = spec["ticker"]
    row = {
        "ticker": ticker,
        "name": spec.get("name", ticker),
        "sector17": spec.get("sector17", ""),
        "market_cap": None,
        "current_price": None,
        "dividend_yield": None,
        "payout_ratio": None,
        "trailing_pe": None,
        "price_to_book": None,
        "return_on_equity": None,
        "return_on_assets": None,
        "revenue_growth": None,
        "earnings_growth": None,
        "operating_margins": None,
        "profit_margins": None,
        "debt_to_equity": None,
        "beta": None,
        "trailing_eps": None,
        "forward_eps": None,
    }
    try:
        tk = yf.Ticker(ticker)
        info = tk.info or {}
        row["market_cap"] = _safe_num(info.get("marketCap"))

        # **株価は1つのキーに頼らない。**
        # currentPrice は日本株だと欠けていることが多く、ここが None になると
        # 後段(build_dividend_screener)が行ごと捨てる。実際それで東証プライム
        # 1,550銘柄のうち700銘柄ほどがスクリーナーから消えていた。
        for key in ("currentPrice", "regularMarketPrice",
                    "previousClose", "regularMarketPreviousClose"):
            row["current_price"] = _safe_num(info.get(key))
            if row["current_price"] is not None:
                break
        if row["current_price"] is None:
            # info に無くても fast_info なら返ることがある
            try:
                row["current_price"] = _safe_num(getattr(tk.fast_info, "last_price", None))
            except Exception:
                pass

        # yfinance unit conventions (verified):
        #   dividendYield   → already a percentage (3.35 = 3.35%)
        #   payoutRatio, returnOnEquity, returnOnAssets, revenueGrowth,
        #   earningsGrowth, operatingMargins, profitMargins → decimals (×100 needed)
        def _pct(v):  # decimal → percent
            x = _safe_num(v)
            return round(x * 100, 2) if x is not None else None

        row["dividend_yield"]    = _safe_num(info.get("dividendYield"))
        if row["dividend_yield"] is not None:
            row["dividend_yield"] = round(row["dividend_yield"], 2)
        row["payout_ratio"]      = _pct(info.get("payoutRatio"))
        row["trailing_pe"]       = _safe_num(info.get("trailingPE"))
        row["price_to_book"]     = _safe_num(info.get("priceToBook"))
        row["return_on_equity"]  = _pct(info.get("returnOnEquity"))
        row["return_on_assets"]  = _pct(info.get("returnOnAssets"))
        row["revenue_growth"]    = _pct(info.get("revenueGrowth"))
        row["earnings_growth"]   = _pct(info.get("earningsGrowth"))
        row["operating_margins"] = _pct(info.get("operatingMargins"))
        row["profit_margins"]    = _pct(info.get("profitMargins"))
        row["debt_to_equity"]    = _safe_num(info.get("debtToEquity"))
        row["beta"]              = _safe_num(info.get("beta"))
        row["trailing_eps"]      = _safe_num(info.get("trailingEps"))
        row["forward_eps"]       = _safe_num(info.get("forwardEps"))
    except Exception as e:
        # 1銘柄の失敗で全体を止めない。ただし握り潰すと原因が追えないので
        # 理由だけは残す（集計に使う）。
        row["fetch_error"] = type(e).__name__
    return row


def main() -> int:
    if not UNIVERSE_PATH.exists():
        print(f"ERROR: {UNIVERSE_PATH} not found", file=sys.stderr)
        return 1

    universe = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    print(f"Fetching value data for {len(universe)} JP Prime tickers...", file=sys.stderr)

    rows: list[dict] = []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(fetch_one, u): u["ticker"] for u in universe}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                rows.append(fut.result())
            except Exception as e:
                print(f"  [{i}] error {futs[fut]}: {e}", file=sys.stderr)
            if i % PROGRESS_EVERY == 0:
                elapsed = time.time() - t0
                print(f"  {i}/{len(universe)} done ({elapsed:.0f}s elapsed, {elapsed/i:.2f}s/ticker)", file=sys.stderr)

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8", newline="") as f:
        # fetch_error は診断用に行へ付けてあるだけなので CSV には出さない
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # **株価が取れなかった行の内訳を出す。**
    # 後段は株価の無い行を捨てるので、ここが欠けた数＝スクリーナーから
    # 消える銘柄数になる。黙って減っていると気づけない。
    no_price = [r for r in rows if r["current_price"] is None]
    errs = {}
    for r in no_price:
        k = r.get("fetch_error") or "株価フィールド無し"
        errs[k] = errs.get(k, 0) + 1
    if no_price:
        detail = " / ".join(f"{k}:{v}" for k, v in sorted(errs.items(), key=lambda x: -x[1]))
        print(f"  株価が取れなかった銘柄: {len(no_price)}/{len(rows)}  ({detail})",
              file=sys.stderr)
        print("  例: " + ", ".join(r["ticker"] for r in no_price[:10]), file=sys.stderr)

    # Quick coverage stats
    have_pe = sum(1 for r in rows if r["trailing_pe"] is not None)
    have_dy = sum(1 for r in rows if r["dividend_yield"] is not None)
    have_roe = sum(1 for r in rows if r["return_on_equity"] is not None)
    print(
        f"Wrote {len(rows)} rows to {OUTPUT_PATH} "
        f"(PE: {have_pe}, Div: {have_dy}, ROE: {have_roe}) in {time.time()-t0:.0f}s",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
