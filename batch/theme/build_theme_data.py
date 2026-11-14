#!/usr/bin/env python3
"""テーマごとの相対力の推移と、テーマ内の銘柄一覧を書き出す。

出力は2つ。

  web/data/theme/market_data.json
      セクターローテーションと**同じ形**にしてある。chart側の描画処理を
      そのまま使えるようにするため。キー名が "sectors" なのはそのせい。
      中身はテーマだが、構造を合わせることを優先した。

  web/data/theme/stocks.json
      テーマごとの銘柄一覧。前日比・出来高・売買代金。

**等ウェイトで指数を作る。**
時価総額で重み付けすると、トヨタやソフトバンクGが入るテーマはその1社の
動きになる。テーマの強弱を見たいので、各銘柄を同じ重みで扱う。

**各系列を期初=1に揃えてから割る。**
株価の水準はバラバラなので、そのまま平均すると値がさ株に引っ張られる。
日々の騰落率から累積指数を作り、TOPIXの累積指数で割る。

**時価総額は入れていない。**
yfinance で時価総額を取るには Ticker.info を1銘柄ずつ叩く必要があり、
100銘柄だと時間もレート制限も厳しい。前日比・出来高・売買代金は
株価データだけで出せるので、まずはそこまでにしてある。
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

sys.path.insert(0, str(Path(__file__).resolve().parent))
from themes import THEME_BASKETS, BENCHMARK, all_tickers  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
OUT_DIR = REPO_ROOT / "web" / "data" / "theme"

JST = timezone(timedelta(hours=9))
PERIOD = os.getenv("THEME_PERIOD", "2y")

# 相対力の推移として持つ日数。チャートは最大でも数十営業日しか描かないが、
# 期間を変えて見られるよう少し長めに残す。
KEEP_DAYS = int(os.getenv("THEME_KEEP_DAYS", "260"))

# 東証の大引け(15:30)。データが確定するまで少し余裕を見る。
# これより前に走った場合、当日の足は場中の途中値なので捨てる。
# (GitHub Actions の cron は数時間遅れることがあり、朝の便が場中に
#  ずれ込んで前場の途中値を「当日の終値」として書いていた)
CLOSE_CUTOFF = (15, 45)


def drop_intraday_bar(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    """場中に取得した当日の足(未確定)を落とす。"""
    if df.empty:
        return df
    last = df.index[-1]
    last_date = last.date() if hasattr(last, "date") else last
    if last_date == now.date() and (now.hour, now.minute) < CLOSE_CUTOFF:
        print(f"  当日 {last_date} の足は場中の途中値のため除外します", file=sys.stderr)
        return df.iloc[:-1]
    return df


def download(tickers: list[str]) -> pd.DataFrame:
    df = yf.download(tickers, period=PERIOD, interval="1d",
                     auto_adjust=True, progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = df[t]["Close"].dropna() if len(tickers) > 1 else df["Close"].dropna()
            if len(s) > 30:
                out[t] = s
        except (KeyError, TypeError):
            print(f"  [warn] {t}: 取得できず", file=sys.stderr)
    return pd.DataFrame(out)


def volumes(tickers: list[str]) -> pd.DataFrame:
    df = yf.download(tickers, period="3mo", interval="1d",
                     auto_adjust=False, progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = df[t]["Volume"].dropna() if len(tickers) > 1 else df["Volume"].dropna()
            if len(s) > 2:
                out[t] = s
        except (KeyError, TypeError):
            continue
    return pd.DataFrame(out)


def equal_weight_index(closes: pd.DataFrame, codes: list[str]) -> pd.Series | None:
    """等ウェイトの累積指数。期初=1。

    日々の騰落率を銘柄間で平均してから積み上げる。価格をそのまま平均すると
    値がさ株の影響が大きくなりすぎる。
    """
    cols = [f"{c}.T" for c in codes if f"{c}.T" in closes.columns]
    if not cols:
        return None
    rets = closes[cols].pct_change().mean(axis=1)
    return (1 + rets.fillna(0)).cumprod()


def main() -> int:
    tickers = all_tickers()
    print(f"{len(tickers)} 銘柄を取得します", file=sys.stderr)
    now = datetime.now(JST)
    closes = drop_intraday_bar(download(tickers), now)
    if closes.empty or BENCHMARK not in closes.columns:
        print("株価が取得できませんでした。既存ファイルを維持します。", file=sys.stderr)
        return 1

    bench = (1 + closes[BENCHMARK].pct_change().fillna(0)).cumprod()

    series = []
    for name, rows in THEME_BASKETS.items():
        idx = equal_weight_index(closes, [c for c, _ in rows])
        if idx is None:
            print(f"  [warn] {name}: 構成銘柄が取れず除外", file=sys.stderr)
            continue
        rs = (idx / bench).tail(KEEP_DAYS)
        series.append({"name": name, "rs": [round(float(v), 5) for v in rs]})

    dates = [d.date().isoformat() for d in closes.index[-KEEP_DAYS:]]

    market = {
        "generated_at": now.isoformat(timespec="seconds"),
        "benchmark": BENCHMARK,
        "dates": dates,
        # キー名はセクター側と合わせてある（描画処理を共用するため）
        "sectors": series,
        "brief_table": [],
    }

    # ---- テーマごとの銘柄一覧 ----
    vol = volumes(tickers)
    if not vol.empty:
        # 終値と同じ日の出来高を使う(場中の当日分は落としてある)
        vol = vol[vol.index <= closes.index[-1]]
    stocks = {}
    for name, rows in THEME_BASKETS.items():
        items = []
        for code, jp in rows:
            t = f"{code}.T"
            if t not in closes.columns or len(closes[t].dropna()) < 2:
                # 取れなかった銘柄は「変化なし」ではなく「不明」として出す
                items.append({"code": code, "name": jp, "change_pct": None,
                              "volume": None, "turnover": None})
                continue
            s = closes[t].dropna()
            chg = (float(s.iloc[-1]) / float(s.iloc[-2]) - 1) * 100
            v = None
            if t in vol.columns and len(vol[t].dropna()):
                v = int(vol[t].dropna().iloc[-1])
            items.append({
                "code": code, "name": jp,
                "price": round(float(s.iloc[-1]), 1),
                "change_pct": round(chg, 2),
                "volume": v,
                # 売買代金 = 終値 × 出来高。概算だが桁感は掴める。
                "turnover": int(float(s.iloc[-1]) * v) if v else None,
            })
        items.sort(key=lambda r: (r["change_pct"] is None, -(r["change_pct"] or 0)))
        stocks[name] = items

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for path, data in ((OUT_DIR / "market_data.json", market),
                       (OUT_DIR / "stocks.json",
                        {"generated_at": now.isoformat(timespec="seconds"),
                         "asof": dates[-1] if dates else None,
                         "themes": stocks})):
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    print(f"テーマ {len(series)} 件 / 日付 {len(dates)} 日分を書き出しました",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
