"""
セクター別 移動平均乖離率ランキング — データ取得パイプライン (yfinance無料版)
============================================================================
universe.py の主要銘柄ユニバースを yfinance で取得し、各銘柄の
移動平均乖離率を計算して、業種ごとに乖離の大きい順トップ50を
data/deviation.json に出力する。認証不要・当日データ。

乖離率 = (直近終値 / N日単純移動平均 - 1) * 100   (Nは MA_WINDOWS)

使い方:
    pip install yfinance pandas
    python pipeline/fetch_deviation.py
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

from universe import SECTOR_UNIVERSE, all_pairs

OUT = Path(__file__).resolve().parent.parent.parent / "web" / "data" / "sector" / "deviation.json"

MA_WINDOWS = [25, 75]     # 計算する移動平均日数(先頭が主指標=ランキング基準)
PRIMARY_MA = MA_WINDOWS[0]
TOP_N = 50                # 業種ごとに出す上位件数


def download_closes(tickers: list[str], period: str = "6mo") -> pd.DataFrame:
    """日次終値(調整後)のフレーム。列は ".T" 無しの4桁コード。"""
    data = yf.download(tickers, period=period, interval="1d",
                       auto_adjust=True, progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if len(s) > PRIMARY_MA:
                out[t.replace(".T", "")] = s
        except (KeyError, TypeError):
            continue
    return pd.DataFrame(out)


def _rnd(v, nd):
    """None/NaN/異常値を弾いて丸める。"""
    try:
        if v is None:
            return None
        v = float(v)
        if v != v or abs(v) > 1e7:     # NaN / 異常値
            return None
        return round(v, nd)
    except (TypeError, ValueError):
        return None


def fetch_fundamentals(codes: list[str]) -> dict:
    """各銘柄の PER(実績)・PBR・配当利回り(%)・1株配当(円) を取得。
    yfinance の .info は1銘柄ずつのため時間がかかる。取れない値は None。"""
    out = {}
    for n, code in enumerate(codes, 1):
        try:
            i = yf.Ticker(f"{code}.T").info
            out[code] = {
                "per": _rnd(i.get("trailingPE"), 1),
                "pbr": _rnd(i.get("priceToBook"), 2),
                "divy": _rnd(i.get("dividendYield"), 2),   # 既に%表記
                "dps": _rnd(i.get("dividendRate"), 0),      # 1株あたり年間配当(円)
            }
        except Exception:
            out[code] = {}
        if n % 50 == 0:
            print(f"  ファンダ取得 {n}/{len(codes)}")
    return out


def main():
    pairs = all_pairs()                       # [(sector, code, name), ...]
    name_map = {code: name for _, code, name in pairs}
    sector_map = {code: sector for sector, code, _ in pairs}
    tickers = [f"{code}.T" for _, code, _ in pairs]
    print(f"取得対象: {len(tickers)}銘柄 / {len(SECTOR_UNIVERSE)}業種")

    prices = download_closes(tickers)
    if prices.empty:
        raise SystemExit("価格データを取得できませんでした")
    as_of = prices.index[-1].strftime("%Y-%m-%d")

    # 各銘柄の乖離率を計算
    records = []
    for code in prices.columns:
        s = prices[code].dropna()
        if len(s) < PRIMARY_MA:
            continue
        close = float(s.iloc[-1])
        devs = {}
        for w in MA_WINDOWS:
            if len(s) >= w:
                ma = float(s.iloc[-w:].mean())
                if ma > 0:
                    devs[f"dev{w}"] = round((close / ma - 1) * 100, 2)
        if f"dev{PRIMARY_MA}" not in devs:
            continue
        records.append({
            "code": code,
            "name": name_map.get(code, code),
            "sector": sector_map.get(code, "その他"),
            "close": round(close, 1),
            **devs,
        })

    # PER/PBR/配当を付与(乖離が算出できた銘柄のみ)
    print(f"ファンダメンタルズ取得: {len(records)}銘柄")
    fund = fetch_fundamentals([r["code"] for r in records])
    for r in records:
        r.update(fund.get(r["code"], {}))

    # 業種ごとに |主指標乖離| の大きい順トップN
    primary_key = f"dev{PRIMARY_MA}"
    by_sector: dict[str, list] = {}
    for r in records:
        by_sector.setdefault(r["sector"], []).append(r)

    sectors_out = []
    for sector in SECTOR_UNIVERSE:            # 定義順を維持
        rows = by_sector.get(sector, [])
        rows.sort(key=lambda r: abs(r[primary_key]), reverse=True)
        sectors_out.append({
            "name": sector,
            "count": len(rows),
            "stocks": rows[:TOP_N],
        })

    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "as_of": as_of,
        "source": "yfinance(主要銘柄ユニバース)",
        "ma_windows": MA_WINDOWS,
        "primary_ma": PRIMARY_MA,
        "top_n": TOP_N,
        "sectors": sectors_out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    total = sum(s["count"] for s in sectors_out)
    print(f"完了: {OUT} ({len(sectors_out)}業種 / {total}銘柄 / 基準日 {as_of})")


if __name__ == "__main__":
    main()
