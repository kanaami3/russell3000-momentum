import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import yfinance as yf

OUT = Path(__file__).resolve().parent.parent.parent / "web" / "data" / "sector" / "market_data.json"

# 業種ごとの代表銘柄バスケット(等ウェイトで業種指数の代替とする)
SECTOR_BASKETS = {
    "半導体":     ["8035.T", "6857.T", "6146.T", "6920.T", "6723.T"],
    "電気機器":   ["6501.T", "6503.T", "6752.T", "6758.T", "6981.T"],
    "情報通信":   ["9432.T", "9433.T", "9613.T", "4689.T", "9984.T"],
    "医薬品":     ["4502.T", "4503.T", "4568.T", "4519.T", "4523.T"],
    "銀行":       ["8306.T", "8316.T", "8411.T", "8308.T", "7186.T"],
    "自動車":     ["7203.T", "7267.T", "7201.T", "7269.T", "7261.T"],
    "機械":       ["6301.T", "6326.T", "7011.T", "7013.T", "6273.T"],
    "商社":       ["8001.T", "8002.T", "8031.T", "8053.T", "8058.T"],
    "小売":       ["9983.T", "3382.T", "8267.T", "2914.T", "7532.T"],
    "不動産":     ["8801.T", "8802.T", "8830.T", "3289.T", "8804.T"],
    "化学":       ["4063.T", "4188.T", "4452.T", "4901.T", "3407.T"],
    "海運":       ["9101.T", "9104.T", "9107.T"],
    "鉄鋼":       ["5401.T", "5411.T", "5406.T"],
    "食品":       ["2802.T", "2503.T", "2269.T", "2801.T"],
}
BENCHMARK = "1306.T"  # TOPIX連動ETF

# 週次ブリーフのテーブル用グループ(等ウェイト vs 時価総額ウェイト)
BRIEF_GROUPS = {
    "日本 半導体関連": ["8035.T", "6857.T", "6146.T", "6920.T", "6723.T", "4063.T"],
    "米国 半導体関連": ["NVDA", "AMD", "AVGO", "MU", "TSM"],
    "米国 Growth":     ["MSFT", "AAPL", "GOOGL", "AMZN", "META"],
    "米国 Value":      ["BRK-B", "JPM", "JNJ", "XOM", "PG"],
}


def download_closes(tickers: list[str], period: str = "2y") -> pd.DataFrame:
    data = yf.download(tickers, period=period, interval="1d",
                       auto_adjust=True, progress=False, group_by="ticker")
    out = {}
    for t in tickers:
        try:
            s = data[t]["Close"].dropna() if len(tickers) > 1 else data["Close"].dropna()
            if len(s) > 30:
                out[t] = s
        except (KeyError, TypeError):
            print(f"  [warn] {t}: スキップ")
    return pd.DataFrame(out)


def build_sector_series() -> tuple[pd.DataFrame, pd.Series]:
    """業種ごとの日次指数(等ウェイト)とTOPIX日次終値を返す。

    旧: daily.resample("W-FRI").last() で週足に丸めていたが、
    セクターローテーションを日次粒度で追えるようにするため
    リサンプルを廃止し、日足の終値をそのまま使用する。
    """
    all_tickers = sorted({t for ts in SECTOR_BASKETS.values() for t in ts} | {BENCHMARK})
    print(f"日次データ取得: {len(all_tickers)}銘柄")
    daily = download_closes(all_tickers)
    weekly = daily.dropna(how="all")  # 変数名は既存コードとの互換のため維持(中身は日次)

    sectors = {}
    for name, ts in SECTOR_BASKETS.items():
        cols = [t for t in ts if t in weekly.columns]
        if not cols:
            continue
        norm = weekly[cols] / weekly[cols].iloc[0]      # 各銘柄を1に正規化
        sectors[name] = norm.mean(axis=1)               # 等ウェイト平均=業種指数の代替
    bench = weekly[BENCHMARK] / weekly[BENCHMARK].iloc[0]
    return pd.DataFrame(sectors).dropna(), bench.dropna()


def calc_brief_table(lookback: int = 5) -> list[dict]:
    """等ウェイト/時価総額ウェイト騰落率(直近5営業日)。"""
    rows = []
    for name, ts in BRIEF_GROUPS.items():
        prices = download_closes(ts, period="3mo")
        if prices.empty or len(prices) <= lookback:
            continue
        rets = prices.iloc[-1] / prices.iloc[-1 - lookback] - 1.0
        caps = {}
        for t in prices.columns:
            try:
                c = getattr(yf.Ticker(t).fast_info, "market_cap", None)
                if c:
                    caps[t] = float(c)
            except Exception:
                pass
        ew = float(rets.mean()) * 100
        cw = None
        avail = [t for t in rets.index if t in caps]
        if avail:
            w = pd.Series({t: caps[t] for t in avail})
            cw = float((rets[avail] * (w / w.sum())).sum()) * 100
        rows.append({"name": name, "ew": round(ew, 2),
                     "cw": round(cw, 2) if cw is not None else None})
    return rows


def main():
    sectors, bench = build_sector_series()
    idx = sectors.index.intersection(bench.index)
    sectors, bench = sectors.loc[idx], bench.loc[idx]

    # 相対力 = 業種 / TOPIX (フロント側でRRG座標を計算)
    result = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "benchmark": "TOPIX(1306)",
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "sectors": [
            {"name": name, "rs": [round(float(v), 5) for v in (sectors[name] / bench)]}
            for name in sectors.columns
        ],
        "brief_table": calc_brief_table(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(f"完了: {OUT} ({len(idx)}日 × {len(sectors.columns)}業種)")


if __name__ == "__main__":
    main()
