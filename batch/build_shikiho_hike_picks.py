"""四季報型「増配期待バリュー9条件」× 直近決算の増配 のピックを作る。

dividend_screener.json の全銘柄に9条件を当て、data/dividend_hikes_jp.json の
「直近 RECENT_DAYS 日に増配・配当予想の増額修正を発表した銘柄」と突き合わせる。

  A: 判定できる8条件(有利子負債はデータなしで未判定)をすべて満たし、直近に増配
  B: 1条件だけ外れたが、直近に増配

結果は dividend_screener.json の "shikiho_hike_picks" に書き足す。
build_dividend_screener.py の後に走らせること(通信なし)。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENER = REPO_ROOT / "web" / "data" / "dividend_screener.json"
HIKES = REPO_ROOT / "data" / "dividend_hikes_jp.json"
JST = timezone(timedelta(hours=9))
RECENT_DAYS = 120  # 中間決算〜本決算の1シーズン分
MAX_A = 15
MAX_B = 8

# Bグループで「外れた条件」として表に出す言い方
MISS = {
    "per": "PER15倍超", "pbr": "PBR1.5倍超", "roe": "ROE8%未満", "yield": "利回り3%未満",
    "payout": "配当性向50%超", "nocut": "減配なしが5期未満", "fwd": "今期は減益予想",
    "mcap": "時価総額100億円未満",
}

CONDS = [
    ("per", "PER15倍以下", lambda s: s.get("per") is not None and 0 < s["per"] <= 15),
    ("pbr", "PBR1.5倍以下", lambda s: s.get("pbr") is not None and s["pbr"] <= 1.5),
    ("roe", "ROE8%以上", lambda s: (s.get("roe") or 0) >= 8),
    ("yield", "利回り3%以上", lambda s: (s.get("yield") or 0) >= 3),
    ("payout", "配当性向50%以下", lambda s: s.get("payout") is not None and 0 < s["payout"] <= 50),
    ("nocut", "5期以上減配なし", lambda s: ((s.get("streak") or {}).get("non_decreasing") or 0) >= 5),
    ("fwd", "減益予想でない", lambda s: not s.get("fwdWeak")),
    ("mcap", "時価総額100億円以上", lambda s: (s.get("mcap") or 0) >= 100),
]


def main() -> int:
    scr = json.loads(SCREENER.read_text(encoding="utf-8"))
    if not HIKES.exists():
        print("no dividend_hikes_jp.json — skip")
        return 0
    events = json.loads(HIKES.read_text(encoding="utf-8")).get("events", [])
    cutoff = (datetime.now(JST) - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    latest: dict[str, dict] = {}
    for e in events:
        if e["date"] < cutoff:
            continue
        if e["code"] not in latest or e["date"] > latest[e["code"]]["date"]:
            latest[e["code"]] = e

    a, b = [], []
    for s in scr.get("stocks", []):
        ev = latest.get(s["code"])
        if not ev:
            continue
        failed = [MISS[k] for k, _, f in CONDS if not f(s)]
        # 高配当狙いなので、利回り3%未満はBにも入れない
        if len(failed) > 1 or "利回り3%未満" in failed:
            continue
        st = s.get("streak") or {}
        row = {
            "code": s["code"], "name": s["name"], "sector": s.get("sector"),
            "yield": s.get("yield"), "per": s.get("per"), "pbr": s.get("pbr"),
            "roe": s.get("roe"), "payout": s.get("payout"),
            "streak_years": st.get("years"), "non_decreasing": st.get("non_decreasing"),
            "streak_label": st.get("label"), "headroom": s.get("headroom"),
            "hike_date": ev["date"], "hike_detail": ev["detail"],
            "earnings_up": ev.get("earnings_up", False),
            "failed": failed[0] if failed else None,
        }
        (b if failed else a).append(row)

    def key(r):
        return (-(r["headroom"] or 0), -(r["streak_years"] or 0), -(r["yield"] or 0))

    a.sort(key=key)
    b.sort(key=key)
    scr["shikiho_hike_picks"] = {
        "asof": scr.get("asof"),
        "conditions": [label for _, label, _ in CONDS] + ["有利子負債が自己資本以下(データなしのため未判定)"],
        "recent_days": RECENT_DAYS,
        "hike_events": len(latest),
        "a": a[:MAX_A],
        "b": b[:MAX_B],
    }
    SCREENER.write_text(json.dumps(scr, ensure_ascii=False), encoding="utf-8")
    print(f"shikiho×hike picks: A={len(a)} B={len(b)} (hike events {len(latest)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
