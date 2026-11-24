"""四季報型「増配期待バリュー9条件」× 期中の配当増額修正 のピックを作る。

dividend_screener.json の銘柄のうち、直近 RECENT_DAYS 日に**期中の配当予想を増額修正**
した銘柄に9条件を当てる。各条件は開示データで確かめられたときだけ合格・不合格を付け、
確かめられない条件が残る銘柄はA/B/Cに入れず「要確認」に分ける。

  A: 判定できる8条件(有利子負債はデータなしで未判定)をすべて満たした
  B: 1条件だけ外れた
  C: 2条件外れた(参考枠)
  要確認: 条件のどれかを開示データで確かめられない、または記念・特別配当の扱いが
          自動で判断できない

2026-11 に「何でもかんでも出している」と指摘されて厳しくした点:
  - 増配イベントは期中の増額修正だけ。決算短信で「今期予想が前期実績より多い」
    だけのもの(前期比の増配予想)は、修正ではないので数えない
  - 配当は普通配当で比べる。記念・特別配当は短信の注記から差し引く
  - 利回り・配当性向は会社予想(TDnet)。配当性向 = 普通配当予想 ÷ 1株利益予想
  - 条件7は会社の予想EPS ≥ 前期の実績EPS(純利益や経常利益の前期比ではない)
  - ROE は前期の実績(純利益 ÷ 平均自己資本)。yfinance の直近12か月ではない
  - PER は株価 ÷ 会社の予想EPS

結果は dividend_screener.json の "shikiho_hike_picks" に書き足す。
build_dividend_screener.py の後に走らせること(通信なし)。
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCREENER = REPO_ROOT / "web" / "data" / "dividend_screener.json"
HIKES = REPO_ROOT / "data" / "dividend_hikes_jp.json"
FORECASTS = REPO_ROOT / "data" / "dividend_forecast_jp.json"
ACTUALS = REPO_ROOT / "data" / "fy_actuals_jp.json"
JST = timezone(timedelta(hours=9))
RECENT_DAYS = 120  # 中間決算〜本決算の1シーズン分
FORECAST_FRESH_DAYS = 200
MAX_A = 100
MAX_B = 60
MAX_C = 40
MAX_REVIEW = 60

CONDITIONS = [
    "PER15倍以下(予想)", "PBR1.5倍以下", "ROE8%以上(前期実績)", "利回り3%以上(普通配当予想)",
    "配当性向50%以下(普通配当予想÷予想EPS)", "5期以上減配なし", "減益予想でない(予想EPS≧前期実績EPS)",
    "時価総額100億円以上", "有利子負債が自己資本以下(データなしのため未判定)",
]

# 期中の配当予想の増額修正と読めるタイトル。「決算短信」だけのものは含めない。
REVISION_TITLE = re.compile(r"配当予想.*(修正|増額)|増配|増額修正|配当.*増額|配当.*引き上げ")
SPECIAL_TITLE = re.compile(r"記念配当|特別配当")


def is_revision(e: dict) -> bool:
    """期中の増額修正のイベントか。前期比の増配予想(決算短信)だけのものは False。"""
    if e.get("source") == "TDnet XBRL":
        return e.get("kind") == "revision"
    detail = e.get("detail", "")
    if e.get("source") == "TDnet":
        return bool(REVISION_TITLE.search(detail)) and "決算短信" not in detail
    return "増額" in detail  # 手動収集(決算まとめ)は「配当をN円増額」の形


def revision_events(now: datetime | None = None) -> dict[str, dict]:
    """直近 RECENT_DAYS 日の、銘柄ごとの最新の増額修正イベント。"""
    if not HIKES.exists():
        return {}
    now = now or datetime.now(JST)
    cutoff = (now - timedelta(days=RECENT_DAYS)).strftime("%Y-%m-%d")
    latest: dict[str, dict] = {}
    for e in json.loads(HIKES.read_text(encoding="utf-8")).get("events", []):
        if e["date"] < cutoff or not is_revision(e):
            continue
        if e["code"] not in latest or e["date"] > latest[e["code"]]["date"]:
            latest[e["code"]] = e
    return latest


def _load(path: Path, key: str) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(key, {}) or {}
    except ValueError:
        return {}


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def evaluate(s: dict, ev: dict, fc: dict | None, act: dict | None) -> dict:
    """9条件を開示データで判定する。

    戻り値: {"failed": [...], "unknown": [...], "review": [...], "cut": bool, 値...}
    failed=不合格、unknown=データで確かめられない条件、review=人が判断すべき事情。
    """
    failed: list[str] = []
    unknown: list[str] = []
    review: list[str] = []
    price = s.get("price")

    # --- 配当(普通配当) ---
    dps = prior = eps_f = None
    if fc and _num(fc.get("dps")):
        dps = fc["dps"] - (fc.get("special_cur") or 0)
        if _num(fc.get("prior")):
            prior = fc["prior"] - (fc.get("special_prior") or 0)
        if fc.get("special_note") and "special_prior" not in fc:
            review.append("記念・特別配当の注記あり(金額を自動で読めず)")
    if _num(fc.get("eps") if fc else None):
        eps_f = fc["eps"]
    if SPECIAL_TITLE.search(ev.get("detail", "")):
        review.append("増額が記念・特別配当の可能性(開示タイトル)")

    cut = dps is not None and prior is not None and dps < prior - 1e-6
    yld = round(dps / price * 100, 2) if dps is not None and _num(price) and price > 0 else None
    payout = round(dps / eps_f * 100, 1) if dps is not None and eps_f else None
    per_f = round(price / eps_f, 2) if _num(price) and eps_f and eps_f > 0 else None

    def check(name, known, ok, fail_label):
        if not known:
            unknown.append(name)
        elif not ok:
            failed.append(fail_label)

    pbr, mcap = s.get("pbr"), s.get("mcap")
    nd = (s.get("streak") or {}).get("non_decreasing")
    roe = act.get("roe") if act else None
    eps_a = act.get("eps") if act else None
    check("予想PER", per_f is not None, per_f is not None and per_f <= 15, f"PER15倍超({per_f})")
    check("PBR", _num(pbr), _num(pbr) and pbr <= 1.5, f"PBR1.5倍超({pbr})")
    check("前期ROE", _num(roe), _num(roe) and roe >= 8, f"ROE8%未満(前期{roe}%)")
    check("会社予想の利回り", yld is not None, yld is not None and yld >= 3, f"利回り3%未満({yld}%)")
    check("会社予想の配当性向", payout is not None, payout is not None and 0 < payout <= 50, f"配当性向50%超({payout}%)")
    check("減配なしの期数", _num(nd), _num(nd) and nd >= 5, f"減配なしが5期未満({nd}期)")
    check("予想EPSと前期実績EPS", eps_f is not None and _num(eps_a), eps_f is not None and _num(eps_a) and eps_f >= eps_a,
          f"今期は減益予想(EPS {eps_a}→{eps_f})")
    check("時価総額", _num(mcap), _num(mcap) and mcap >= 100, f"時価総額100億円未満({mcap}億)")

    return {"failed": failed, "unknown": unknown, "review": review, "cut": cut,
            "yield": yld, "payout": payout, "per": per_f, "roe": roe, "dps": dps, "prior": prior,
            "eps": eps_f, "eps_prior": eps_a}


def main() -> int:
    scr = json.loads(SCREENER.read_text(encoding="utf-8"))
    if not HIKES.exists():
        print("no dividend_hikes_jp.json — skip")
        return 0
    now = datetime.now(JST)
    latest = revision_events(now)
    forecasts = _load(FORECASTS, "forecasts")
    actuals = _load(ACTUALS, "actuals")
    fc_cut = (now - timedelta(days=FORECAST_FRESH_DAYS)).strftime("%Y-%m-%d")

    a, b, c, review = [], [], [], []
    for s in scr.get("stocks", []):
        ev = latest.get(s["code"])
        if not ev:
            continue
        fc = forecasts.get(s["code"])
        if fc and fc.get("date", "") < fc_cut:
            fc = None
        r = evaluate(s, ev, fc, actuals.get(s["code"]))
        # 普通配当で前期を下回る(減配予想)なら、増額修正があっても増配銘柄ではない
        if r["cut"]:
            continue
        st = s.get("streak") or {}
        row = {
            "code": s["code"], "name": s["name"], "sector": s.get("sector"),
            "market": s.get("market"),
            "yield": r["yield"], "per": r["per"], "pbr": s.get("pbr"),
            "roe": r["roe"], "payout": r["payout"],
            "dps": r["dps"], "dpsPrior": r["prior"], "eps": r["eps"], "epsPrior": r["eps_prior"],
            "streak_years": st.get("years"), "non_decreasing": st.get("non_decreasing"),
            "streak_label": st.get("label"), "headroom": s.get("headroom"),
            "hike_date": ev["date"], "hike_detail": ev["detail"],
            "earnings_up": ev.get("earnings_up", False),
            "failed": " / ".join(r["failed"]) if r["failed"] else None,
            "unknown": " / ".join(r["unknown"]) if r["unknown"] else None,
            "review": " / ".join(r["review"]) if r["review"] else None,
            "fcDate": fc.get("date") if fc else None,
        }
        if r["unknown"] or r["review"]:
            # 確かめられない条件が残る銘柄は、外れた条件が3つ以上でなければ要確認に回す
            if len(r["failed"]) <= 2 and not any(f.startswith("利回り3%未満") for f in r["failed"]):
                review.append(row)
            continue
        if len(r["failed"]) > 2 or any(f.startswith("利回り3%未満") for f in r["failed"]):
            continue  # 高配当狙いなので、利回り3%未満はBにも入れない
        [a, b, c][len(r["failed"])].append(row)

    def key(r):
        return (-(r["yield"] or 0),)

    for g in (a, b, c, review):
        g.sort(key=key)
    scr["shikiho_hike_picks"] = {
        "asof": scr.get("asof"),
        "conditions": CONDITIONS,
        "recent_days": RECENT_DAYS,
        "hike_events": len(latest),
        "a": a[:MAX_A],
        "b": b[:MAX_B],
        "c": c[:MAX_C],
        "review": review[:MAX_REVIEW],
    }
    SCREENER.write_text(json.dumps(scr, ensure_ascii=False), encoding="utf-8")
    print(f"shikiho×hike picks: A={len(a)} B={len(b)} C={len(c)} 要確認={len(review)} (増額修正 {len(latest)} 銘柄)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
