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
import time
from pathlib import Path

import pandas as pd
import yfinance as yf

# Yahoo Finance occasionally rate-limits GitHub Actions IPs, returning stale or
# empty data. Using curl_cffi's browser-impersonated TLS fingerprint (Chrome)
# bypasses this. Falls back to a normal session if curl_cffi is unavailable.
try:
    from curl_cffi import requests as creq
    _YF_SESSION = creq.Session(impersonate="chrome")
    print("[fetch_indices] using curl_cffi (Chrome impersonation)", file=sys.stderr)
except ImportError:
    _YF_SESSION = None
    print("[fetch_indices] curl_cffi unavailable — using default session", file=sys.stderr)

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "indices.json"

# Tracked indices. Each may list fallback symbols tried in order if the
# primary fails — yfinance occasionally rate-limits GitHub Actions IPs on
# certain symbols (notably ^N225), so we keep alternates ready.
INDICES = [
    # ^N225 (cash index) is the true value but Yahoo lags it 1-2 days; NIY=F
    # (CME Nikkei futures) tracks it closely (~0.3% basis) and stays current,
    # so it's the staleness fallback. (^NKX is delisted — removed.)
    {"id": "N225",   "yf": "^N225",  "fallbacks": ["NIY=F"],
     "label": "日経225",    "category": "equity_index"},
    {"id": "NDX",    "yf": "^NDX",   "fallbacks": ["QQQ"],
     "label": "NASDAQ100",  "category": "equity_index"},
    {"id": "USDJPY", "yf": "JPY=X",  "fallbacks": ["USDJPY=X"],
     "label": "USD/JPY",    "category": "fx"},
]

MAX_RETRIES = 3
RETRY_DELAY_SEC = 5

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


def macd_phase(macd_line: float, signal: float) -> str:
    """4-state interpretation of MACD position.

    - 強気継続 : MACD > signal AND MACD > 0    (uptrend momentum intact)
    - 強気転換 : MACD > signal AND MACD < 0    (bullish crossover, base building)
    - 失速     : MACD < signal AND MACD > 0    (still positive but losing steam)
    - 弱気継続 : MACD < signal AND MACD < 0    (downtrend momentum intact)
    """
    if pd.isna(macd_line) or pd.isna(signal):
        return "判定不能"
    above_signal = macd_line > signal
    above_zero = macd_line > 0
    if above_signal and above_zero:    return "強気継続"
    if above_signal and not above_zero: return "強気転換"
    if not above_signal and above_zero: return "失速"
    return "弱気継続"


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

def _try_fetch(symbol: str) -> pd.DataFrame | None:
    """Fetch with retry. Returns None if all attempts fail."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            ticker = yf.Ticker(symbol, session=_YF_SESSION) if _YF_SESSION else yf.Ticker(symbol)
            df = ticker.history(period=PERIOD, interval="1d", auto_adjust=True)
            if not df.empty and len(df) >= 50:
                return df
            print(f"    {symbol}: attempt {attempt} got {len(df)} rows (need ≥50)", file=sys.stderr)
        except Exception as e:
            print(f"    {symbol}: attempt {attempt} ERROR: {e}", file=sys.stderr)
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_DELAY_SEC)
    return None


def analyze_index(spec: dict) -> dict | None:
    print(f"  Fetching {spec['label']} ({spec['yf']})...", file=sys.stderr)

    # Fetch the primary AND every fallback, then pick whichever has the most
    # RECENT latest bar. Yahoo's cash-index tickers (notably ^N225) sometimes
    # return valid-but-stale data — 1-2 days behind — so a plain "use the first
    # that succeeds" approach silently shows old prices. Comparing latest dates
    # and preferring the freshest source fixes that. Ties prefer the primary
    # (the true cash index) over futures, since the primary is listed first.
    candidates = [spec["yf"], *spec.get("fallbacks", [])]
    best_df = None
    best_symbol = None
    best_date = None
    for sym in candidates:
        df = _try_fetch(sym)
        if df is None:
            continue
        latest = df.index[-1].date()
        print(f"    {sym}: latest bar {latest}", file=sys.stderr)
        if best_date is None or latest > best_date:
            best_df, best_symbol, best_date = df, sym, latest

    if best_df is None:
        print(f"    SKIPPED {spec['label']}: all symbols failed", file=sys.stderr)
        return None

    df = best_df
    used_symbol = best_symbol
    if used_symbol != spec["yf"]:
        print(f"    Using fresher source {used_symbol} (latest {best_date}) "
              f"instead of stale {spec['yf']}", file=sys.stderr)

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
        "macd_phase": macd_phase(last["macd"], last["macd_signal"]),
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
        "yf_symbol": used_symbol,
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
