"""四季報×増配ピックの判定(開示データで確かめた条件だけで A/B/C を付ける)のテスト。

数値は 2026-11 の決算短信・配当予想の修正と、前期の年次決算(yfinance)から。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "batch"))

from build_shikiho_hike_picks import evaluate, is_revision  # noqa: E402

REV = {"source": "TDnet", "detail": "配当予想の修正（増配）に関するお知らせ", "date": "2026-11-16"}


def stock(price, pbr, nd, mcap):
    return {"price": price, "pbr": pbr, "mcap": mcap, "streak": {"non_decreasing": nd}}


def test_toyo_tire_is_a():
    # 純利益の前期比は -7.3% だが、EPSは 413.1 → 478.94 で増益(条件7は EPS で見る)
    r = evaluate(stock(3830, 1.13, 15, 4718), REV,
                 {"dps": 153, "prior": 130, "eps": 478.94, "profit_chg": -0.073},
                 {"eps": 413.1, "roe": 12.78})
    assert r["failed"] == [] and r["unknown"] == [] and r["review"] == []
    assert r["yield"] == 3.99 and r["payout"] == 31.9


def test_nippon_yakin_fails_only_roe():
    r = evaluate(stock(5260, 0.69, 5, 729), REV, {"dps": 265, "prior": 220, "eps": 757.3},
                 {"eps": 519.86, "roe": 7.31})
    assert len(r["failed"]) == 1 and r["failed"][0].startswith("ROE8%未満")


def test_daiwa_house_fails_only_eps():
    r = evaluate(stock(2312, 0.96, 5, 28654), REV, {"dps": 90, "prior": 87.5, "eps": 231.6},
                 {"eps": 283.2, "roe": 12.72})
    assert len(r["failed"]) == 1 and r["failed"][0].startswith("今期は減益予想")


def test_toa_special_dividend_is_not_a_cut():
    # 前期100円のうち23円は特別配当。普通配当では 77 → 85 円の増配
    r = evaluate(stock(2036, 1.3, 8, 1617), REV,
                 {"dps": 85, "prior": 100, "eps": 210.01, "special_note": True,
                  "special_prior": 23.0, "special_cur": 0.0},
                 {"eps": 248.4, "roe": 17.36})
    assert r["cut"] is False and r["prior"] == 77


def test_dowa_compares_ordinary_dividends():
    r = evaluate(stock(7126, 0.87, 17, 4216), REV,
                 {"dps": 415, "prior": 368, "eps": 1183.4, "special_note": True,
                  "special_prior": 100.0, "special_cur": 0.0},
                 {"eps": 1049.83, "roe": 14.63})
    assert r["failed"] == [] and r["prior"] == 268


def test_missing_data_is_unknown_not_pass():
    r = evaluate(stock(1000, 1.0, 10, 500), REV, None, None)
    assert "ROE" not in " ".join(r["failed"])
    assert len(r["unknown"]) >= 4      # PER・ROE・利回り・性向・条件7が確かめられない


def test_unparsed_special_note_needs_review():
    r = evaluate(stock(1000, 1.0, 10, 500), REV,
                 {"dps": 100, "prior": 80, "eps": 300, "special_note": True}, {"eps": 250, "roe": 10})
    assert r["review"]


def test_commemorative_title_needs_review():
    ev = {"source": "TDnet", "detail": "期末配当予想の修正（会社設立15周年記念配当）に関するお知らせ"}
    r = evaluate(stock(2590, 0.6, 9, 363), ev, {"dps": 110, "prior": 100, "eps": 300}, {"eps": 250, "roe": 10})
    assert r["review"]


def test_revision_vs_year_over_year():
    assert is_revision(REV)
    assert is_revision({"source": "TDnet XBRL", "kind": "revision", "detail": "x"})
    # 決算短信で「今期予想が前期より多い」だけ(期中の修正ではない)は数えない
    assert not is_revision({"source": "TDnet XBRL", "kind": "tanshin", "detail": "決算短信で今期の年間配当予想…"})
    assert not is_revision({"source": "TDnet", "detail": "2027年３月期 第２四半期決算短信（増配）"})
    assert is_revision({"source": "決算まとめ(手動収集)", "detail": "配当も20円増額"})
