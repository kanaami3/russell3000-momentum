"""Dividend-stock screener: fetch metrics for a watchlist and assign 4 judgments.

Reads batch/dividend_watchlist.json (code / name / sector), fetches per-stock
metrics via yfinance, and computes 4 machine judgments (◎○△×):

  割安判定 (undervalued): PER & PBR の水準
  配当判定 (dividend):     利回り & 配当性向の持続性
  業績判定 (earnings):     ROE & 増益率
  総合判定 (overall):      上記3つの平均

Output: web/data/dividend_screener.json

All thresholds are explicit and easy to tweak (see JUDGE_* functions).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    from curl_cffi import requests as _http
    _SESSION = _http.Session(impersonate="chrome")
except ImportError:
    _SESSION = None

import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = Path(__file__).resolve().parent / "dividend_watchlist.json"
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "dividend_screener.json"

# 判定記号(値が大きいほど良い)
MARKS = {4: "◎", 3: "○", 2: "△", 1: "×"}


def _num(v):
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 判定ロジック(しきい値はここを編集すれば調整可能)
# ---------------------------------------------------------------------------

def judge_value(per, pbr):
    """割安判定: PER・PBR が低いほど良い。"""
    if per is None or pbr is None or per <= 0:
        return None
    if per < 12 and pbr < 1.0:   return 4  # ◎ 両方割安
    if per < 15 and pbr < 1.5:   return 3  # ○ 妥当
    if per < 20 and pbr < 2.5:   return 2  # △ やや割高
    return 1                                # × 割高


def judge_dividend(yield_pct, payout):
    """配当判定: 高利回り × 無理のない配当性向 = 良い。"""
    if yield_pct is None:
        return None
    p = payout if payout is not None else None
    # payout は 0-1 の比率(例 0.40)。100%超は持続性に懸念。
    if yield_pct >= 3.5 and (p is None or p <= 0.60):   return 4  # ◎
    if yield_pct >= 2.5 and (p is None or p <= 0.70):   return 3  # ○
    if yield_pct >= 1.5 and (p is None or p <= 1.00):   return 2  # △
    return 1                                                       # ×


def judge_earnings(roe, eg, rg):
    """業績判定: ROE と増益率。"""
    if roe is None and eg is None:
        return None
    roe_pct = roe * 100 if roe is not None else None
    growth_pos = (eg is not None and eg > 0) or (rg is not None and rg > 0)
    growth_neg = (eg is not None and eg < -0.20)
    if roe_pct is not None and roe_pct >= 10 and growth_pos:   return 4  # ◎
    if roe_pct is not None and roe_pct >= 7 and not growth_neg: return 3  # ○
    if roe_pct is not None and roe_pct >= 3:                    return 2  # △
    if growth_pos:                                             return 2  # △
    return 1                                                             # ×


def overall(vals):
    got = [v for v in vals if v is not None]
    if not got:
        return None
    return round(sum(got) / len(got))


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

def fetch_one(entry: dict) -> dict | None:
    sym = entry["code"] + ".T"
    t = yf.Ticker(sym, session=_SESSION) if _SESSION else yf.Ticker(sym)
    try:
        info = t.info or {}
    except Exception as e:
        print(f"  WARN {sym}: {e}", file=sys.stderr)
        info = {}

    price = _num(info.get("currentPrice")) or _num(info.get("regularMarketPrice"))
    if price is None:
        print(f"  SKIP {sym}: no price", file=sys.stderr)
        return None

    yld = _num(info.get("dividendYield"))            # 既に %
    div = _num(info.get("dividendRate"))             # 予想年間配当
    payout = _num(info.get("payoutRatio"))           # 0-1
    per = _num(info.get("trailingPE"))
    pbr = _num(info.get("priceToBook"))
    roe = _num(info.get("returnOnEquity"))
    eg = _num(info.get("earningsGrowth"))
    rg = _num(info.get("revenueGrowth"))
    fye = info.get("lastFiscalYearEnd")
    fmonth = None
    if fye:
        try:
            fmonth = datetime.fromtimestamp(fye, tz=timezone.utc).month
        except Exception:
            fmonth = None

    j_val = judge_value(per, pbr)
    j_div = judge_dividend(yld, payout)
    j_ern = judge_earnings(roe, eg, rg)
    j_all = overall([j_val, j_div, j_ern])

    return {
        "code": entry["code"],
        "name": entry["name"],
        "sector": entry["sector"],
        "yield": round(yld, 2) if yld is not None else None,
        "dividend": round(div) if div is not None else None,
        "price": round(price) if price is not None else None,
        "payout": round(payout * 100, 1) if payout is not None else None,
        "per": round(per, 2) if per is not None else None,
        "pbr": round(pbr, 2) if pbr is not None else None,
        "roe": round(roe * 100, 1) if roe is not None else None,
        "fmonth": fmonth,
        "judge": {"overall": j_all, "value": j_val, "dividend": j_div, "earnings": j_ern},
    }


def main() -> int:
    watch = json.loads(WATCHLIST.read_text(encoding="utf-8"))
    print(f"Screening {len(watch)} stocks...", file=sys.stderr)
    rows = []
    for i, e in enumerate(watch, 1):
        r = fetch_one(e)
        if r:
            rows.append(r)
        if i % 10 == 0:
            print(f"  {i}/{len(watch)}", file=sys.stderr)

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    out = {
        "asof": now_jst.date().isoformat(),
        "generated_at_jst": now_jst.isoformat(),
        "source": "yfinance",
        "marks": MARKS,
        "criteria": {
            "value": "割安判定: PER<12&PBR<1.0=◎ / <15&<1.5=○ / <20&<2.5=△ / それ以上=×",
            "dividend": "配当判定: 利回り≥3.5%&性向≤60%=◎ / ≥2.5%&≤70%=○ / ≥1.5%&≤100%=△ / それ以下=×",
            "earnings": "業績判定: ROE≥10%&増益=◎ / ≥7%=○ / ≥3%=△ / それ以下=×",
            "overall": "総合判定: 割安・配当・業績の平均",
        },
        "stocks": rows,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH} ({len(rows)} stocks)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
