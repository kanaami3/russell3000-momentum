"""Compute value-investing rankings from raw fundamentals.

Reads data/value_data_jp.csv, applies sanity filters, computes ranking
percentiles and a composite value score, and emits the 6 ranking lists
plus a full-coverage table to web/data/value_jp.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = REPO_ROOT / "data" / "value_data_jp.csv"
OUTPUT_PATH = REPO_ROOT / "web" / "data" / "value_jp.json"

# Common filters across all rankings (avoid micro caps, illiquid, distressed)
MIN_MARKET_CAP = 50_000_000_000      # ¥500億 以上(中小型避け)
MAX_PAYOUT_RATIO = 100               # 配当性向 100% 超は無理な配当
MAX_DIV_YIELD = 10                   # 配当 10%超は減配リスクで除外


def _has(row: pd.Series, *cols: str) -> bool:
    return all(pd.notna(row.get(c)) for c in cols)


def base_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Common quality filters: liquid mid/large cap with reasonable financials."""
    return df[
        df["market_cap"].fillna(0) >= MIN_MARKET_CAP
    ].copy()


def _row_dict(r: pd.Series) -> dict:
    return {
        "ticker": r["ticker"],
        "name": r.get("name", r["ticker"]),
        "sector17": r.get("sector17", ""),
        "current_price": _safe(r.get("current_price")),
        "market_cap_oku": int(r["market_cap"] / 1e8) if pd.notna(r["market_cap"]) else None,
        "dividend_yield": _safe(r.get("dividend_yield")),
        "payout_ratio": _safe(r.get("payout_ratio")),
        "trailing_pe": _safe(r.get("trailing_pe")),
        "price_to_book": _safe(r.get("price_to_book")),
        "return_on_equity": _safe(r.get("return_on_equity")),
        "return_on_assets": _safe(r.get("return_on_assets")),
        "revenue_growth": _safe(r.get("revenue_growth")),
        "earnings_growth": _safe(r.get("earnings_growth")),
        "operating_margins": _safe(r.get("operating_margins")),
        "profit_margins": _safe(r.get("profit_margins")),
        "debt_to_equity": _safe(r.get("debt_to_equity")),
        "value_score": _safe(r.get("value_score")),
    }


def _safe(v):
    if v is None or pd.isna(v):
        return None
    return round(float(v), 2)


# ---------------------------------------------------------------------------
# Rankings
# ---------------------------------------------------------------------------

def rank_high_dividend(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["dividend_yield"]).copy()
    sub = sub[
        (sub["dividend_yield"] >= 3)
        & (sub["dividend_yield"] <= MAX_DIV_YIELD)
        & ((sub["payout_ratio"].isna()) | (sub["payout_ratio"] <= MAX_PAYOUT_RATIO))
    ]
    sub = sub.sort_values("dividend_yield", ascending=False).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


def rank_low_pe(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["trailing_pe"]).copy()
    # PE < 3 はデータ異常 or 特殊要因(売却益等)で実態を反映しないため除外
    sub = sub[(sub["trailing_pe"] >= 3) & (sub["trailing_pe"] <= 15)]
    # Prefer those also with positive revenue growth (avoid declining biz)
    sub = sub[sub["revenue_growth"].fillna(0) >= 0]
    sub = sub.sort_values("trailing_pe", ascending=True).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


def rank_low_pbr(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["price_to_book"]).copy()
    sub = sub[(sub["price_to_book"] > 0) & (sub["price_to_book"] <= 1.0)]
    # Prefer with healthy ROE (avoid value traps with poor returns on equity)
    sub = sub[sub["return_on_equity"].fillna(0) >= 5]
    sub = sub.sort_values("price_to_book", ascending=True).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


def rank_high_roe(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["return_on_equity"]).copy()
    sub = sub[sub["return_on_equity"] >= 10]
    sub = sub.sort_values("return_on_equity", ascending=False).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


def rank_growth(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["revenue_growth"]).copy()
    # 200%超は M&A/会計変更 起因の異常値が多いので除外
    sub = sub[(sub["revenue_growth"] >= 5) & (sub["revenue_growth"] <= 200)]
    # Prefer with positive earnings growth too
    sub = sub[sub["earnings_growth"].fillna(0) >= 0]
    # Filter out crazy PE (avoid speculative growth)
    sub = sub[sub["trailing_pe"].fillna(999) <= 40]
    sub = sub.sort_values("revenue_growth", ascending=False).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


# ---------------------------------------------------------------------------
# Composite value score
# ---------------------------------------------------------------------------

def compute_value_score(df: pd.DataFrame) -> pd.DataFrame:
    """Percentile-rank each metric (winsorize first), then weighted blend.

    Higher is better. Score range roughly 0..1.
    """
    df = df.copy()
    df["value_score"] = None

    # Build sub-frame of stocks with ALL the score components present
    needed = ["dividend_yield", "trailing_pe", "price_to_book",
              "return_on_equity", "revenue_growth"]
    mask = df[needed].notna().all(axis=1)
    sub = df[mask].copy()
    if sub.empty:
        return df

    # Sanity filters before scoring (avoid distressed names + data errors polluting percentiles)
    sub = sub[
        (sub["trailing_pe"] >= 3) & (sub["trailing_pe"] <= 40)       # PE<3 はデータ異常
        & (sub["price_to_book"] > 0) & (sub["price_to_book"] <= 5)   # PBR>5 は割安とは言えない
        & (sub["dividend_yield"] <= MAX_DIV_YIELD)
        & (sub["revenue_growth"] <= 200)                              # 200%超は M&A
        & (sub["return_on_equity"] >= 3)                              # 最低限の収益性
    ]
    if sub.empty:
        return df

    # Percentile rank: high-good metrics get raw rank; low-good get inverted rank
    sub["p_div"]  = sub["dividend_yield"].rank(pct=True)        # high better
    sub["p_pe"]   = 1 - sub["trailing_pe"].rank(pct=True)       # low better
    sub["p_pbr"]  = 1 - sub["price_to_book"].rank(pct=True)     # low better
    sub["p_roe"]  = sub["return_on_equity"].rank(pct=True)      # high better
    sub["p_grow"] = sub["revenue_growth"].rank(pct=True)        # high better

    sub["score"] = (
        sub["p_div"]  * 0.25
        + sub["p_pe"]   * 0.20
        + sub["p_pbr"]  * 0.15
        + sub["p_roe"]  * 0.25
        + sub["p_grow"] * 0.15
    )
    df.loc[sub.index, "value_score"] = sub["score"].round(3)
    return df


def rank_composite(df: pd.DataFrame, n: int = 30) -> list[dict]:
    sub = df.dropna(subset=["value_score"]).sort_values("value_score", ascending=False).head(n)
    return [_row_dict(r) for _, r in sub.iterrows()]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not INPUT_PATH.exists():
        print(f"ERROR: {INPUT_PATH} not found — run fetch_value_data.py first", file=sys.stderr)
        return 1

    df = pd.read_csv(INPUT_PATH)
    print(f"Loaded {len(df)} rows", file=sys.stderr)

    filtered = base_filter(df)
    print(f"After base filter (mcap >= ¥500億): {len(filtered)} stocks", file=sys.stderr)

    filtered = compute_value_score(filtered)
    n_scored = filtered["value_score"].notna().sum()
    print(f"Composite value_score computed for: {n_scored} stocks", file=sys.stderr)

    rankings = {
        "high_dividend": rank_high_dividend(filtered),
        "low_pe":        rank_low_pe(filtered),
        "low_pbr":       rank_low_pbr(filtered),
        "high_roe":      rank_high_roe(filtered),
        "growth":        rank_growth(filtered),
        "composite":     rank_composite(filtered),
    }

    result = {
        "asof": pd.Timestamp.now(tz="Asia/Tokyo").strftime("%Y-%m-%d"),
        "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "universe_size": int(len(df)),
        "filtered_count": int(len(filtered)),
        "scored_count": int(n_scored),
        "rankings": rankings,
    }

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    size_kb = OUTPUT_PATH.stat().st_size / 1024
    print(f"Wrote {OUTPUT_PATH} ({size_kb:.1f} KB)", file=sys.stderr)
    for k, v in rankings.items():
        print(f"  {k:15} = {len(v)} 銘柄", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
