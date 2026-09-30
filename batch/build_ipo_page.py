#!/usr/bin/env python3
"""IPO銘柄の上場後の値動きを計算し、画面用データとチャートデータを書き出す。

入力:  data/ipo_jp.json          (fetch_ipo_jp.py が作る上場一覧)
出力:  web/data/ipo_jp.json      (画面が読む一覧。数値は計算済み)
       web/data/chart_data_ipo.json (chart_data_jp.json と同じ形のOHLCV)

**初値は「上場初日の始値」ではなく「値がついた最初の日の始値」。**
人気銘柄は初日に売買が成立せず、初値が翌日以降につくことがある。yfinance は
値がつかなかった日のバーを返さないので、取得できた最初のバーの Open が初値に
なる。上場日を決め打ちで参照すると、そういう銘柄だけ初値が取れずに落ちる。

**上場来高値は終値ではなく High で取る。**
「初値からいくら上げて、そこからいくら調整したか」を見るのが目的なので、
ザラ場の天井を使う。終値ベースだと、寄り天で上げた分が消える。

**銘柄コードに英字が入る。**
2024年以降の新規上場は 648A のような形式。yfinance には "648A.T" で渡す。
数値としてゼロ埋めなどをしてはいけない。

**取得できなかった銘柄は「まだ値動きなし」ではなく「不明」。**
上場予定の銘柄（listed=False）は最初から価格を持たないので区別する。上場済み
なのに取れなかったものは fetch_failed を立てて、画面で空欄と区別できるように
する。ゼロや0%で埋めると、下落率ランキングの最下位に居座ることになる。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
IPO_LIST = REPO_ROOT / "data" / "ipo_jp.json"
PROFILES = REPO_ROOT / "data" / "ipo_profiles_jp.json"
TDNET = REPO_ROOT / "data" / "tdnet_ipo_jp.json"
OUT_PAGE = REPO_ROOT / "web" / "data" / "ipo_jp.json"
OUT_CHART = REPO_ROOT / "web" / "data" / "chart_data_ipo.json"

JST = timezone(timedelta(hours=9))
BATCH_SIZE = int(os.getenv("IPO_BATCH_SIZE", "40"))
PERIOD = os.getenv("IPO_PERIOD", "3y")

MA_SHORT = 5
MA_LONG = 25


def _r(v, nd=2):
    return None if v is None or pd.isna(v) else round(float(v), nd)


def fetch_ohlcv(tickers: list[str]) -> dict[str, pd.DataFrame]:
    """yf.download をまとめて呼び、ticker -> DataFrame に割り直す。"""
    if not tickers:
        return {}
    df = yf.download(
        tickers=tickers,
        period=PERIOD,
        interval="1d",
        auto_adjust=False,   # 初値を実際の値段で見たいので調整前を使う
        progress=False,
        group_by="ticker",
        threads=True,
    )
    out: dict[str, pd.DataFrame] = {}
    if df is None or df.empty:
        return out

    if isinstance(df.columns, pd.MultiIndex):
        for t in tickers:
            if t in df.columns.get_level_values(0):
                sub = df[t].dropna(how="all")
                if not sub.empty:
                    out[t] = sub
    elif len(tickers) == 1:
        sub = df.dropna(how="all")
        if not sub.empty:
            out[tickers[0]] = sub
    return out


def series_rows(sub: pd.DataFrame) -> list[list]:
    """chart_data_jp.json と同じ [date, open, high, low, close, volume]。"""
    rows = []
    for ts, row in sub.iterrows():
        c = row.get("Close")
        if pd.isna(c):
            continue
        rows.append([
            ts.date().isoformat(),
            _r(row.get("Open", c)), _r(row.get("High", c)),
            _r(row.get("Low", c)), _r(c),
            int(row.get("Volume") or 0),
        ])
    return rows


SPARK_POINTS = 60

# 日中の高値が終値のこの倍率を超える行は、値そのものが壊れているとみなす。
# 日本株には値幅制限があり、1日で終値の3倍の高値を付けることはできない。
# 実測: 8303 の 2023-09-27 に 55,900,000,256 という行があり(出来高0)、
# これを拾うと上場来高値が5.6兆円になり、下落率が-100%になった。
BAD_TICK_RATIO = float(os.getenv("IPO_BAD_TICK_RATIO", "3.0"))

# 中央値からこの倍率だけ離れた終値は、値ではなく事故とみなす。
ABSURD_RATIO = float(os.getenv("IPO_ABSURD_RATIO", "50.0"))


def clean_bars(sub: pd.DataFrame, listing_date: str | None) -> pd.DataFrame:
    """上場日より前の行と、壊れた行を落とす。

    **上場日で切るのが重要。**
    持株会社化やテクニカル上場の銘柄は、同じティッカーに前身企業の株価が
    続いている。period="3y" で取ると上場前の値が混ざり、「初値」が上場の
    2年前の始値になる。実測で 8303 SBI新生銀行が2023年から始まっていた。
    """
    if sub is None or sub.empty:
        return sub

    if listing_date:
        try:
            sub = sub[sub.index >= pd.Timestamp(listing_date)]
        except (ValueError, TypeError):
            pass
    if sub.empty:
        return sub

    hi, cl = sub["High"], sub["Close"]

    # (1) 日中の値幅が異常。値幅制限のある日本株で終値の3倍の高値はない。
    bad = (cl > 0) & (hi > cl * BAD_TICK_RATIO)

    # (2) 行まるごとが桁違い。8303 の壊れた行は O/H/L/C すべてが 5.5e10 で
    #     整合していたため (1) では捕まらなかった。中央値と比べて桁が違う
    #     ものを落とす。上場後に75倍になった銘柄(キオクシア)でも最大値は
    #     中央値の6倍程度なので、50倍を閾値にすれば正常値は巻き込まない。
    med = cl[cl > 0].median()
    if pd.notna(med) and med > 0:
        bad = bad | (cl > med * ABSURD_RATIO) | ((cl > 0) & (cl < med / ABSURD_RATIO))

    if bad.any():
        print(f"  壊れた行を {int(bad.sum())} 件落としました", file=sys.stderr)
        sub = sub[~bad]
    return sub


def spark(sub: pd.DataFrame, n: int = SPARK_POINTS) -> list[list[float]]:
    """一覧に並べるミニローソク足用に、上場来のOHLCを n 本へまとめる。

    **間引きではなく集約する。**
    等間隔に抜き出すと、抜かれた日の高値・安値が消えてヒゲが無くなる。
    日足を n 等分のバケツに入れ、始値=最初の始値 / 高値=期間中の最大 /
    安値=期間中の最小 / 終値=最後の終値 とまとめる。週足や月足を作るのと
    同じやり方で、本数が減っても値動きの幅は保たれる。

    一覧で141銘柄ぶんを同時に描くので、フルの日足を読ませると重い。
    拡大チャートの方は chart_data_ipo.json の日足をそのまま使う。
    """
    rows = sub.dropna(subset=["Close"])
    m = len(rows)
    if m == 0:
        return []

    o, h, l, c = rows["Open"], rows["High"], rows["Low"], rows["Close"]
    out: list[list[float]] = []
    buckets = min(n, m)
    for i in range(buckets):
        a = i * m // buckets
        b = (i + 1) * m // buckets
        if b <= a:
            continue
        op = o.iloc[a]
        if pd.isna(op):
            op = c.iloc[a]
        out.append([
            round(float(op), 2),
            round(float(h.iloc[a:b].max()), 2),
            round(float(l.iloc[a:b].min()), 2),
            round(float(c.iloc[b - 1]), 2),
        ])
    return out


# 資金流入を見る窓。25日＝おおよそ1か月の営業日。
FLOW_SHORT = 5
FLOW_LONG = 25

# 流動性の下限（円）。売買代金がこれを下回ると、数字が良くても
# 実際には買えない・売れない。
MIN_TURNOVER = float(os.getenv("IPO_MIN_TURNOVER", "100000000"))  # 1億円


def flow_metrics(sub: pd.DataFrame) -> dict:
    """出来高と資金流入の続き具合を測る。

    見ているのは3つ。

      1. 出来高が増えているか   直近5日平均 ÷ 直近25日平均
      2. 買いが優勢か           上昇日の出来高が全体に占める割合
      3. 続いているか           25日線の上にいるか、25日で上げているか

    **上昇日の出来高比率を見るのが要点。**
    単に出来高が増えただけでは、投げ売りで増えたのか買いが入ったのか
    分からない。上げた日に出来高が偏っているなら、売り手より買い手が
    急いでいる。逆に下げた日に偏っていれば、増えた出来高は流出。

    **25日分に満たない銘柄は判定しない。**
    上場3日目の銘柄に「出来高が増えている」とは言えない。0や False で
    埋めると、上場直後の銘柄が一律で「流入なし」として並ぶ。
    """
    vol = sub["Volume"].fillna(0)
    close = sub["Close"].dropna()
    if len(close) < FLOW_LONG or len(vol) < FLOW_LONG:
        return {"flow_judgeable": False}

    v_short = float(vol.tail(FLOW_SHORT).mean())
    v_long = float(vol.tail(FLOW_LONG).mean())
    vol_ratio = (v_short / v_long) if v_long > 0 else None

    tail = sub.tail(FLOW_LONG)
    chg = tail["Close"].diff()
    v = tail["Volume"].fillna(0)
    total = float(v.sum())
    up_share = float(v[chg > 0].sum()) / total if total > 0 else None

    ma25 = float(close.tail(FLOW_LONG).mean())
    last = float(close.iloc[-1])
    ret25 = (last / float(close.iloc[-FLOW_LONG]) - 1) * 100

    turnover = float((tail["Close"] * tail["Volume"]).tail(FLOW_SHORT).mean())

    return {
        "flow_judgeable": True,
        "vol_ratio": _r(vol_ratio),
        "up_volume_share": _r(up_share, 3),
        "above_ma25_flow": bool(last >= ma25),
        "ret_25d_pct": _r(ret25),
        "turnover_5d": int(turnover),
        "liquid": bool(turnover >= MIN_TURNOVER),
    }


# 「資金が入っていて続いている」とみなすしきい値
FLOW_VOL_RATIO = 1.3      # 出来高が直近1か月平均の1.3倍以上
FLOW_UP_SHARE = 0.55      # 上昇日の出来高が全体の55%以上


def flow_score(m: dict) -> int | None:
    """0〜4点。判定できなければ None。

    点を足すだけの素朴な作りにしてある。重み付けを凝っても、
    根拠のない係数が増えるだけで当たりやすくはならない。
    """
    if not m.get("flow_judgeable"):
        return None
    s = 0
    if isinstance(m.get("vol_ratio"), (int, float)) and m["vol_ratio"] >= FLOW_VOL_RATIO:
        s += 1
    if isinstance(m.get("up_volume_share"), (int, float)) and m["up_volume_share"] >= FLOW_UP_SHARE:
        s += 1
    if m.get("above_ma25_flow"):
        s += 1
    if isinstance(m.get("ret_25d_pct"), (int, float)) and m["ret_25d_pct"] > 0:
        s += 1
    return s


def metrics(sub: pd.DataFrame, offer_price: float | None) -> dict:
    closes = sub["Close"].dropna()
    if closes.empty:
        return {"fetch_failed": True}

    first_idx = closes.index[0]
    first_open = sub.loc[first_idx, "Open"]
    first_price = float(first_open) if pd.notna(first_open) else float(closes.iloc[0])

    high = float(sub["High"].max())
    high_date = sub["High"].idxmax().date().isoformat()
    last = float(closes.iloc[-1])

    ma5 = closes.tail(MA_SHORT).mean() if len(closes) >= MA_SHORT else None
    ma25 = closes.tail(MA_LONG).mean() if len(closes) >= MA_LONG else None

    vol = sub["Volume"].dropna()
    vol5 = float(vol.tail(5).mean()) if len(vol) >= 5 else None

    return {
        "fetch_failed": False,
        "first_date": first_idx.date().isoformat(),
        "first_price": _r(first_price),
        # 公募価格が未定（承認直後）の銘柄では初値騰落率は出せない
        "first_pop_pct": _r((first_price / offer_price - 1) * 100)
        if offer_price else None,
        "high": _r(high),
        "high_date": high_date,
        "last_close": _r(last),
        "last_date": closes.index[-1].date().isoformat(),
        "from_first_pct": _r((last / first_price - 1) * 100) if first_price else None,
        # 上場来高値からの下落率。マイナスで表示される。
        "drawdown_pct": _r((last / high - 1) * 100) if high else None,
        "ma5": _r(ma5),
        "ma25": _r(ma25),
        "above_ma5": None if ma5 is None else bool(last >= ma5),
        "above_ma25": None if ma25 is None else bool(last >= ma25),
        "vol_avg5": None if vol5 is None else int(vol5),
        "bars": int(len(closes)),
        "spark": spark(sub),
    }


def load_profiles() -> dict:
    """会社プロフィールのキャッシュを読む。無ければ空。

    fetch_ipo_profiles.py が少しずつ埋めていくので、最初のうちは
    プロフィールが付かない銘柄がある。付いていないことと「情報が無い」
    ことは別なので、画面側では単に非表示にする。
    """
    if not PROFILES.exists():
        return {}
    try:
        d = json.loads(PROFILES.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("プロフィールのキャッシュが読めませんでした。無視して続けます。",
              file=sys.stderr)
        return {}
    return {k: v for k, v in (d.get("profiles") or {}).items() if v.get("ok")}


# 画面に出すプロフィール項目。
# summary_en（英文の会社概要）はここに含めない。画面は日本語なので、
# 英文をそのまま出さず、generate_ipo_ai.py が作った summary_ja を使う。
# 英文はキャッシュには残してあり、日本語を作るときの元データになる。
PROFILE_FIELDS = ("sector_ja", "sector_en", "industry_en", "employees",
                  "website", "city", "market_cap", "biz_ja", "summary_ja")


# 「最近」とみなす日数。業績予想の修正は出てから時間が経つと材料として
# 薄れるので、直近のものだけを印にする。
RECENT_DISCLOSURE_DAYS = 90
MAX_DISCLOSURES_PER_STOCK = 4


def load_tdnet() -> tuple[dict[str, list[dict]], str | None]:
    """銘柄ごとの適時開示。あわせて「いつから集めているか」を返す。

    収集開始日を持ち回るのが大事。TDnetは31日しか遡れないので、それ以前の
    決算は永遠に空欄のままになる。画面で「決算の開示なし」と出してしまうと、
    実際には決算を出している会社まで出していないように見える。
    """
    if not TDNET.exists():
        return {}, None
    try:
        d = json.loads(TDNET.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("TDnetのキャッシュが読めませんでした。無視して続けます。", file=sys.stderr)
        return {}, None

    by_code: dict[str, list[dict]] = {}
    for row in d.get("disclosures", []):
        by_code.setdefault(str(row.get("code")), []).append(row)
    for rows in by_code.values():
        rows.sort(key=lambda r: (r.get("date", ""), r.get("time", "")), reverse=True)
    return by_code, d.get("first_collected")


def attach_disclosures(row: dict, rows: list[dict], today) -> None:
    if not rows:
        return
    row["disclosures"] = [
        {k: r.get(k) for k in ("date", "kind", "title")}
        for r in rows[:MAX_DISCLOSURES_PER_STOCK]
    ]
    kessan = [r for r in rows if r.get("kind") == "決算短信"]
    if kessan:
        row["last_earnings_date"] = kessan[0]["date"]
        row["last_earnings_title"] = kessan[0]["title"]

    def recent(kind: str) -> bool:
        for r in rows:
            if r.get("kind") != kind:
                continue
            try:
                d = datetime.fromisoformat(r["date"]).date()
            except (ValueError, KeyError):
                continue
            if (today - d).days <= RECENT_DISCLOSURE_DAYS:
                return True
        return False

    row["guidance_revised"] = recent("業績予想の修正")
    row["dividend_revised"] = recent("配当予想の修正")


def main() -> int:
    if not IPO_LIST.exists():
        print(f"{IPO_LIST} がありません。fetch_ipo_jp.py を先に動かしてください。",
              file=sys.stderr)
        return 1

    src = json.loads(IPO_LIST.read_text(encoding="utf-8"))
    ipos = src.get("ipos", [])
    listed = [r for r in ipos if r.get("listed")]
    print(f"対象 {len(ipos)} 件（上場済み {len(listed)}）", file=sys.stderr)

    tickers = [f"{r['code']}.T" for r in listed]
    frames: dict[str, pd.DataFrame] = {}
    for i in range(0, len(tickers), BATCH_SIZE):
        chunk = tickers[i:i + BATCH_SIZE]
        try:
            frames.update(fetch_ohlcv(chunk))
        except Exception as e:
            print(f"  {i//BATCH_SIZE + 1}バッチ目の取得に失敗: {e}", file=sys.stderr)
        print(f"  {min(i + BATCH_SIZE, len(tickers))}/{len(tickers)}", file=sys.stderr)

    profiles = load_profiles()
    print(f"会社プロフィール {len(profiles)} 件を読み込みました", file=sys.stderr)

    tdnet, tdnet_since = load_tdnet()
    today = datetime.now(JST).date()
    print(f"適時開示 {len(tdnet)} 銘柄ぶん（収集開始 {tdnet_since or '—'}）",
          file=sys.stderr)

    chart: dict[str, list] = {}
    out_rows = []
    failed = 0

    for r in ipos:
        row = dict(r)
        ticker = f"{r['code']}.T"
        row["ticker"] = ticker

        attach_disclosures(row, tdnet.get(r["code"], []), today)

        prof = profiles.get(r["code"])
        if prof:
            for k in PROFILE_FIELDS:
                v = prof.get(k)
                if v not in (None, ""):
                    row[k] = v

        if not r.get("listed"):
            row["status"] = "upcoming"
            out_rows.append(row)
            continue

        sub = frames.get(ticker)
        if sub is not None and not sub.empty:
            sub = clean_bars(sub, r.get("listing_date"))
        if sub is None or sub.empty:
            failed += 1
            row["status"] = "no_data"
            row["fetch_failed"] = True
            out_rows.append(row)
            continue

        row["status"] = "listed"
        row.update(metrics(sub, r.get("offer_price")))
        fm = flow_metrics(sub)
        row.update(fm)
        row["flow_score"] = flow_score(fm)
        rows = series_rows(sub)
        if rows:
            chart[ticker] = rows
        out_rows.append(row)

    now = datetime.now(JST)
    payload = {
        "asof": src.get("asof"),
        "generated_at_jst": now.isoformat(timespec="seconds"),
        "source": src.get("source"),
        "years_back": src.get("years_back"),
        "count": len(out_rows),
        "upcoming": sum(1 for r in out_rows if r["status"] == "upcoming"),
        "listed_count": sum(1 for r in out_rows if r["status"] == "listed"),
        "no_data": failed,
        "ma_short": MA_SHORT,
        "ma_long": MA_LONG,
        "flow_criteria": {
            "window_short": FLOW_SHORT, "window_long": FLOW_LONG,
            "vol_ratio": FLOW_VOL_RATIO, "up_volume_share": FLOW_UP_SHARE,
            "min_turnover": MIN_TURNOVER,
        },
        "flow_judgeable_count": sum(1 for r in out_rows if r.get("flow_judgeable")),
        # いつから適時開示を集めているか。これより前の決算は取得できない。
        "tdnet_since": tdnet_since,
        "tdnet_stocks": len(tdnet),
        "ipos": out_rows,
    }

    for path, data in ((OUT_PAGE, payload), (OUT_CHART, chart)):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    print(f"{payload['listed_count']} 銘柄の値動きを計算（取得失敗 {failed}）",
          file=sys.stderr)
    print(f"チャートデータ {len(chart)} 銘柄", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
