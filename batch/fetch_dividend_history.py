"""Fetch per-stock dividend history for the JP universe.

Deliberately separate from build_dividend_screener.py, which is a no-network
script by design. Dividend history changes at most twice a year per company,
so refetching it daily would spend 800+ API calls to learn nothing. This runs
weekly and leaves a CSV that the daily build reads for free.

Input:  data/value_data_jp.csv          (the universe the value pipeline builds)
Output: data/dividend_history_jp.csv    long format: code, ex_date, amount

Failures are tolerated per ticker. A stock we cannot fetch simply has no rows,
and the screener reports it as 判定なし rather than as a dividend cut — telling
the two apart matters more here than coverage.
"""

from __future__ import annotations

import csv
import os
import sys
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = REPO_ROOT / "data" / "value_data_jp.csv"
OUTPUT_CSV = REPO_ROOT / "data" / "dividend_history_jp.csv"

BATCH_SIZE = int(os.getenv("DIV_BATCH_SIZE", "40"))
BATCH_PAUSE_SEC = float(os.getenv("DIV_BATCH_PAUSE_SEC", "2.0"))

# 花王は35期連続増配。窓を短く切ると記録が途中で切れて実態を下回るので、
# 取れるだけ取る。配当明細は1銘柄あたり年2行しかなく、40年分でも80行で済む。
YEARS_BACK = int(os.getenv("DIV_YEARS_BACK", "40"))


def read_universe() -> list[str]:
    if not INPUT_CSV.exists():
        print(f"{INPUT_CSV} がありません。fetch_value_data.py を先に流してください。",
              file=sys.stderr)
        return []
    codes: list[str] = []
    with INPUT_CSV.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            code = (row.get("code") or row.get("Code") or row.get("ticker") or "").strip()
            code = code.replace(".T", "")
            if code:
                codes.append(code)
    return list(dict.fromkeys(codes))   # 重複除去（順序は維持）


def fetch_one(code: str) -> pd.Series | None:
    try:
        div = yf.Ticker(f"{code}.T").dividends
    except Exception:
        return None
    if div is None or len(div) == 0:
        return None
    div = div.copy()
    div.index = pd.to_datetime(div.index).tz_localize(None)
    cutoff = pd.Timestamp.utcnow().tz_localize(None) - pd.DateOffset(years=YEARS_BACK)
    return div[div.index >= cutoff]


def main() -> int:
    codes = read_universe()
    if not codes:
        return 1

    rows: list[tuple[str, str, float]] = []
    failed: list[str] = []

    for i in range(0, len(codes), BATCH_SIZE):
        for code in codes[i:i + BATCH_SIZE]:
            div = fetch_one(code)
            if div is None:
                failed.append(code)
                continue
            for ex_date, amount in div.items():
                rows.append((code, ex_date.date().isoformat(), float(amount)))
        done = min(i + BATCH_SIZE, len(codes))
        print(f"  {done}/{len(codes)} 銘柄 … 行数 {len(rows)}", flush=True)
        if done < len(codes):
            time.sleep(BATCH_PAUSE_SEC)

    if not rows:
        # 全滅した場合は既存のキャッシュを残す。空ファイルで上書きすると、
        # 翌日のビルドで全銘柄が「判定なし」になり、原因も分からなくなる。
        print("1件も取得できませんでした。既存のキャッシュを維持します。", file=sys.stderr)
        return 1

    OUTPUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUTPUT_CSV.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["code", "ex_date", "amount"])
        w.writerows(rows)
    os.replace(tmp, OUTPUT_CSV)

    covered = len(codes) - len(failed)
    print(f"完了: {covered}/{len(codes)} 銘柄, {len(rows)} 行 -> {OUTPUT_CSV.name}")
    if failed:
        print(f"取得できず: {len(failed)} 銘柄（例: {', '.join(failed[:10])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
