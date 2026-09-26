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
    }


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

    chart: dict[str, list] = {}
    out_rows = []
    failed = 0

    for r in ipos:
        row = dict(r)
        ticker = f"{r['code']}.T"
        row["ticker"] = ticker

        if not r.get("listed"):
            row["status"] = "upcoming"
            out_rows.append(row)
            continue

        sub = frames.get(ticker)
        if sub is None or sub.empty:
            failed += 1
            row["status"] = "no_data"
            row["fetch_failed"] = True
            out_rows.append(row)
            continue

        row["status"] = "listed"
        row.update(metrics(sub, r.get("offer_price")))
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
