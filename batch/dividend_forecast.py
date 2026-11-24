"""配当スクリーナーの利回り・配当性向を、会社予想(TDnet)で計算し直す。

yfinance の dividendYield は「過去12か月の実績」や古い配当にもとづくことがあり、
2026-11 の手作業チェックで次のずれが見つかった。
  - シライ電子工業(6658): 6.73% は前期の35円ベース。会社予想は28円(減配)で5.38%
  - システムエグゼ(548A): 10.25% は誤り。会社予想は44.48円で約5.1%
  - 大末建設(1814): 配当性向42.2% は実績EPSベース。会社予想ベースは 234÷467.14=50.09%
配当履歴との突き合わせ(dividend_streak.reconcile_yield)は「高すぎる方向」には
効くが、減配予想・増配予想・新設会社には効かない。

そこで fetch_dividend_hikes.py が TDnet の決算短信・配当予想の修正の XBRL から
取った会社予想(data/dividend_forecast_jp.json)があれば、それで計算し直す。
  利回り   = 年間配当予想 ÷ 株価
  配当性向 = 年間配当予想 ÷ 1株利益予想(会社が開示する配当性向と同じ定義)
会社予想が取れない銘柄は従来どおり推定値を残し、divSrc で区別する。
画面と四季報×増配ピックは、推定値の銘柄を「未確認」として扱う。

通信なし(JSON を読むだけ)。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FORECAST_JSON = REPO_ROOT / "data" / "dividend_forecast_jp.json"

# 会社は四半期ごとに予想を出し直す(約90日)。決算1回を飛ばしても拾えるよう
# 余裕を見て200日。それより古い予想は今の予想と言えないので使わない。
FRESH_DAYS = 200

JST = timezone(timedelta(hours=9))


def load(path: Path = FORECAST_JSON) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("forecasts", {}) or {}
    except (OSError, ValueError):
        return {}


def fresh(fc: dict | None, today: date | None = None) -> dict | None:
    """FRESH_DAYS 以内の予想だけを返す。"""
    if not fc or not fc.get("date"):
        return None
    today = today or datetime.now(JST).date()
    try:
        d = date.fromisoformat(fc["date"])
    except ValueError:
        return None
    return fc if (today - d).days <= FRESH_DAYS else None


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def resolve(est_yield, est_payout, price, per, fc: dict | None) -> dict:
    """利回り・配当性向と、その出どころを決める。

    戻り値:
      yield, payout   … 画面と判定に使う値(%)
      divSrc          … "company"(会社予想) / "undecided"(会社予想が未定) / "estimate"(推定)
      dps, dpsPrior, dpsDate, divCut, divHike … 会社予想の中身(company のときだけ)
      fwdEps, profitChg … 会社の1株利益予想・純利益予想の前期比(比率)
    """
    out = {"yield": est_yield, "payout": est_payout, "divSrc": "estimate"}
    if not fc:
        return out
    if _num(fc.get("eps")):
        out["fwdEps"] = fc["eps"]
    if _num(fc.get("profit_chg")):
        out["profitChg"] = fc["profit_chg"]
    out["dpsDate"] = fc.get("date")
    dps = fc.get("dps")
    if not _num(dps):
        # 会社が配当予想を「未定」にしている。過去の配当で出した利回りは今期の
        # 利回りではないので、値は残すが未確認として扱う。
        out["divSrc"] = "undecided"
        return out
    if not _num(price) or price <= 0:
        return out
    out["divSrc"] = "company"
    out["dps"] = round(dps, 2)
    out["yield"] = round(dps / price * 100, 2)
    eps = fc.get("eps")
    if _num(eps) and eps != 0:
        out["payout"] = round(dps / eps * 100, 2)
    elif _num(per) and per > 0:
        out["payout"] = round(dps / (price / per) * 100, 2)   # 利益予想が無いときは実績EPS
    else:
        out["payout"] = None
    prior = fc.get("prior")
    if _num(prior):
        out["dpsPrior"] = round(prior, 2)
        out["divCut"] = dps < prior - 1e-6
        out["divHike"] = dps > prior + 1e-6
    return out


def note(row: dict) -> str:
    """利回りセルのホバー用の説明。"""
    src = row.get("divSrc")
    if src == "company":
        s = f"会社予想 年間{row['dps']:g}円"
        if row.get("dpsPrior") is not None:
            s += f"(前期 {row['dpsPrior']:g}円"
            s += "・減配予想)" if row.get("divCut") else ("・増配予想)" if row.get("divHike") else "・据え置き)")
        return s + f" / {row.get('dpsDate')} の開示(TDnet)"
    if src == "undecided":
        return f"会社の配当予想は未定({row.get('dpsDate')} の開示)。表示は過去の配当からの推定で未確認"
    return "会社予想を未確認。yfinance と配当履歴からの推定で、旧配当・特別配当がまざる場合があります"
