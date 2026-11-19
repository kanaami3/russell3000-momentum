"""10年実績チェック(予想ではなく実績で選ぶための6項目)。

有価証券報告書から集めた直近10期の経営指標(fetch_edinet_financials.py の出力)を読み、
銘柄ごとに次の6項目を ○(合格)/ ×(不合格)/ 判定不能 で判定する。

  1. eps     EPSが増加傾向にあるか
  2. dps     増配傾向にあるか
  3. bps     BPSが着実に積み上がっているか
  4. payout  配当性向が上昇傾向ではないか(利益が伸びずに配当だけ増やしていないか)
  5. equity  自己資本比率の悪化が続いていないか
  6. profit  高ROE・高ROAが一過性ではないか

**見るのは直近10期だけ。** 11期以上前は主力事業も経営陣も入れ替わっている可能性が
あるので、データがあっても使わない。10期あれば、どこを切り取っても大きな暴落局面
(今ならコロナショック)が入る。逆境でどういう実績を残したかを見るための長さ。

**データが足りない銘柄は「判定不能」。** 上場して間もない会社や有報が取れなかった
会社を、合格にも不合格にもしない(MIN_POINTS 期未満)。

**1株指標は今の株数ベースにそろえる。** 有報の EPS・BPS は提出時点までの分割を
遡って調整してあるが、5年前の有報はその後の分割を知らない。1株配当は実際に払った
額なので分割調整されていない。yfinance の分割履歴(data/splits_jp*.csv)で割り戻す。
配当性向は分割の影響を受けないよう、調整後の 1株配当 ÷ EPS で計算し直す。

入力:  data/financial_history_jp.csv, data/splits_jp.csv, data/splits_jp_ext.csv
出力:  web/data/track_record_jp.json(推移と判定理由。画面で行を開いたときに読む)
       web/data/dividend_screener.json の各銘柄に tr(判定の要約)を追記
"""

from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FIN_CSV = Path(os.getenv("FIN_HISTORY_CSV", REPO_ROOT / "data" / "financial_history_jp.csv"))
SPLITS_CSVS = [REPO_ROOT / "data" / "splits_jp.csv", REPO_ROOT / "data" / "splits_jp_ext.csv"]
TRACK_JSON = REPO_ROOT / "web" / "data" / "track_record_jp.json"
SCREENER_JSON = REPO_ROOT / "web" / "data" / "dividend_screener.json"

WINDOW_FY = 10
MIN_POINTS = int(os.getenv("TR_MIN_POINTS", "8"))   # 10期中これ未満は判定不能

ITEMS = ["eps", "dps", "bps", "payout", "equity", "profit"]
ITEM_LABELS = {
    "eps": "EPSが増加傾向",
    "dps": "増配傾向",
    "bps": "BPSが着実に積み上がり",
    "payout": "配当性向が上昇傾向でない",
    "equity": "自己資本比率の悪化が続いていない",
    "profit": "高ROE・高ROAが一過性でない",
}

# --- しきい値 ------------------------------------------------------------------
MOVE_EPS = 0.01          # 1%未満の増減は「横ばい」とみなす(端数・為替などを吸収)
DPS_MAX_CUTS = 2         # 10期で減配2回まで(記念配当の剥落を1回は許す)
BPS_MAX_DECLINES = 1     # 10期でBPS減少1回まで(「着実に」)
EPS_MAX_LOSSES = 1       # 10期で赤字1回まで
PAYOUT_SLOPE_PT = 1.0    # 性向が年1pt以上のペースで上がり…
PAYOUT_RISE_PT = 10.0    # …かつ最初の3期平均から10pt以上上がっていれば「上昇傾向」
PAYOUT_CEIL = 100.0      # 直近3期平均が100%以上(利益以上に配当)も不合格
EQ_RUN = 3               # 自己資本比率が直近で3期以上続けて低下 → 悪化が続いている
EQ_DROP_PT = 10.0        # または最初の3期平均から10pt以上低下
ROE_MIN = float(os.getenv("TR_ROE_MIN", "8"))
ROA_MIN = float(os.getenv("TR_ROA_MIN", "4"))
HIGH_SHARE = 0.7         # 10期の7割以上で基準を満たせば「一過性でない」

CRITERIA = {
    "window": f"直近{WINDOW_FY}期(11期以上前は使わない)。データが{MIN_POINTS}期未満の項目は判定不能",
    "eps": f"回帰の傾きがプラス、直近3期平均>最初の3期平均、赤字{EPS_MAX_LOSSES}期以下",
    "dps": f"直近3期平均>最初の3期平均、減配{DPS_MAX_CUTS}回以下",
    "bps": f"最新>10期前、減少{BPS_MAX_DECLINES}回以下",
    "payout": (f"年{PAYOUT_SLOPE_PT:.0f}pt以上のペースで上がり、かつ最初の3期平均から{PAYOUT_RISE_PT:.0f}pt以上"
               f"上昇していれば不合格。直近3期平均{PAYOUT_CEIL:.0f}%以上も不合格(赤字期は除外)"),
    "equity": f"直近{EQ_RUN}期以上続けて低下、または最初の3期平均から{EQ_DROP_PT:.0f}pt以上低下なら不合格",
    "profit": (f"ROE{ROE_MIN:.0f}%以上とROA{ROA_MIN:.0f}%以上が、それぞれ{int(HIGH_SHARE * 10)}割以上の期で"
               "成立(ROA=純利益÷総資産)"),
    "source": "EDINET 有価証券報告書「主要な経営指標等の推移」(最新と5年前の2通で10期)",
}


def _f(v):
    try:
        if v in (None, "", "None"):
            return None
        x = float(v)
        return None if x != x else x
    except (TypeError, ValueError):
        return None


def _ym(fy: str) -> int:
    return int(fy[:4]) * 12 + int(fy[5:7])


# --- 入力 ---------------------------------------------------------------------

def load_splits() -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for p in SPLITS_CSVS:
        if not p.exists():
            continue
        with p.open(encoding="utf-8", newline="") as f:
            for r in csv.DictReader(f):
                ratio = _f(r.get("ratio"))
                if ratio and ratio > 0:
                    out.setdefault(r["code"], [])
                    if (r["date"], ratio) not in out[r["code"]]:
                        out[r["code"]].append((r["date"], ratio))
    return out


def _factor(splits: list[tuple[str, float]], after: str) -> float:
    """after(YYYY-MM-DD)より後の分割をすべて掛けた倍率。"""
    f = 1.0
    for d, r in splits:
        if d > after:
            f *= r
    return f


def load_series(fin_csv: Path | None = None, splits: dict | None = None) -> dict[str, dict[str, dict]]:
    """code -> fy_end -> 指標(分割調整済み)。同じ期が複数の有報にあれば新しい提出を採る。"""
    fin_csv = fin_csv or FIN_CSV
    splits = load_splits() if splits is None else splits
    if not fin_csv.exists():
        return {}
    best: dict[tuple[str, str], dict] = {}
    with fin_csv.open(encoding="utf-8", newline="") as f:
        for r in csv.DictReader(f):
            k = (r["code"], r["fy_end"])
            if k not in best or r["submitted"] > best[k]["submitted"]:
                best[k] = r
    out: dict[str, dict[str, dict]] = {}
    for (code, fy), r in best.items():
        sp = splits.get(code, [])
        # EPS・BPS: 有報の提出日までの分割は反映済み → それ以降の分割で割る
        f_ps = _factor(sp, r["submitted"])
        # 1株配当: 実額 → 期末より後の分割で割る(期末日はおおよそ月末で十分)
        f_dps = _factor(sp, fy + "-31")
        eps, bps, dps = _f(r.get("eps")), _f(r.get("bps")), _f(r.get("dps"))
        eps = eps / f_ps if eps is not None else None
        bps = bps / f_ps if bps is not None else None
        dps = dps / f_dps if dps is not None else None
        ni, ta = _f(r.get("net_income")), _f(r.get("total_assets"))
        payout = None
        if dps is not None and eps is not None and eps > 0:
            payout = dps / eps * 100
        elif _f(r.get("payout")) is not None and (eps is None or eps > 0):
            payout = _f(r.get("payout"))
        out.setdefault(code, {})[fy] = {
            "eps": eps, "bps": bps, "dps": dps, "payout": payout,
            "eq": _f(r.get("equity_ratio")), "roe": _f(r.get("roe")),
            "roa": (ni / ta * 100) if (ni is not None and ta) else None,
        }
    return out


# --- 判定の部品 ------------------------------------------------------------------

def _vals(xs):
    return [x for x in xs if x is not None]


def _slope(xs) -> float | None:
    pts = [(i, x) for i, x in enumerate(xs) if x is not None]
    if len(pts) < 3:
        return None
    n = len(pts)
    mx = sum(i for i, _ in pts) / n
    my = sum(x for _, x in pts) / n
    den = sum((i - mx) ** 2 for i, _ in pts)
    return sum((i - mx) * (x - my) for i, x in pts) / den if den else None


def _head_tail(xs, k=3):
    v = _vals(xs)
    if len(v) < 2 * k:
        k = max(1, len(v) // 2)
    return sum(v[:k]) / k, sum(v[-k:]) / k


def _moves(xs, rel=True, eps=MOVE_EPS):
    """(上昇回数, 低下回数)。連続しない欠損は飛ばして隣どうしで比べる。"""
    v = _vals(xs)
    up = down = 0
    for a, b in zip(v, v[1:]):
        d = (b - a) / abs(a) if (rel and a) else (b - a)
        th = eps if rel else 0.3
        if d > th:
            up += 1
        elif d < -th:
            down += 1
    return up, down


def _trailing_declines(xs, th=0.3):
    v = _vals(xs)
    n = 0
    for a, b in zip(v[::-1][1:], v[::-1]):   # 新しい側から (前, 後) を見る
        if b < a - th:
            n += 1
        else:
            break
    return n


def _yen(x):
    if x is None:
        return "-"
    return f"{x:,.0f}" if abs(x) >= 100 else f"{x:,.1f}"


# --- 6項目 --------------------------------------------------------------------

def judge_eps(xs):
    v = _vals(xs)
    if len(v) < MIN_POINTS:
        return None, f"EPSのデータが{len(v)}期分のみ"
    s = _slope(xs)
    h, t = _head_tail(xs)
    losses = sum(1 for x in v if x <= 0)
    ok = s is not None and s > 0 and t > h and losses <= EPS_MAX_LOSSES
    note = f"EPS {_yen(v[0])}→{_yen(v[-1])}円"
    if v[0] > 0 and v[-1] > 0:
        note += f"({v[-1] / v[0]:.1f}倍)"
    note += f"、赤字{losses}期"
    return ok, note


def judge_dps(xs):
    v = _vals(xs)
    if len(v) < MIN_POINTS:
        return None, f"配当のデータが{len(v)}期分のみ"
    h, t = _head_tail(xs)
    up, down = _moves(xs)
    ok = t > h and down <= DPS_MAX_CUTS
    return ok, f"1株配当 {_yen(v[0])}→{_yen(v[-1])}円、増配{up}回・減配{down}回"


def judge_bps(xs):
    v = _vals(xs)
    if len(v) < MIN_POINTS:
        return None, f"BPSのデータが{len(v)}期分のみ"
    up, down = _moves(xs)
    ok = v[-1] > v[0] and down <= BPS_MAX_DECLINES
    note = f"BPS {_yen(v[0])}→{_yen(v[-1])}円"
    if v[0] > 0 and v[-1] > 0 and len(v) > 1:
        cagr = (v[-1] / v[0]) ** (1 / (len(v) - 1)) - 1
        note += f"(年率{cagr * 100:+.1f}%)"
    return ok, note + f"、減少{down}回"


def judge_payout(xs):
    # 赤字期・異常値(300%超)は性向の意味をなさないので除く
    xs = [x if (x is not None and 0 <= x <= 300) else None for x in xs]
    v = _vals(xs)
    if len(v) < MIN_POINTS - 2:
        return None, f"配当性向を計算できる期が{len(v)}期のみ"
    s = _slope(xs) or 0.0
    h, t = _head_tail(xs)
    rising = s >= PAYOUT_SLOPE_PT and (t - h) >= PAYOUT_RISE_PT
    ok = not rising and t < PAYOUT_CEIL
    note = f"配当性向 {h:.0f}%→{t:.0f}%(最初と直近の3期平均)、傾き{s:+.1f}pt/年"
    if t >= PAYOUT_CEIL:
        note += "、利益以上に配当"
    return ok, note


def judge_equity(xs):
    v = _vals(xs)
    if len(v) < MIN_POINTS:
        return None, f"自己資本比率のデータが{len(v)}期分のみ"
    run = _trailing_declines(xs)
    h, t = _head_tail(xs)
    ok = run < EQ_RUN and (h - t) < EQ_DROP_PT
    note = f"自己資本比率 {v[0]:.1f}%→{v[-1]:.1f}%"
    if run >= 2:
        note += f"、直近{run}期連続で低下"
    return ok, note


def judge_profit(roe, roa):
    r = _vals(roe)
    a = _vals(roa)
    if len(r) < MIN_POINTS:
        return None, f"ROEのデータが{len(r)}期分のみ"
    hr = sum(1 for x in r if x >= ROE_MIN)
    ok = hr >= HIGH_SHARE * len(r)
    note = f"ROE{ROE_MIN:.0f}%以上 {hr}/{len(r)}期"
    if len(a) >= MIN_POINTS:
        ha = sum(1 for x in a if x >= ROA_MIN)
        ok = ok and ha >= HIGH_SHARE * len(a)
        note += f"、ROA{ROA_MIN:.0f}%以上 {ha}/{len(a)}期"
    else:
        note += "(ROAはデータ不足のためROEのみで判定)"
    return ok, note


def evaluate(by_fy: dict[str, dict]) -> dict | None:
    """1銘柄ぶん。直近10期の窓を切り出して6項目を判定する。"""
    if not by_fy:
        return None
    fys = sorted(by_fy)
    latest = _ym(fys[-1])
    fys = [fy for fy in fys if _ym(fy) > latest - WINDOW_FY * 12][-WINDOW_FY:]
    col = lambda k: [by_fy[fy][k] for fy in fys]  # noqa: E731
    series = {k: col(k) for k in ("eps", "dps", "bps", "payout", "eq", "roe", "roa")}
    res = {
        "eps": judge_eps(series["eps"]),
        "dps": judge_dps(series["dps"]),
        "bps": judge_bps(series["bps"]),
        "payout": judge_payout(series["payout"]),
        "equity": judge_equity(series["eq"]),
        "profit": judge_profit(series["roe"], series["roa"]),
    }
    items = [None if res[k][0] is None else int(bool(res[k][0])) for k in ITEMS]
    rnd = lambda xs, d: [None if x is None else round(x, d) for x in xs]  # noqa: E731
    return {
        "fy": fys,
        "eps": rnd(series["eps"], 1), "dps": rnd(series["dps"], 1), "bps": rnd(series["bps"], 0),
        "payout": rnd(series["payout"], 1), "eq": rnd(series["eq"], 1),
        "roe": rnd(series["roe"], 1), "roa": rnd(series["roa"], 1),
        "items": items,
        "notes": [res[k][1] for k in ITEMS],
    }


def build(fin_csv: Path | None = None) -> dict[str, dict]:
    return {code: ev for code, by_fy in load_series(fin_csv).items()
            if (ev := evaluate(by_fy)) is not None}


def summary(ev: dict | None) -> dict | None:
    """スクリーナーの各行に載せる要約。p=合格数, k=判定できた項目数, i=6項目(1/0/null)"""
    if not ev:
        return None
    items = ev["items"]
    return {"p": sum(1 for x in items if x == 1), "k": sum(1 for x in items if x is not None),
            "i": items, "fy": ev["fy"][-1] if ev["fy"] else None, "n": len(ev["fy"])}


def load_track_json(path: Path | None = None) -> dict:
    path = path or TRACK_JSON
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def attach(rows: list[dict], track: dict | None = None) -> int:
    """スクリーナーの行に tr を付ける。付けた件数を返す。"""
    track = load_track_json() if track is None else track
    stocks = track.get("stocks", {})
    n = 0
    for r in rows:
        s = summary(stocks.get(str(r.get("code"))))
        if s:
            r["tr"] = s
            n += 1
        else:
            r.pop("tr", None)
    return n


def meta(track: dict | None = None) -> dict | None:
    track = load_track_json() if track is None else track
    if not track.get("stocks"):
        return None
    return {"items": ITEMS, "labels": ITEM_LABELS, "criteria": track.get("criteria", CRITERIA),
            "generated_at_jst": track.get("generated_at_jst"), "count": len(track["stocks"])}


def main() -> int:
    stocks = build()
    if not stocks:
        print("10年実績: 財務データがまだ無いので何もしません(EDINET の取得待ち)")
        return 0
    now = datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds")
    out = {"generated_at_jst": now, "items": ITEMS, "labels": ITEM_LABELS,
           "criteria": CRITERIA, "stocks": stocks}
    TRACK_JSON.parent.mkdir(parents=True, exist_ok=True)
    TRACK_JSON.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    full = sum(1 for e in stocks.values() if all(x == 1 for x in e["items"]))
    print(f"10年実績: {len(stocks)}銘柄を判定(6項目すべて合格 {full}銘柄) -> {TRACK_JSON.name}")

    # いまのスクリーナーにも反映する(日次の再生成を待たずに画面に出す)
    if SCREENER_JSON.exists():
        cur = json.loads(SCREENER_JSON.read_text(encoding="utf-8"))
        n = attach(cur.get("stocks", []), out)
        cur["track_meta"] = meta(out)
        SCREENER_JSON.write_text(json.dumps(cur, ensure_ascii=False), encoding="utf-8")
        print(f"スクリーナー {len(cur.get('stocks', []))}銘柄のうち {n}銘柄に10年実績を付与")
    return 0


if __name__ == "__main__":
    sys.exit(main())
