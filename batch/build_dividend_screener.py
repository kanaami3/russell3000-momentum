"""Full-universe dividend screener.

Reads the JP fundamentals table that fetch_value_data.py already produces daily
(data/value_data_jp.csv, ~1,500 TSE stocks) and assigns 4 machine judgments
(割安 / 配当 / 業績 / 総合) to every stock that has data. The frontend then
filters this full universe live by user-set conditions (yield≥, PBR≤, …).

No new network fetching — this reuses the value pipeline's CSV, so it must run
AFTER fetch_value_data.py.

Output: web/data/dividend_screener.json
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = REPO_ROOT / "data" / "value_data_jp.csv"
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "dividend_screener.json"

MARKS = {4: "◎", 3: "○", 2: "△", 1: "×"}


import math


def _f(v):
    try:
        if v in (None, "", "None"):
            return None
        f = float(v)
        if math.isnan(f) or math.isinf(f):  # JSではJSON.parse不可 → None
            return None
        return f
    except (TypeError, ValueError):
        return None


# --- 判定ロジック(fetch_dividend_screener.py と同一のしきい値)---

def judge_value(per, pbr):
    if per is None or pbr is None or per <= 0:
        return None
    if per < 12 and pbr < 1.0:  return 4
    if per < 15 and pbr < 1.5:  return 3
    if per < 20 and pbr < 2.5:  return 2
    return 1


def judge_dividend(yld, payout):
    # payout は既に%(例 40.0)。
    if yld is None:
        return None
    p = payout
    # 配当性向>100%(利益超の配当)は持続性に難 → 上限△。
    if p is not None and p > 100:
        return min(2, 2 if yld >= 1.5 else 1)
    # 超高利回り>8%は特別配当/減配リスク(罠)の可能性 → ◎にはせず○上限。
    if yld > 8:
        return 3 if (p is None or p <= 80) else 2
    if yld >= 3.5 and (p is None or p <= 60):   return 4
    if yld >= 2.5 and (p is None or p <= 70):   return 3
    if yld >= 1.5 and (p is None or p <= 100):  return 2
    return 1


def yield_caution(yld):
    """高利回りの注意フラグ(特別配当/減配リスクの可能性)。"""
    return yld is not None and yld > 8


def judge_earnings(roe, eg, rg, fwd_rev):
    # roe / eg / rg は既に%(例 ROE 8.88, 増益 31.4)。
    # fwd_rev = 予想EPS ÷ 実績EPS − 1(会社予想の改定方向。マイナス=減額)。
    if roe is None and eg is None and fwd_rev is None:
        return None
    # フォワード(今期予想)を最優先: 大幅減額は過去がよくても評価を下げる。
    if fwd_rev is not None and fwd_rev <= -0.25:  # 大幅減額
        return 1
    growth_pos = (eg is not None and eg > 0) or (rg is not None and rg > 0)
    growth_neg = (eg is not None and eg < -20)
    if roe is not None and roe >= 10 and growth_pos:    base = 4
    elif roe is not None and roe >= 7 and not growth_neg: base = 3
    elif roe is not None and roe >= 3:                    base = 2
    elif growth_pos:                                    base = 2
    else:                                              base = 1
    # 減額基調(予想EPSが実績比 -10%以下)なら △ 止まり。
    if fwd_rev is not None and fwd_rev <= -0.10:
        base = min(base, 2)
    return base


def eps_revision(fwd, trail):
    """予想EPS改定の方向。None/赤字は判定不能。"""
    if fwd is None or trail is None or trail <= 0:
        return None
    return fwd / trail - 1


def revision_reliable(rev):
    """改定率が現実的な範囲か。yfinanceの予想EPSは日本の中小型株で
    実績と桁違いの異常値になることがある(例 -82%)。極端な値は
    データ異常とみなし、業績判定には使わない。"""
    return rev is not None and -0.55 <= rev <= 1.50


def overall(vals):
    got = [v for v in vals if v is not None]
    return round(sum(got) / len(got)) if got else None


def main() -> int:
    if not INPUT_CSV.exists():
        print(f"ERROR: {INPUT_CSV} not found — run fetch_value_data.py first", file=sys.stderr)
        return 1

    rows = []
    with open(INPUT_CSV, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            price = _f(r.get("current_price"))
            per = _f(r.get("trailing_pe"))
            pbr = _f(r.get("price_to_book"))
            yld = _f(r.get("dividend_yield"))
            payout = _f(r.get("payout_ratio"))
            roe = _f(r.get("return_on_equity"))
            eg = _f(r.get("earnings_growth"))
            rg = _f(r.get("revenue_growth"))
            mcap = _f(r.get("market_cap"))
            fwd_eps = _f(r.get("forward_eps"))
            trail_eps = _f(r.get("trailing_eps"))
            # 価格が無い＝データ取得失敗行はスキップ
            if price is None:
                continue

            rev = eps_revision(fwd_eps, trail_eps)   # 予想EPS改定の方向
            rev_ok = revision_reliable(rev)          # 現実的な範囲か
            rev_eff = rev if rev_ok else None        # 判定に使うのは信頼できる時のみ
            j_val = judge_value(per, pbr)
            j_div = judge_dividend(yld, payout)
            j_ern = judge_earnings(roe, eg, rg, rev_eff)
            j_all = overall([j_val, j_div, j_ern])

            code = (r.get("ticker") or "").replace(".T", "")
            rows.append({
                "code": code,
                "name": r.get("name"),
                "sector": r.get("sector17"),
                "yield": round(yld, 2) if yld is not None else None,
                "price": round(price) if price is not None else None,
                "payout": round(payout, 1) if payout is not None else None,   # 既に%
                "per": round(per, 2) if per is not None else None,
                "pbr": round(pbr, 2) if pbr is not None else None,
                "roe": round(roe, 1) if roe is not None else None,            # 既に%
                "mcap": round(mcap / 1e8) if mcap is not None else None,  # 億円
                "rev": round(rev * 100, 1) if rev is not None else None,  # 予想EPS改定 %
                "revOk": rev_ok,  # 改定率が現実的な範囲か(判定に使ったか)
                "yieldWarn": yield_caution(yld),  # 超高利回り(要確認)
                # 予想EPSが異常 or 欠損 → 業績判定は過去ベースのみ(予想を織り込めず)
                "fwdWeak": (fwd_eps is None) or (not rev_ok and rev is not None),
                "judge": {"overall": j_all, "value": j_val, "dividend": j_div, "earnings": j_ern},
            })

    # 総合判定の高い順に並べておく(デフォルト表示用)
    rows.sort(key=lambda x: (x["judge"]["overall"] or 0, x["yield"] or 0), reverse=True)

    now_jst = datetime.now(timezone(timedelta(hours=9)))
    out = {
        "asof": now_jst.date().isoformat(),
        "generated_at_jst": now_jst.isoformat(),
        "source": "yfinance (JP universe via value pipeline)",
        "universe_size": len(rows),
        "marks": MARKS,
        "criteria": {
            "value": "割安: PER<12&PBR<1.0=◎ / <15&<1.5=○ / <20&<2.5=△ / それ以上=×",
            "dividend": "配当: 利回り≥3.5%&性向≤60%=◎ / ≥2.5%&≤70%=○ / ≥1.5%&≤100%=△ / それ以下=×",
            "earnings": "業績: ROE≥10%&増益=◎ / ≥7%=○ / ≥3%=△。予想EPSが実績比-25%以下(大幅減額)は×、-10%以下は△止まり",
            "overall": "総合: 3軸の平均",
        },
        "stocks": rows,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.0f} KB, {len(rows)} stocks)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
