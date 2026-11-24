"""四季報×増配ピックの判定に使う「前期の実績」(1株利益・ROE)を取る。

条件7「減益予想でない」は、会社の今期予想EPS(TDnet)と前期の実績EPSを比べる。
yfinance の trailingEps は直近12か月で、前期実績とずれる(TOYO TIRE は前期413.1円、
予想478.9円で増益だが、純利益の前期比は -7.3% で、どちらを使うかで判定が逆になる)。
ROE も yfinance の returnOnEquity は直近12か月で、日本冶金は 8.9% と出るが
前期実績は 7.31%(純利益 ÷ 期首期末平均の自己資本)。

取るのは直近 RECENT_DAYS 日に配当を増額修正した銘柄だけ(数百銘柄)。
年1回しか変わらないので、REFRESH_DAYS 日以内に取ったものは取り直さない。
出力: data/fy_actuals_jp.json {"actuals": {code: {fy_end, eps, roe, fetched}}}
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import build_shikiho_hike_picks as picks  # noqa: E402  (増額修正の判定を共有)

OUT = REPO_ROOT / "data" / "fy_actuals_jp.json"
JST = timezone(timedelta(hours=9))
REFRESH_DAYS = 30


def _row(df, *names):
    for n in names:
        if df is not None and n in df.index:
            s = df.loc[n].dropna()
            if len(s):
                return s.sort_index()
    return None


def fy_actual(code: str) -> dict | None:
    import yfinance as yf
    t = yf.Ticker(f"{code}.T")
    inc, bs = t.income_stmt, t.balance_sheet
    eps = _row(inc, "Basic EPS", "Diluted EPS")
    ni = _row(inc, "Net Income Common Stockholders", "Net Income")
    eq = _row(bs, "Stockholders Equity", "Common Stock Equity")
    if eps is None:
        return None
    fy_end = eps.index[-1]
    out = {"fy_end": fy_end.strftime("%Y-%m-%d"), "eps": round(float(eps.iloc[-1]), 2)}
    # ROE = 当期純利益 ÷ 期首・期末平均の自己資本(会社の決算短信と同じ定義)
    if ni is not None and eq is not None and fy_end in ni.index and fy_end in eq.index:
        prev = eq[eq.index < fy_end]
        base = (float(eq[fy_end]) + float(prev.iloc[-1])) / 2 if len(prev) else float(eq[fy_end])
        if base > 0:
            out["roe"] = round(float(ni[fy_end]) / base * 100, 2)
    return out


def main() -> int:
    data = json.loads(OUT.read_text(encoding="utf-8")) if OUT.exists() else {}
    actuals = data.get("actuals", {})
    today = datetime.now(JST)
    fresh_cut = (today - timedelta(days=REFRESH_DAYS)).strftime("%Y-%m-%d")
    codes = sorted(picks.revision_events(today).keys())
    todo = [c for c in codes if actuals.get(c, {}).get("fetched", "") < fresh_cut]
    ok = 0
    for c in todo:
        try:
            a = fy_actual(c)
        except Exception as e:  # レート制限など。次回また取る
            print(f"  {c}: {e}")
            a = None
        if a:
            a["fetched"] = today.strftime("%Y-%m-%d")
            actuals[c] = a
            ok += 1
        time.sleep(0.3)
    OUT.write_text(json.dumps({
        "note": "増額修正した銘柄の前期実績(yfinance の年次決算)。eps=1株利益, roe=純利益÷平均自己資本(%)。"
                "batch/fetch_fy_actuals.py が更新。",
        "updated_at": today.strftime("%Y-%m-%d"),
        "actuals": dict(sorted(actuals.items())),
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"fy actuals: 対象 {len(codes)} / 取得 {ok}/{len(todo)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
