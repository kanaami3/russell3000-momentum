"""増配株スクリーニング（実装可能な7条件版）。

出典の12条件のうち、いま手元のデータで判定できるものだけを実装する。

  ①② PER・PBR が市場中央値より低い     value_data 由来
  ③  ROE が下限以上                     value_data 由来
  ⑤  配当利回り 3% 以上                 value_data 由来
  ⑦  時価総額 100億円以上               value_data 由来
  ⑪  減配が直近10期で1回以下            配当履歴25年分
  ⑫  直近10期で無配転落なし             配当履歴25年分

未実装（データが無い）:
  ④ ROA / ⑥ 自己資本比率 … value_data に項目が無い
  ⑧ EPS赤字転落なし / ⑨ EPS9期前比2倍 / ⑩ BPS10期連続増額
     … 10期分の財務データが要る。J-Quants 無料プランは2期分、
       yfinance の日本株は四半期財務がほぼ取得できない（実測 20件中1件）。

**「市場平均」は平均ではなく中央値を使う。**
PERは赤字企業や一過性要因で数百倍という値が混じり、平均は容易に壊れる。
実測でも配当性向に2195%という値があった。中央値なら外れ値に動かされない。

**10期分のデータが無い銘柄は「判定不能」にする。**
条件を満たしたことにしてはいけない。上場5年の会社に「10期減配なし」は
言えない。合格でも不合格でもなく、判定できないものとして分ける。
"""

from __future__ import annotations

import os
from statistics import median

import pandas as pd

import dividend_streak as ds

MIN_YIELD = float(os.getenv("DGS_MIN_YIELD", "3.0"))          # ⑤
MIN_MCAP_OKU = float(os.getenv("DGS_MIN_MCAP_OKU", "100"))    # ⑦ 億円
MIN_ROE = float(os.getenv("DGS_MIN_ROE", "8.0"))              # ③
LOOKBACK_FY = int(os.getenv("DGS_LOOKBACK_FY", "10"))         # ⑪⑫
MAX_CUTS = int(os.getenv("DGS_MAX_CUTS", "1"))                # ⑪

# 金融・リース業は事業特性上この条件では拾えないと出典にもある。
# ROA・自己資本比率が未実装なので現時点で挙動は変わらないが、
# セクターを識別できるようにはしておく。
FINANCIAL_SECTORS = {"銀行", "金融（除く銀行）", "保険"}


def market_medians(stocks: list[dict]) -> dict:
    """①② の基準となる市場中央値。

    正の値だけを対象にする。赤字企業のPERは負またはNoneで、
    混ぜると「割安」の意味が変わる。
    """
    per = [s["per"] for s in stocks if isinstance(s.get("per"), (int, float)) and s["per"] > 0]
    pbr = [s["pbr"] for s in stocks if isinstance(s.get("pbr"), (int, float)) and s["pbr"] > 0]
    return {
        "per": round(median(per), 2) if per else None,
        "pbr": round(median(pbr), 2) if pbr else None,
        "n_per": len(per),
        "n_pbr": len(pbr),
    }


def dividend_record(series: pd.Series, now: pd.Timestamp | None = None) -> dict:
    """⑪⑫ を配当履歴から判定する。

    dividend_streak の会計年度集計をそのまま使う。権利落ち日ベースで
    会計年度にまとめ、期末配当が確定していない期は落とす、という処理を
    二重に書かないため。
    """
    now = now or pd.Timestamp.utcnow().tz_localize(None)
    annual = ds._drop_incomplete(ds._annual_dps(series, ds.FY_END_MONTH), ds.FY_END_MONTH, now)

    if annual.empty:
        return {"complete_fy": 0, "cuts": None, "zero_years": None, "judgeable": False}

    tail = annual.tail(LOOKBACK_FY)
    vals = tail.tolist()

    zero_years = len([v for v in vals if v <= 0])
    cuts = 0
    for i in range(1, len(vals)):
        if vals[i - 1] > 0 and vals[i] / vals[i - 1] < 1 - ds.INCREASE_EPS:
            cuts += 1

    return {
        "complete_fy": len(vals),
        "first_fy": int(tail.index[0]),
        "last_fy": int(tail.index[-1]),
        "cuts": cuts,
        "zero_years": zero_years,
        # 10期揃っていなければ「10期減配なし」とは言えない
        "judgeable": len(vals) >= LOOKBACK_FY,
    }


def evaluate(stock: dict, med: dict, div: dict) -> dict:
    """1銘柄を12条件（実装分）で判定する。

    True/False/None の3値。None は「データが無く判定できない」。
    False と混ぜると、データ欠損が不合格として扱われてしまう。
    """
    per, pbr = stock.get("per"), stock.get("pbr")
    roe, y, mcap = stock.get("roe"), stock.get("yield"), stock.get("mcap")
    num = lambda v: isinstance(v, (int, float))

    c = {
        "c1_per_cheap": (per < med["per"]) if (num(per) and per > 0 and med["per"]) else None,
        "c2_pbr_cheap": (pbr < med["pbr"]) if (num(pbr) and pbr > 0 and med["pbr"]) else None,
        "c3_roe": (roe >= MIN_ROE) if num(roe) else None,
        "c5_yield": (y >= MIN_YIELD) if num(y) else None,
        "c7_mcap": (mcap >= MIN_MCAP_OKU) if num(mcap) else None,
        "c11_few_cuts": (div["cuts"] <= MAX_CUTS) if div["judgeable"] else None,
        "c12_no_zero": (div["zero_years"] == 0) if div["judgeable"] else None,
    }

    passed = [k for k, v in c.items() if v is True]
    failed = [k for k, v in c.items() if v is False]
    unknown = [k for k, v in c.items() if v is None]

    return {
        "conditions": c,
        "pass_count": len(passed),
        "failed": failed,
        "unknown": unknown,
        # 全条件を満たし、かつ判定不能が1つも無いものだけを合格とする
        "qualified": not failed and not unknown,
    }


def screen(stocks: list[dict], history: dict[str, pd.Series],
           now: pd.Timestamp | None = None) -> dict:
    med = market_medians(stocks)
    results = []

    for s in stocks:
        code = str(s.get("code") or "")
        div = dividend_record(history.get(code, pd.Series(dtype="float64")), now=now)
        ev = evaluate(s, med, div)
        results.append({
            "code": code, "name": s.get("name"), "sector": s.get("sector"),
            "yield": s.get("yield"), "per": s.get("per"), "pbr": s.get("pbr"),
            "roe": s.get("roe"), "mcap": s.get("mcap"),
            "is_financial": s.get("sector") in FINANCIAL_SECTORS,
            "dividend_record": div,
            **ev,
        })

    qualified = [r for r in results if r["qualified"]]
    qualified.sort(key=lambda r: -(r["yield"] or 0))

    return {
        "market_medians": med,
        "criteria": {
            "min_yield": MIN_YIELD, "min_mcap_oku": MIN_MCAP_OKU,
            "min_roe": MIN_ROE, "lookback_fy": LOOKBACK_FY, "max_cuts": MAX_CUTS,
        },
        "universe": len(results),
        "qualified_count": len(qualified),
        "qualified": qualified,
        "all": results,
        "unimplemented": ["④ROA", "⑥自己資本比率", "⑧EPS赤字なし",
                          "⑨EPS9期前比2倍", "⑩BPS10期連続増額"],
    }
