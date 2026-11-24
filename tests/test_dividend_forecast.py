"""会社予想(TDnet XBRL)で利回り・配当性向を出し直す処理のテスト。

数値は 2026-11 の実際の決算短信(サマリー iXBRL)から取ったもの。
  大末建設(1814)     2026-11-05 中間決算短信: 前期183円 → 今期予想234円、EPS予想467.14円
  シライ電子工業(6658) 2026-11-06 中間決算短信: 前期35円 → 今期予想28円(減配)、EPS予想66.55円
  システムエグゼ(548A) 2026-11-11 中間決算短信: 前期37.60円 → 今期予想44.48円、EPS予想111.21円
実行: python -m pytest tests/ (または python tests/test_dividend_forecast.py)
"""

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "batch"))

import dividend_forecast as df  # noqa: E402
from fetch_dividend_hikes import _facts, extract_forecast, judge_xbrl  # noqa: E402

NC = "NonConsolidatedMember"


def ix(name, ctx, val, scale=0, sign=""):
    if val is None:
        return f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{ctx}" xsi:nil="true"/>'
    s = f' sign="-"' if sign else ""
    return f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{ctx}" scale="{scale}"{s}>{val}</ix:nonFraction>'


def quarterly(prior, fcst, eps, chg=None, eps_name="NetIncomePerShare"):
    parts = [
        ix("DividendPerShare", f"PriorYearDuration_SecondQuarterMember_{NC}_ResultMember", "10.00"),
        ix("DividendPerShare", f"PriorYearDuration_AnnualMember_{NC}_ResultMember", prior),
        ix("DividendPerShare", f"CurrentYearDuration_ThirdQuarterMember_{NC}_ForecastMember", None),
        ix("DividendPerShare", f"CurrentYearDuration_AnnualMember_{NC}_ForecastMember", fcst),
        ix(eps_name, "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", "100.00"),
        ix(eps_name, "CurrentYearDuration_ConsolidatedMember_ForecastMember", eps),
    ]
    if chg is not None:
        neg = chg.startswith("-")
        parts.append(ix("ChangeInProfitAttributableToOwnersOfParent",
                        "CurrentYearDuration_ConsolidatedMember_ForecastMember",
                        chg.lstrip("-"), scale=-2, sign=neg))
    return "<html>" + "".join(parts) + "</html>"


def test_daisue_payout_matches_company_disclosure():
    fc = extract_forecast(_facts(quarterly("183.00", "234.00", "467.14", "26.6")))
    assert fc["kind"] == "tanshin" and fc["dps"] == 234 and fc["prior"] == 183
    assert abs(fc["profit_chg"] - 0.266) < 1e-9
    r = df.resolve(6.32, 42.2, 3700, 7.33, fc)
    assert r["divSrc"] == "company"
    assert r["payout"] == 50.09          # 会社開示の 50.09%(サイトは42.2%だった)
    assert r["yield"] == 6.32
    assert r["divHike"] and not r["divCut"]


def test_shirai_uses_cut_forecast_not_old_dividend():
    fc = extract_forecast(_facts(quarterly("35.00", "28.00", "66.55", "-12.0")))
    r = df.resolve(6.73, 55.1, 520, 8.19, fc)
    assert r["yield"] == 5.38            # 旧配当35円の 6.73% ではない
    assert r["divCut"] is True
    assert r["profitChg"] < 0


def test_system_exe_yield():
    fc = extract_forecast(_facts(quarterly("37.60", "44.48", "111.21")))
    r = df.resolve(10.25, None, 868, 8.69, fc)
    assert r["yield"] == 5.12            # 10.25% ではない
    assert r["payout"] == 40.0


def test_ifrs_eps_name():
    fc = extract_forecast(_facts(quarterly("15.00", "17.00", "60.84", eps_name="BasicEarningsPerShareIFRS")))
    assert fc["eps"] == 60.84


def test_undecided_forecast_is_not_company_verified():
    fc = extract_forecast(_facts(quarterly("30.00", None, "50.00")))
    assert fc["undecided"] is True and fc["dps"] is None
    r = df.resolve(4.0, 40.0, 1000, 10, fc)
    assert r["divSrc"] == "undecided" and r["yield"] == 4.0


def test_full_year_tanshin_uses_next_year_forecast():
    html = "<html>" + "".join([
        ix("DividendPerShare", f"PriorYearDuration_AnnualMember_{NC}_ResultMember", "40.00"),
        ix("DividendPerShare", f"CurrentYearDuration_AnnualMember_{NC}_ResultMember", "50.00"),
        ix("DividendPerShare", f"NextYearDuration_AnnualMember_{NC}_ForecastMember", "45.00"),
        ix("NetIncomePerShare", "NextYearDuration_ConsolidatedMember_ForecastMember", "150.00"),
    ]) + "</html>"
    fc = extract_forecast(_facts(html))
    assert fc["kind"] == "tanshin_full" and fc["dps"] == 45 and fc["prior"] == 50
    assert df.resolve(3, 30, 1500, 10, fc)["divCut"] is True


def test_range_forecast_uses_lower():
    html = "<html>" + "".join([
        ix("DividendPerShare", f"PriorYearDuration_AnnualMember_{NC}_ResultMember", "40.00"),
        ix("DividendPerShare", f"CurrentYearDuration_AnnualMember_{NC}_ForecastMember", None),
        ix("DividendPerShare", f"CurrentYearDuration_AnnualMember_{NC}_LowerMember", "42.00"),
        ix("DividendPerShare", f"CurrentYearDuration_AnnualMember_{NC}_UpperMember", "48.00"),
    ]) + "</html>"
    fc = extract_forecast(_facts(html))
    assert fc["dps"] == 42


def test_hike_judgement_unchanged():
    facts = _facts(quarterly("183.00", "234.00", "467.14"))
    assert judge_xbrl(facts["dps"])["kind"] == "tanshin"


def test_no_forecast_keeps_estimate():
    r = df.resolve(5.98, None, 1940, None, None)
    assert r == {"yield": 5.98, "payout": None, "divSrc": "estimate"}


def test_stale_forecast_dropped():
    assert df.fresh({"date": "2026-01-01"}, today=date(2026, 11, 24)) is None
    assert df.fresh({"date": "2026-11-05"}, today=date(2026, 11, 24))


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)


def test_split_suspect_not_cut():
    r = df.resolve(3.0, 30, 1124, 9, {"dps": 43, "prior": 180, "eps": 133.08, "date": "2026-11-11"})
    assert r.get("divCut") is None and r["divSplitSuspect"]
