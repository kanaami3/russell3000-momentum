"""Fetch Japan equity universe (TSE Prime market constituents) from JPX listed companies file.

Source: 日本取引所グループ (JPX) — 東証上場銘柄一覧
URL: https://www.jpx.co.jp/markets/statistics-equities/misc/01.html
The Excel is updated monthly. Free, no auth required.

Output: data/universe_jp.json
    [{ticker, code, name, sector33, sector17, size_cat}, ...]

Tickers use the yfinance convention: 4-digit code + ".T" (e.g. "7203.T" for Toyota).
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pandas as pd
import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "data" / "universe_jp.json"

JPX_XLS_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xls"
)
TARGET_SEGMENT = "プライム（内国株式）"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
    ),
}


def main() -> int:
    print(f"Fetching JPX listed companies file...", file=sys.stderr)
    resp = requests.get(JPX_XLS_URL, headers=HEADERS, timeout=60)
    resp.raise_for_status()

    df = pd.read_excel(io.BytesIO(resp.content))
    print(f"  Total rows: {len(df)}", file=sys.stderr)

    # Filter to Prime market (domestic equities)
    prime = df[df["市場・商品区分"] == TARGET_SEGMENT].copy()
    print(f"  Prime equities: {len(prime)}", file=sys.stderr)

    universe: list[dict] = []
    for _, row in prime.iterrows():
        code = str(row["コード"]).strip()
        if not code or code == "-":
            continue
        # JPX codes are typically 4 digits, sometimes 5 (newer issues / preferreds).
        # yfinance uses the format "CODE.T" for Tokyo Stock Exchange.
        ticker = f"{code}.T"
        universe.append(
            {
                "ticker": ticker,
                "code": code,
                "name": str(row["銘柄名"]).strip(),
                "sector33": str(row.get("33業種区分", "")).strip(),
                "sector17": str(row.get("17業種区分", "")).strip(),
                "size_cat": str(row.get("規模区分", "")).strip(),
            }
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(universe, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {len(universe)} tickers to {OUTPUT_PATH}", file=sys.stderr)
    if universe:
        print(f"  first: {universe[0]['ticker']} {universe[0]['name']}", file=sys.stderr)
        print(f"  last:  {universe[-1]['ticker']} {universe[-1]['name']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
