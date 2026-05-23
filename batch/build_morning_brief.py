"""Build the morning day-trade brief for the JP market.

Produces web/data/morning_brief_jp.json with:
  - macro_signals: overnight changes for Nikkei futures, USDJPY, S&P500,
    NASDAQ, SOX, VIX, crude, gold, etc.
  - sector_signals: overnight US sector ETF changes mapped to JP 17-sector
    classifications (so a hot US sector flags relevant JP stocks).
  - gap_candidates: JP stocks in hot/cold US sectors, ranked by liquidity.
  - picks: 6 day-trade ranking lists derived from yesterday's JP OHLCV
    (combined score, volume surge, range, turnover, momentum continuation,
    reversal candidates).

Inputs:
  - data/universe_jp.json
  - data/prices_jp.csv (OHLCV — produced by fetch_prices.py jp)

Output:
  - web/data/morning_brief_jp.json
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd
import yfinance as yf

REPO_ROOT = Path(__file__).resolve().parent.parent
UNIVERSE_PATH = REPO_ROOT / "data" / "universe_jp.json"
PRICES_PATH = REPO_ROOT / "data" / "prices_jp.csv"
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"

# Filters for day-trade tradability
MIN_AVG_VOLUME = 10_000        # 1万株/日 以上
MIN_CLOSE_YEN = 100            # 100円超
MAX_CLOSE_YEN = 100_000        # 10万円未満 (一般的なデイトレ予算で扱える上限)

# Overnight macro signals to fetch
MACRO_SYMBOLS = {
    "NKD=F":  {"label": "日経225先物 (CME)",        "category": "macro_jp"},
    "^GSPC":  {"label": "S&P 500",                  "category": "macro_us"},
    "^NDX":   {"label": "NASDAQ 100",               "category": "macro_us"},
    "^SOX":   {"label": "SOX (米半導体指数)",        "category": "macro_us"},
    "^VIX":   {"label": "VIX (恐怖指数)",            "category": "macro_us"},
    "JPY=X":  {"label": "USD/JPY",                  "category": "fx"},
    "CL=F":   {"label": "WTI原油",                   "category": "commodity"},
    "GC=F":   {"label": "金",                        "category": "commodity"},
    "BTC-USD":{"label": "Bitcoin",                  "category": "crypto"},
}

# US sector ETFs → JP 17-sector mapping
# Note: JPX uses fullwidth parens in "金融(除く銀行)" — exact-match required.
SECTOR_ETFS = {
    "XLK":  {"label": "テクノロジー",  "jp_sectors": ["電機・精密", "情報通信・サービスその他"]},
    "SOXX": {"label": "半導体",        "jp_sectors": ["電機・精密"]},
    "XLF":  {"label": "金融",          "jp_sectors": ["銀行", "金融（除く銀行）"]},
    "XLE":  {"label": "エネルギー",    "jp_sectors": ["エネルギー資源", "商社・卸売"]},
    "XLY":  {"label": "一般消費財",    "jp_sectors": ["自動車・輸送機", "小売"]},
    "XLI":  {"label": "資本財",        "jp_sectors": ["機械", "鉄鋼・非鉄", "商社・卸売", "運輸・物流", "建設・資材"]},
    "XLP":  {"label": "生活必需品",    "jp_sectors": ["食品", "小売"]},
    "XLV":  {"label": "ヘルスケア",    "jp_sectors": ["医薬品"]},
    "XLB":  {"label": "素材",          "jp_sectors": ["素材・化学", "鉄鋼・非鉄"]},
    "XLU":  {"label": "公益",          "jp_sectors": ["電力・ガス"]},
    "XLC":  {"label": "通信サービス",  "jp_sectors": ["情報通信・サービスその他"]},
    "VNQ":  {"label": "不動産",        "jp_sectors": ["不動産"]},
}

GAP_THRESHOLD = 1.0      # ETF動きがこの%超で「注目セクター」候補に含める
TOP_HOT_SECTORS = 3      # 騰落上位N個までは閾値未達でも取り上げる(その日が静かでも候補が出る)
GAP_CANDIDATES_PER_SECTOR = 5


# ---------------------------------------------------------------------------
# Overnight macro & sector fetch
# ---------------------------------------------------------------------------

def fetch_overnight() -> tuple[list[dict], list[dict]]:
    """Fetch latest macro/sector data via yfinance, compute overnight change.

    Returns (macro_signals, sector_signals).
    """
    all_symbols = list(MACRO_SYMBOLS.keys()) + list(SECTOR_ETFS.keys())
    print(f"Fetching overnight data: {len(all_symbols)} symbols...", file=sys.stderr)

    macro_signals = []
    sector_signals = []
    for ticker in all_symbols:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d", auto_adjust=False)
            if len(hist) < 2:
                continue
            last = hist["Close"].iloc[-1]
            prev = hist["Close"].iloc[-2]
            if pd.isna(last) or pd.isna(prev) or prev == 0:
                continue
            change_pct = float((last - prev) / prev * 100)
            value = float(last)
            entry = {
                "ticker": ticker,
                "value": round(value, 2),
                "change_pct": round(change_pct, 2),
                "last_date": str(hist.index[-1].date()),
            }
            if ticker in MACRO_SYMBOLS:
                entry["label"] = MACRO_SYMBOLS[ticker]["label"]
                entry["category"] = MACRO_SYMBOLS[ticker]["category"]
                macro_signals.append(entry)
            if ticker in SECTOR_ETFS:
                entry["label"] = SECTOR_ETFS[ticker]["label"]
                entry["jp_sectors"] = SECTOR_ETFS[ticker]["jp_sectors"]
                sector_signals.append(entry)
        except Exception as e:
            print(f"  {ticker}: ERROR {e}", file=sys.stderr)
            continue

    # Sort sectors by absolute change (most newsworthy first)
    sector_signals.sort(key=lambda s: abs(s["change_pct"]), reverse=True)
    return macro_signals, sector_signals


# ---------------------------------------------------------------------------
# JP day-trade picks from yesterday's OHLCV
# ---------------------------------------------------------------------------

def compute_indicators(prices: pd.DataFrame) -> pd.DataFrame:
    """For each ticker, compute the metrics we need for day-trade scoring."""
    prices = prices.sort_values(["ticker", "date"])
    rows: list[dict] = []
    for ticker, group in prices.groupby("ticker", sort=False):
        if len(group) < 5:
            continue
        latest = group.iloc[-1]
        prev = group.iloc[-2]
        close = float(latest["close"])
        open_ = float(latest["open"])
        high = float(latest["high"])
        low = float(latest["low"])
        volume = float(latest["volume"])
        prev_close = float(prev["close"])

        if prev_close <= 0 or close <= 0:
            continue

        # 20-day average volume
        avg_vol_20 = float(group["volume"].tail(20).mean()) or 1.0
        volume_ratio = volume / avg_vol_20 if avg_vol_20 > 0 else 0.0

        # Daily return
        daily_return = (close - prev_close) / prev_close * 100

        # 5-day return (recent short-term momentum)
        ret_5d = None
        if len(group) >= 6:
            close_5d_ago = float(group["close"].iloc[-6])
            if close_5d_ago > 0:
                ret_5d = (close - close_5d_ago) / close_5d_ago * 100

        # Intraday range %
        range_pct = (high - low) / close * 100 if close > 0 else 0.0

        # Turnover (yen)
        vwap_approx = (open_ + high + low + close) / 4
        turnover = vwap_approx * volume

        # Position within 20-day range — how stretched vs how far from support
        tail_20 = group.tail(20)
        high_20 = float(tail_20["high"].max())
        low_20 = float(tail_20["low"].min())
        pct_from_high_20 = (close - high_20) / high_20 * 100 if high_20 > 0 else 0.0  # negative if below
        pct_from_low_20 = (close - low_20) / low_20 * 100 if low_20 > 0 else 0.0      # positive if above

        # RSI(14) on closes
        closes = group["close"].to_numpy()
        rsi = _rsi(closes, 14)

        rows.append({
            "ticker": ticker,
            "close": round(close, 2),
            "open": round(open_, 2),
            "high": round(high, 2),
            "low": round(low, 2),
            "volume": int(volume),
            "avg_vol_20": round(avg_vol_20, 0),
            "volume_ratio": round(volume_ratio, 2),
            "daily_return": round(daily_return, 2),
            "ret_5d": round(ret_5d, 2) if ret_5d is not None else None,
            "range_pct": round(range_pct, 2),
            "turnover": int(turnover),
            "pct_from_high_20": round(pct_from_high_20, 2),
            "pct_from_low_20": round(pct_from_low_20, 2),
            "high_20": round(high_20, 2),
            "low_20": round(low_20, 2),
            "rsi14": round(rsi, 1) if rsi is not None else None,
        })

    return pd.DataFrame(rows)


def _rsi(closes, period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]
    # Use Wilder's smoothing
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    return df[
        (df["avg_vol_20"] >= MIN_AVG_VOLUME)
        & (df["close"] >= MIN_CLOSE_YEN)
        & (df["close"] <= MAX_CLOSE_YEN)
    ].copy()


def _zscore(series: pd.Series) -> pd.Series:
    s = series.astype(float)
    sd = s.std() or 1.0
    return (s - s.mean()) / sd


def compute_day_trade_score(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["log_turnover"] = df["turnover"].apply(lambda v: math.log10(max(v, 1)))
    df["z_volume"] = _zscore(df["volume_ratio"])
    df["z_range"] = _zscore(df["range_pct"])
    df["z_turnover"] = _zscore(df["log_turnover"])
    df["z_move"] = _zscore(df["daily_return"].abs())
    df["score"] = (
        df["z_volume"] * 0.30
        + df["z_range"] * 0.25
        + df["z_turnover"] * 0.20
        + df["z_move"] * 0.25
    ).round(3)
    # Matsui-style "デイトレ適性" composite: range × turnover.
    # Source inspiration: https://finance.matsui.co.jp/ranking-day-trading-morning/
    # Intuitive interpretation: "how much price moves" × "how much money trades it"
    # = an estimate of the tradable opportunity per name.
    # Turnover is normalized to 億円 so numbers stay readable.
    df["matsui_score"] = (df["range_pct"] * (df["turnover"] / 1e8)).round(1)
    return df


def top_n(df: pd.DataFrame, sort_col: str, n: int = 10, asc: bool = False, extra_filter=None) -> list[dict]:
    sub = df
    if extra_filter is not None:
        sub = sub[extra_filter]
    sub = sub.sort_values(sort_col, ascending=asc).head(n)
    return [
        {
            "ticker": r["ticker"],
            "name": r.get("name", r["ticker"]),
            "sector17": r.get("sector17", ""),
            "close": r["close"],
            "daily_return": r["daily_return"],
            "volume_ratio": r["volume_ratio"],
            "range_pct": r["range_pct"],
            "turnover": int(r["turnover"]),
            "rsi14": r.get("rsi14"),
            "score": float(r.get("score", 0)),
            "matsui_score": float(r.get("matsui_score", 0)),
        }
        for _, r in sub.iterrows()
    ]


# ---------------------------------------------------------------------------
# Gap candidates: stocks in JP sectors corresponding to hot/cold US sector ETFs
# ---------------------------------------------------------------------------

def _build_ai_input_pool(scored: pd.DataFrame, picks: dict, gap_cands: dict, n: int = 30) -> list[dict]:
    """Return top N candidates by combined score, augmented with category flags
    and gap-candidate triggers so the LLM has rich context to pick its own.
    """
    # Build ticker -> set of category labels appearance map
    appears: dict[str, list[str]] = {}
    category_labels = {
        "total_score": "総合", "matsui_fitness": "デイトレ適性", "volume_surge": "出来高急増",
        "high_range": "値幅大", "high_turnover": "売買代金",
        "momentum_long": "続伸", "reversal_long": "反転",
    }
    for cat, label in category_labels.items():
        for p in picks.get(cat, []):
            appears.setdefault(p["ticker"], []).append(label)

    gap_trigger: dict[str, str] = {}
    for side, rows in (("up", gap_cands.get("up", [])), ("down", gap_cands.get("down", []))):
        for r in rows:
            gap_trigger.setdefault(r["ticker"], f"{('ギャップ上' if side=='up' else 'ギャップ下')}: {r['trigger_label']} {r['trigger_change']:+.2f}%")

    # Union: top by score + anyone appearing in any pick category + gap candidates
    eligible_tickers = set(scored.sort_values("score", ascending=False).head(n)["ticker"])
    eligible_tickers.update(appears.keys())
    eligible_tickers.update(gap_trigger.keys())

    sub = scored[scored["ticker"].isin(eligible_tickers)].sort_values("score", ascending=False).head(n + 10)
    rows = []
    for _, r in sub.iterrows():
        rows.append({
            "ticker": r["ticker"],
            "name": r["name"],
            "sector17": r["sector17"],
            "close": float(r["close"]),
            "daily_return": float(r["daily_return"]),
            "ret_5d": float(r["ret_5d"]) if pd.notna(r["ret_5d"]) else None,
            "volume_ratio": float(r["volume_ratio"]),
            "range_pct": float(r["range_pct"]),
            "turnover_oku": round(float(r["turnover"]) / 1e8, 1),  # 億円単位
            "rsi14": float(r["rsi14"]) if pd.notna(r["rsi14"]) else None,
            "pct_from_high_20": float(r["pct_from_high_20"]),
            "pct_from_low_20": float(r["pct_from_low_20"]),
            "high_20": float(r["high_20"]),
            "low_20": float(r["low_20"]),
            "score": float(r["score"]),
            "matsui_score": float(r["matsui_score"]),
            "appears_in": appears.get(r["ticker"], []),
            "gap_trigger": gap_trigger.get(r["ticker"]),
        })
    return rows


def build_gap_candidates(metrics: pd.DataFrame, sector_signals: list[dict]) -> dict:
    """Find JP stocks in JP sectors mapped from hot US sector ETFs.

    Includes any sector with |change| >= GAP_THRESHOLD, plus always the
    TOP_HOT_SECTORS biggest movers so we always surface something even on
    quiet days.
    """
    by_abs = sorted(sector_signals, key=lambda s: abs(s["change_pct"]), reverse=True)
    selected = []
    for i, sig in enumerate(by_abs):
        if i < TOP_HOT_SECTORS or abs(sig["change_pct"]) >= GAP_THRESHOLD:
            selected.append(sig)

    up_candidates: list[dict] = []
    down_candidates: list[dict] = []
    seen_up: set[str] = set()
    seen_down: set[str] = set()

    for sig in selected:
        target_side = "up" if sig["change_pct"] > 0 else "down"
        in_sectors = metrics[metrics["sector17"].isin(sig["jp_sectors"])]
        if in_sectors.empty:
            continue
        ranked = in_sectors.sort_values("turnover", ascending=False).head(GAP_CANDIDATES_PER_SECTOR)
        for _, r in ranked.iterrows():
            entry = {
                "ticker": r["ticker"],
                "name": r["name"],
                "sector17": r["sector17"],
                "close": r["close"],
                "daily_return": r["daily_return"],
                "turnover": int(r["turnover"]),
                "trigger_etf": sig["ticker"],
                "trigger_label": sig["label"],
                "trigger_change": sig["change_pct"],
            }
            if target_side == "up" and r["ticker"] not in seen_up:
                up_candidates.append(entry)
                seen_up.add(r["ticker"])
            elif target_side == "down" and r["ticker"] not in seen_down:
                down_candidates.append(entry)
                seen_down.add(r["ticker"])

    return {"up": up_candidates, "down": down_candidates}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not UNIVERSE_PATH.exists() or not PRICES_PATH.exists():
        print(f"ERROR: missing inputs ({UNIVERSE_PATH}, {PRICES_PATH})", file=sys.stderr)
        return 1

    universe = pd.DataFrame(json.loads(UNIVERSE_PATH.read_text(encoding="utf-8")))
    prices = pd.read_csv(PRICES_PATH)
    print(f"Loaded: universe={len(universe)}, prices_rows={len(prices):,}", file=sys.stderr)

    # 1. Compute per-ticker indicators
    metrics = compute_indicators(prices)
    metrics = metrics.merge(
        universe[["ticker", "name", "sector17"]],
        on="ticker",
        how="left",
    )
    metrics["name"] = metrics["name"].fillna(metrics["ticker"])
    metrics["sector17"] = metrics["sector17"].fillna("")

    # 2. Apply tradability filters
    filtered = apply_filters(metrics)
    print(f"After filters: {len(filtered)} / {len(metrics)} tickers", file=sys.stderr)

    # 3. Compute day-trade scores
    scored = compute_day_trade_score(filtered)

    # 4. Build pick lists
    picks = {
        "total_score":         top_n(scored, "score", 10),
        "matsui_fitness":      top_n(scored, "matsui_score", 10),
        "volume_surge":        top_n(scored, "volume_ratio", 10),
        "high_range":          top_n(scored, "range_pct", 10),
        "high_turnover":       top_n(scored, "turnover", 10),
        "momentum_long":       top_n(
            scored, "score", 10,
            extra_filter=(scored["daily_return"] >= 2.0) & (scored["volume_ratio"] >= 1.5),
        ),
        "reversal_long":       top_n(
            scored, "volume_ratio", 10,
            extra_filter=(scored["daily_return"] <= -3.0) & (scored["rsi14"].fillna(50) < 30),
        ),
    }

    # 5. Overnight macro + sector signals
    macro_signals, sector_signals = fetch_overnight()

    # 6. Gap candidates from sector overnight moves
    gap_candidates = build_gap_candidates(scored, sector_signals)

    # 7. AI input pool: top 30 by combined score, with category flags so the
    #    LLM can see which stocks appear in multiple ranking lists.
    ai_pool = _build_ai_input_pool(scored, picks, gap_candidates, n=30)

    asof_date = str(prices["date"].max())
    result = {
        "asof": asof_date,                              # data is from this trading day's close
        "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "universe_size": int(len(universe)),
        "tradable_count": int(len(filtered)),
        "macro_signals": macro_signals,
        "sector_signals": sector_signals,
        "gap_candidates": gap_candidates,
        "picks": picks,
        "ai_input_pool": ai_pool,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)", file=sys.stderr)
    print(f"  macro signals: {len(macro_signals)}", file=sys.stderr)
    print(f"  sector signals: {len(sector_signals)}", file=sys.stderr)
    print(f"  gap candidates: up={len(gap_candidates['up'])}, down={len(gap_candidates['down'])}", file=sys.stderr)
    print(f"  picks: {[(k, len(v)) for k, v in picks.items()]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
