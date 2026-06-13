"""Fetch Nikkei 225 official PER → derive EPS, accumulate a daily history.

The Nikkei publishes the daily NK225 PER (weighted-average) at
  https://indexes.nikkei.co.jp/nkave/archives/data?list=per
but only the last ~10 trading days are scrapeable; older months are not.
So we scrape what's available each day and append to a JSON history file.

Daily prices (close) are taken from the Nikkei's official daily CSV
  https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv
which goes back to 2023.

EPS for a given day = close / weighted_average_PER.

YoY EPS change uses the value ~252 trading days (≈1 year) earlier.

Output: web/data/nikkei_eps_history.json
"""

from __future__ import annotations

import io
import json
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:
    import urllib.request
    _SESSION = None


REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "nikkei_eps_history.json"

PER_URL   = "https://indexes.nikkei.co.jp/nkave/archives/data?list=per"
DAILY_CSV = "https://indexes.nikkei.co.jp/nkave/historical/nikkei_stock_average_daily_jp.csv"


def _get(url: str) -> str:
    if _SESSION is not None:
        return _SESSION.get(url, timeout=20).text
    return urllib.request.urlopen(url, timeout=20).read().decode("utf-8", "ignore")


# ---------------------------------------------------------------------------
# Scrapers
# ---------------------------------------------------------------------------

def scrape_per_table() -> list[dict]:
    """Returns [{date: 'YYYY-MM-DD', per_weighted: float, per_index_based: float}, ...]"""
    html = _get(PER_URL)
    rows: list[dict] = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", tr, re.DOTALL)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        cells = [c for c in cells if c]
        if not cells or not re.match(r"^\d{4}\.\d{2}\.\d{2}$", cells[0]):
            continue
        try:
            iso_date = cells[0].replace(".", "-")
            rows.append({
                "date": iso_date,
                "per_weighted":    float(cells[1].replace(",", "")),
                "per_index_based": float(cells[2].replace(",", "")) if len(cells) > 2 else None,
            })
        except ValueError:
            continue
    return rows


def fetch_close_prices() -> dict[str, float]:
    """Return {iso_date: close_float} from Nikkei's daily CSV (2023-)."""
    # The official CSV is Shift-JIS encoded. We don't need the header so any
    # decode works for the data rows (numbers + ISO-ish dates).
    if _SESSION is not None:
        raw = _SESSION.get(DAILY_CSV, timeout=20).content
    else:
        raw = urllib.request.urlopen(DAILY_CSV, timeout=20).read()
    try:
        text = raw.decode("shift_jis")
    except UnicodeDecodeError:
        text = raw.decode("cp932", "ignore")
    closes: dict[str, float] = {}
    for line in text.splitlines():
        # Each row: "YYYY/MM/DD","close","open","high","low"
        m = re.match(r'^"(\d{4})/(\d{2})/(\d{2})","([\d,.]+)"', line)
        if not m:
            continue
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        try:
            closes[iso] = float(m.group(4).replace(",", ""))
        except ValueError:
            continue
    return closes


# ---------------------------------------------------------------------------
# Aggregator
# ---------------------------------------------------------------------------

def merge_history(existing: list[dict], new_rows: list[dict], closes: dict) -> list[dict]:
    """Update history with the latest PER scrape + ensure close is filled in."""
    by_date: dict[str, dict] = {h["date"]: h for h in existing}
    for r in new_rows:
        d = r["date"]
        merged = dict(by_date.get(d, {}))
        merged.update(r)
        merged["close"] = closes.get(d) or merged.get("close")
        if merged.get("close") and merged.get("per_weighted"):
            merged["eps_weighted"] = round(merged["close"] / merged["per_weighted"], 2)
        if merged.get("close") and merged.get("per_index_based"):
            merged["eps_index_based"] = round(merged["close"] / merged["per_index_based"], 2)
        by_date[d] = merged

    # Also ensure existing rows have close filled if missing (CSV catches up later)
    for d, h in list(by_date.items()):
        if not h.get("close") and d in closes:
            h["close"] = closes[d]
            if h.get("per_weighted"):
                h["eps_weighted"] = round(h["close"] / h["per_weighted"], 2)
            if h.get("per_index_based"):
                h["eps_index_based"] = round(h["close"] / h["per_index_based"], 2)

    return sorted(by_date.values(), key=lambda x: x["date"])


def compute_yoy(history: list[dict]) -> None:
    """Add eps_weighted_yoy_pct to each row using ~252 trading days earlier."""
    by_date = {h["date"]: h for h in history}
    dates = [h["date"] for h in history]
    eps_lookup = {h["date"]: h.get("eps_weighted") for h in history if h.get("eps_weighted")}

    for h in history:
        cur = h.get("eps_weighted")
        if not cur:
            h["eps_weighted_yoy_pct"] = None
            continue
        # 1-year-ago target = same calendar date 1 year ago; fall back to
        # the closest prior date with EPS data.
        d = datetime.strptime(h["date"], "%Y-%m-%d")
        target = d.replace(year=d.year - 1)
        # Walk backward to find nearest available date
        prior = None
        for delta in range(0, 31):
            cand = target.strftime("%Y-%m-%d")
            if cand in eps_lookup:
                prior = eps_lookup[cand]
                break
            target = target.replace(day=max(1, target.day - 1))
            if target.day == 1:
                break
        if prior:
            h["eps_weighted_yoy_pct"] = round((cur / prior - 1) * 100, 2)
        else:
            h["eps_weighted_yoy_pct"] = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Fetching Nikkei PER table...", file=sys.stderr)
    try:
        per_rows = scrape_per_table()
        print(f"  {len(per_rows)} PER rows scraped", file=sys.stderr)
    except Exception as e:
        print(f"  ERROR PER scrape: {e}", file=sys.stderr)
        per_rows = []

    print("Fetching Nikkei daily price CSV...", file=sys.stderr)
    try:
        closes = fetch_close_prices()
        print(f"  {len(closes)} daily close rows", file=sys.stderr)
    except Exception as e:
        print(f"  ERROR CSV: {e}", file=sys.stderr)
        closes = {}

    if not per_rows:
        print("ERROR: no PER data; aborting", file=sys.stderr)
        return 1

    existing: list[dict] = []
    if OUTPUT_PATH.exists():
        try:
            existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8")).get("history", [])
        except Exception:
            existing = []

    history = merge_history(existing, per_rows, closes)
    compute_yoy(history)

    latest = history[-1] if history else {}
    output = {
        "asof": latest.get("date", ""),
        "generated_at_jst": datetime.now().astimezone().isoformat(),
        "source": "Nikkei Indexes (https://indexes.nikkei.co.jp)",
        "metric_notes": {
            "per_weighted":    "加重平均PER(各構成銘柄のPERを時価総額加重平均)",
            "per_index_based": "指数ベースPER(NK225採用銘柄を1株とみなした計算)",
            "eps_weighted":    "close / per_weighted",
            "eps_index_based": "close / per_index_based",
            "eps_weighted_yoy_pct": "1年前(~252営業日前の同月日)との比較(%)",
        },
        "history": history,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB, {len(history)} rows)", file=sys.stderr)
    if latest:
        print(f"  Latest {latest['date']}: close ¥{latest.get('close',0):,.2f} "
              f"PER(加重平均) {latest.get('per_weighted','-')} "
              f"EPS ¥{latest.get('eps_weighted','-')} "
              f"YoY {latest.get('eps_weighted_yoy_pct','-')}%",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
