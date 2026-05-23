"""Fetch and analyze key indices for the index-monitor tab.

For each tracked index:
  - latest price + daily change
  - technical indicators: RSI(14), MACD(12/26/9), Bollinger Bands(20,2),
    ATR(14), SMA(20/50/200)
  - trend judgment (強い上昇 / 上昇 / 中立 / 下落 / 強い下落)
  - 3-year daily OHLCV for chart display

Also computes pairwise 30-day correlations for cross-asset context.

Output: web/data/indices.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "indices.json"

# Tracked indices. Add more here if needed.
INDICES = [
    {"id": "N225",   "yf": "^N225",  "label": "日経225",    "category": "equity_index"},
    {"id": "NDX",    "yf": "^NDX",   "label": "NASDAQ100",  "category": "equity_index"},
    {"id": "USDJPY", "yf": "JPY=X",  "label": "USD/JPY",    "category": "fx"},
]

PERIOD = "3y"        # yfinance history length
CHART_DAYS = 252 * 3  # roughly 3 years of trading days


# ---------------------------------------------------------------------------
# Technical indicators
# ---------------------------------------------------------------------------

def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.where(delta > 0, 0.0).rolling(period).mean()
    loss = (-delta.where(delta < 0, 0.0)).rolling(period).mean()
    rs = gain / loss.replace(0, pd.NA)
    return 100 - (100 / (1 + rs))


def macd(closes: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_fast = closes.ewm(span=fast, adjust=False).mean()
    ema_slow = closes.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    return macd_line, signal_line, macd_line - signal_line


def bollinger(closes: pd.Series, period: int = 20, sd: float = 2.0):
    mid = closes.rolling(period).mean()
    std = closes.rolling(period).std()
    return mid + sd * std, mid, mid - sd * std


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def trend_judgement(close: float, sma20: float, sma50: float, sma200: float) -> str:
    """Combine perfect-order checks with 200-day relationship."""
    if any(pd.isna(v) for v in (sma200, sma50, sma20)):
        return "判定不能"
    bullish_arrange = close > sma20 > sma50 > sma200
    bearish_arrange = close < sma20 < sma50 < sma200
    if bullish_arrange: return "強い上昇"
    if bearish_arrange: return "強い下落"
    if close > sma200 and sma20 > sma50: return "上昇"
    if close < sma200 and sma20 < sma50: return "下落"
    return "中立"


def _safe(v) -> float | None:
    if v is None or pd.isna(v):
        return None
    return round(float(v), 2)


# ---------------------------------------------------------------------------
# Per-index analysis
# ---------------------------------------------------------------------------

def analyze_index(spec: dict) -> dict | None:
    print(f"  Fetching {spec['label']} ({spec['yf']})...", file=sys.stderr)
    try:
        df = yf.Ticker(spec["yf"]).history(period=PERIOD, interval="1d", auto_adjust=True)
    except Exception as e:
        print(f"    ERROR: {e}", file=sys.stderr)
        return None
    if df.empty or len(df) < 50:
        print(f"    skipped: insufficient data ({len(df)} rows)", file=sys.stderr)
        return None

    df.columns = [c.lower() for c in df.columns]
    df = df[["open", "high", "low", "close", "volume"]].dropna(subset=["close"])

    closes = df["close"]
    df["rsi14"] = rsi(closes, 14)
    df["sma20"] = closes.rolling(20).mean()
    df["sma50"] = closes.rolling(50).mean()
    df["sma200"] = closes.rolling(200).mean()
    df["macd"], df["macd_signal"], df["macd_hist"] = macd(closes)
    df["bb_upper"], df["bb_mid"], df["bb_lower"] = bollinger(closes)
    df["atr14"] = atr(df["high"], df["low"], df["close"], 14)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) >= 2 else last
    close = float(last["close"])
    change = close - float(prev["close"])
    change_pct = change / float(prev["close"]) * 100 if prev["close"] else 0.0

    # Position within Bollinger Bands (0% = lower band, 100% = upper band)
    bb_width = float(last["bb_upper"]) - float(last["bb_lower"]) if pd.notna(last["bb_upper"]) else 0
    bb_position = (close - float(last["bb_lower"])) / bb_width * 100 if bb_width > 0 else None

    # ATR % of close
    atr_pct = float(last["atr14"]) / close * 100 if pd.notna(last["atr14"]) and close > 0 else None

    indicators = {
        "rsi14": _safe(last["rsi14"]),
        "sma20": _safe(last["sma20"]),
        "sma50": _safe(last["sma50"]),
        "sma200": _safe(last["sma200"]),
        "macd": _safe(last["macd"]),
        "macd_signal": _safe(last["macd_signal"]),
        "macd_hist": _safe(last["macd_hist"]),
        "bb_upper": _safe(last["bb_upper"]),
        "bb_mid": _safe(last["bb_mid"]),
        "bb_lower": _safe(last["bb_lower"]),
        "bb_position_pct": _safe(bb_position),
        "atr14": _safe(last["atr14"]),
        "atr_pct": _safe(atr_pct),
        "trend": trend_judgement(close, last["sma20"], last["sma50"], last["sma200"]),
        "above_sma200": bool(close > float(last["sma200"])) if pd.notna(last["sma200"]) else None,
        "above_sma50":  bool(close > float(last["sma50"]))  if pd.notna(last["sma50"])  else None,
        "sma20_above_sma50": bool(last["sma20"] > last["sma50"]) if pd.notna(last["sma20"]) and pd.notna(last["sma50"]) else None,
        "macd_bullish": bool(last["macd"] > last["macd_signal"]) if pd.notna(last["macd"]) and pd.notna(last["macd_signal"]) else None,
        "pct_from_sma200": _safe((close - float(last["sma200"])) / float(last["sma200"]) * 100) if pd.notna(last["sma200"]) and last["sma200"] > 0 else None,
    }

    # 3-year OHLCV history for chart (compact tuple format)
    tail = df.tail(CHART_DAYS)
    history = []
    macd_hist = []
    for idx, r in tail.iterrows():
        date = idx.date().isoformat()
        c = float(r["close"])
        if pd.isna(c): continue
        history.append([
            date,
            round(float(r["open"]), 2),
            round(float(r["high"]), 2),
            round(float(r["low"]), 2),
            round(c, 2),
            int(float(r["volume"]) if pd.notna(r["volume"]) else 0),
        ])
        if pd.notna(r["macd"]) and pd.notna(r["macd_signal"]):
            macd_hist.append([date, round(float(r["macd"]), 2), round(float(r["macd_signal"]), 2), round(float(r["macd_hist"]), 2)])

    return {
        "id": spec["id"],
        "label": spec["label"],
        "category": spec["category"],
        "yf_symbol": spec["yf"],
        "current": {
            "value": round(close, 2),
            "change": round(change, 2),
            "change_pct": round(change_pct, 2),
            "date": str(tail.index[-1].date()),
        },
        "indicators": indicators,
        "history": history,
        "macd_history": macd_hist,
    }


def compute_correlations(indices: list[dict]) -> list[dict]:
    """Pairwise 30-day daily-return correlations."""
    # Build aligned price dataframe from histories
    series = {}
    for ix in indices:
        if not ix or not ix.get("history"):
            continue
        h = ix["history"][-60:]  # last 60 days for some buffer
        s = pd.Series({row[0]: row[4] for row in h})  # date → close
        series[ix["id"]] = s
    if len(series) < 2:
        return []

    df = pd.DataFrame(series).sort_index()
    rets = df.pct_change().dropna()
    last30 = rets.tail(30)

    out = []
    ids = list(series.keys())
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if a in last30.columns and b in last30.columns:
                c = last30[a].corr(last30[b])
                if pd.notna(c):
                    out.append({
                        "a": a,
                        "b": b,
                        "period_days": 30,
                        "value": round(float(c), 2),
                    })
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    print("Fetching indices...", file=sys.stderr)
    results = []
    for spec in INDICES:
        r = analyze_index(spec)
        if r:
            results.append(r)

    if not results:
        print("ERROR: no index data fetched", file=sys.stderr)
        return 1

    correlations = compute_correlations(results)

    asof = max((r["current"]["date"] for r in results), default="")
    output = {
        "asof": asof,
        "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "indices": results,
        "correlations": correlations,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)", file=sys.stderr)
    for r in results:
        ind = r["indicators"]
        print(
            f"  {r['label']}: {r['current']['value']} ({r['current']['change_pct']:+.2f}%) "
            f"trend={ind['trend']} RSI={ind['rsi14']} 200日線比={ind.get('pct_from_sma200','-')}%",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
