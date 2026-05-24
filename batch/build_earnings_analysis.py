"""Weekly earnings analyzer — find stocks that reported in the last N days,
fetch quarterly data, and call Claude to produce a Fact/Guidance/Speculation
analysis per the earnings-analyzer skill convention.

CLI:
    python batch/build_earnings_analysis.py jp
    python batch/build_earnings_analysis.py us

Output: web/data/earnings_{market}.json

Requires env var: ANTHROPIC_API_KEY (for analysis step)
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import yfinance as yf

# Use curl_cffi browser-impersonated session to avoid Yahoo rate limits
# on GitHub Actions IP ranges.
try:
    from curl_cffi import requests as creq
    _YF_SESSION = creq.Session(impersonate="chrome")
except ImportError:
    _YF_SESSION = None

REPO_ROOT = Path(__file__).resolve().parent.parent
LOOKBACK_DAYS = 7
TOP_N_BY_MCAP = 300           # 上位 N 銘柄に限定(時価総額順)
MAX_ANALYZE = 40              # Claude 投入上限(コスト制御)
DISCOVERY_THREADS = 4         # 低めに(yfinance の rate limit 回避)
DATA_FETCH_THREADS = 4
DISCOVERY_RETRIES = 2         # 空応答時のリトライ回数(rate limit対策)
DISCOVERY_RETRY_DELAY = 4     # 秒
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1400


# ---------------------------------------------------------------------------
# Step 1: discover stocks with recent earnings
# ---------------------------------------------------------------------------

def load_universe(market: str) -> list[dict]:
    """Return list of {ticker, name, market_cap?} sorted by market cap desc, top N."""
    if market == "us":
        path = REPO_ROOT / "web" / "data" / "momentum_us.json"
        if not path.exists():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        ranked = sorted(
            (r for r in data.get("all_tickers", []) if r.get("market_cap")),
            key=lambda r: r.get("market_cap", 0),
            reverse=True,
        )[:TOP_N_BY_MCAP]
        return [{"ticker": r["ticker"], "name": r.get("name", r["ticker"]),
                 "market_cap": r.get("market_cap")} for r in ranked]
    else:  # jp
        # JP universe ranked by avg turnover (no market_cap field there)
        path = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            # Take pool from picks (high_turnover ranking is best proxy)
            ranked = data.get("picks", {}).get("high_turnover", [])
            # If too few, supplement from total_score
            other = data.get("picks", {}).get("total_score", [])
            seen = {r["ticker"] for r in ranked}
            for r in other:
                if r["ticker"] not in seen:
                    ranked.append(r)
            # Then fall back to universe_jp.json for top 500
        # Always supplement from universe_jp.json
        ujp_path = REPO_ROOT / "data" / "universe_jp.json"
        if ujp_path.exists():
            ujp = json.loads(ujp_path.read_text(encoding="utf-8"))
            existing = {r["ticker"] for r in ranked} if 'ranked' in dir() and ranked else set()
            for r in ujp:
                if r["ticker"] not in existing:
                    ranked.append({"ticker": r["ticker"], "name": r["name"]})
        # Limit to top N
        seen = set()
        out = []
        for r in ranked:
            if r["ticker"] not in seen:
                out.append({"ticker": r["ticker"], "name": r.get("name", r["ticker"])})
                seen.add(r["ticker"])
            if len(out) >= TOP_N_BY_MCAP:
                break
        return out


def check_recent_earnings(ticker: str) -> tuple[dict | None, str]:
    """Return (event, status) — status one of: 'hit' / 'no_data' / 'no_recent' / 'error'.

    Retries on empty/error response (Yahoo rate-limits GitHub IPs intermittently).
    """
    for attempt in range(1, DISCOVERY_RETRIES + 2):
        try:
            t = yf.Ticker(ticker, session=_YF_SESSION) if _YF_SESSION else yf.Ticker(ticker)
            ed = t.earnings_dates
            if ed is None or ed.empty:
                if attempt <= DISCOVERY_RETRIES:
                    time.sleep(DISCOVERY_RETRY_DELAY)
                    continue
                return None, "no_data"
            try:
                tz = ed.index.tz
                cutoff = pd.Timestamp.now(tz=tz) - pd.Timedelta(days=LOOKBACK_DAYS)
            except Exception:
                cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=LOOKBACK_DAYS)
            reported_col = None
            for c in ed.columns:
                if c in ("Reported EPS", "Earnings", "EPS Reported"):
                    reported_col = c
                    break
            if reported_col is None:
                return None, "no_data"
            recent = ed[ed[reported_col].notna() & (ed.index >= cutoff)]
            if recent.empty:
                return None, "no_recent"
            last = recent.iloc[0]
            return ({
                "ticker": ticker,
                "earnings_date": str(recent.index[0].date()),
                "eps_actual": float(last[reported_col]) if pd.notna(last[reported_col]) else None,
                "eps_estimate": float(last.get("EPS Estimate", float("nan"))) if pd.notna(last.get("EPS Estimate", float("nan"))) else None,
                "surprise_pct": float(last.get("Surprise(%)", float("nan"))) if pd.notna(last.get("Surprise(%)", float("nan"))) else None,
            }, "hit")
        except Exception as e:
            if attempt <= DISCOVERY_RETRIES:
                time.sleep(DISCOVERY_RETRY_DELAY)
                continue
            return None, "error"
    return None, "error"


def discover_recent_earnings(universe: list[dict]) -> list[dict]:
    """Scan universe in parallel; return stocks with recent earnings.

    Tracks per-status counters so rate-limit issues are visible in logs
    (silent 0-hits previously masked Yahoo throttling GitHub IPs).
    """
    print(f"  Scanning {len(universe)} tickers for recent earnings...", file=sys.stderr)
    results: list[dict] = []
    counts = {"hit": 0, "no_data": 0, "no_recent": 0, "error": 0}
    with ThreadPoolExecutor(max_workers=DISCOVERY_THREADS) as ex:
        futs = {ex.submit(check_recent_earnings, u["ticker"]): u for u in universe}
        for i, fut in enumerate(as_completed(futs), 1):
            u = futs[fut]
            try:
                r, status = fut.result()
                counts[status] = counts.get(status, 0) + 1
                if r:
                    r["name"] = u["name"]
                    results.append(r)
            except Exception:
                counts["error"] = counts.get("error", 0) + 1
            if i % 50 == 0:
                print(
                    f"    {i}/{len(universe)} scanned — "
                    f"hits:{counts['hit']} no_recent:{counts['no_recent']} "
                    f"no_data:{counts['no_data']} error:{counts['error']}",
                    file=sys.stderr,
                )
    print(
        f"  Discovery summary: hits={counts['hit']} no_recent={counts['no_recent']} "
        f"no_data={counts['no_data']} error={counts['error']}",
        file=sys.stderr,
    )
    if counts["no_data"] > len(universe) * 0.5:
        print(
            f"  WARN: {counts['no_data']} tickers returned no_data — likely Yahoo rate-limiting",
            file=sys.stderr,
        )
    # Most recent earnings first
    results.sort(key=lambda r: r["earnings_date"], reverse=True)
    return results[:MAX_ANALYZE]


# ---------------------------------------------------------------------------
# Step 2: fetch detailed earnings data per ticker
# ---------------------------------------------------------------------------

def fetch_earnings_detail(event: dict) -> dict:
    """Augment a discovery event with quarterly financials, YoY, and price reaction."""
    ticker = event["ticker"]
    try:
        t = yf.Ticker(ticker, session=_YF_SESSION) if _YF_SESSION else yf.Ticker(ticker)

        # Quarterly income statement (last 4 quarters typically)
        qfin = t.quarterly_income_stmt
        revenues = []
        net_incomes = []
        op_incomes = []
        gross_margins = []
        if qfin is not None and not qfin.empty:
            for col in qfin.columns[:5]:  # last 5 quarters
                rev = qfin.loc["Total Revenue", col] if "Total Revenue" in qfin.index else None
                ni = qfin.loc["Net Income", col] if "Net Income" in qfin.index else None
                opi = qfin.loc["Operating Income", col] if "Operating Income" in qfin.index else None
                gp = qfin.loc["Gross Profit", col] if "Gross Profit" in qfin.index else None
                revenues.append((str(col.date()), float(rev) if pd.notna(rev) else None))
                net_incomes.append((str(col.date()), float(ni) if pd.notna(ni) else None))
                op_incomes.append((str(col.date()), float(opi) if pd.notna(opi) else None))
                if gp is not None and pd.notna(gp) and rev and rev > 0:
                    gross_margins.append((str(col.date()), float(gp / rev * 100)))
                else:
                    gross_margins.append((str(col.date()), None))

        # YoY: latest quarter vs same quarter previous year
        rev_yoy = ni_yoy = opi_yoy = None
        if len(revenues) >= 5:
            lr, _ = revenues[0][1], revenues[4][1]
            if revenues[0][1] and revenues[4][1] and revenues[4][1] != 0:
                rev_yoy = (revenues[0][1] / revenues[4][1] - 1) * 100
            if net_incomes[0][1] is not None and net_incomes[4][1] is not None and net_incomes[4][1] != 0:
                ni_yoy = (net_incomes[0][1] / net_incomes[4][1] - 1) * 100
            if op_incomes[0][1] is not None and op_incomes[4][1] is not None and op_incomes[4][1] != 0:
                opi_yoy = (op_incomes[0][1] / op_incomes[4][1] - 1) * 100

        # Forward outlook from info
        info = {}
        try:
            info = t.info or {}
        except Exception:
            info = {}

        # Price reaction: 1-day and 5-day post-earnings return
        price_reaction = None
        try:
            ed_date = pd.Timestamp(event["earnings_date"])
            hist = t.history(start=str((ed_date - pd.Timedelta(days=2)).date()),
                             end=str((ed_date + pd.Timedelta(days=8)).date()),
                             interval="1d", auto_adjust=True)
            if not hist.empty:
                # Find the close on (or just after) the earnings date
                pre_idx = hist.index[hist.index.date <= ed_date.date()]
                post_idx = hist.index[hist.index.date > ed_date.date()]
                if len(pre_idx) > 0 and len(post_idx) > 0:
                    pre_close = float(hist.loc[pre_idx[-1], "Close"])
                    next_close = float(hist.loc[post_idx[0], "Close"])
                    react_1d = (next_close / pre_close - 1) * 100 if pre_close > 0 else None
                    react_5d = None
                    if len(post_idx) >= 5:
                        c5 = float(hist.loc[post_idx[4], "Close"])
                        react_5d = (c5 / pre_close - 1) * 100 if pre_close > 0 else None
                    price_reaction = {
                        "1d_pct": round(react_1d, 2) if react_1d is not None else None,
                        "5d_pct": round(react_5d, 2) if react_5d is not None else None,
                    }
        except Exception:
            pass

        return {
            **event,
            "quarterly_revenue":    revenues,
            "quarterly_net_income": net_incomes,
            "quarterly_op_income":  op_incomes,
            "quarterly_gross_margin_pct": gross_margins,
            "yoy_revenue_pct":      round(rev_yoy, 2) if rev_yoy is not None else None,
            "yoy_net_income_pct":   round(ni_yoy, 2) if ni_yoy is not None else None,
            "yoy_op_income_pct":    round(opi_yoy, 2) if opi_yoy is not None else None,
            "forward_eps":          info.get("forwardEps"),
            "forward_pe":           info.get("forwardPE"),
            "trailing_pe":          info.get("trailingPE"),
            "dividend_yield_pct":   info.get("dividendYield"),
            "sector":               info.get("sector"),
            "industry":             info.get("industry"),
            "earnings_growth_pct":  (info.get("earningsGrowth") or 0) * 100 if info.get("earningsGrowth") is not None else None,
            "revenue_growth_pct":   (info.get("revenueGrowth") or 0) * 100 if info.get("revenueGrowth") is not None else None,
            "long_business_summary": (info.get("longBusinessSummary") or "")[:500],
            "price_reaction":       price_reaction,
            "current_price":        info.get("currentPrice"),
        }
    except Exception as e:
        print(f"  {ticker}: detail fetch error: {e}", file=sys.stderr)
        return event


# ---------------------------------------------------------------------------
# Step 3: Claude analysis (earnings-analyzer skill structure)
# ---------------------------------------------------------------------------

EARNINGS_PROMPT_TEMPLATE = """あなたは経験豊富な決算アナリストです。下記の決算データを分析し、**earnings-analyzer スキルの規約に厳密に従って** 日本語で出力してください。

【スキル規約 — 厳守】

出力は3セクション構造:
  ### 事実 (Fact)
    - 検証可能な数値のみ
    - 必ず出典(yfinance)を明記
    - 主観的解釈は一切含めない

  ### ガイダンス (Guidance)
    - 経営陣が発表した会社見通し
    - 前回ガイダンスからの変化(変化があれば)
    - データに無い場合は「明示的ガイダンスなし(取得元: yfinance.info)」と書く

  ### 推測 (Speculation)
    - 分析者(あなた)の解釈
    - **必ず冒頭に「以下は推測です」と明記**
    - 「〜と考えられる」「〜の可能性がある」などの推測表現を用いる

【禁止事項】
- 未確認の数値を事実として記載しない
- 「買い」「売り」の投資判断を断定しない
- データ源を曖昧にしない

【出力ボリューム】
- 全体で300〜500字程度
- 各セクション短く要点のみ

---銘柄---
{name} ({ticker})
セクター: {sector} / 業種: {industry}
決算発表日: {earnings_date}

【EPS データ(yfinance.earnings_dates より)】
EPS 実績: {eps_actual}
EPS コンセンサス予想: {eps_estimate}
サプライズ: {surprise_pct}%

【四半期業績推移(yfinance.quarterly_income_stmt より)】
売上高(直近5四半期): {revenues_str}
営業利益(直近5四半期): {op_incomes_str}
純利益(直近5四半期): {net_incomes_str}
売上総利益率 % : {gross_margins_str}

【YoY 比較】
売上 YoY: {yoy_revenue}%
営業利益 YoY: {yoy_op_income}%
純利益 YoY: {yoy_net_income}%

【ガイダンス関連(yfinance.info より)】
予想EPS(forward): {forward_eps}
予想PER(forward): {forward_pe}
実績PER(trailing): {trailing_pe}
売上成長率(会社予想/直近): {revenue_growth}%
利益成長率(会社予想/直近): {earnings_growth}%
配当利回り: {dividend_yield}%

【株価反応】
決算翌日リターン: {react_1d}%
決算5営業日後リターン: {react_5d}%

【事業概要(参考、最大500字)】
{business_summary}
"""


def fmt_quarters(rows, scale=1e8, unit="億"):
    """Format quarterly data as '日付 値(億)'."""
    parts = []
    for date, val in rows:
        if val is None:
            parts.append(f"{date}: -")
        else:
            parts.append(f"{date}: {val/scale:,.1f}{unit}")
    return " / ".join(parts)


def fmt_pct_list(rows):
    parts = []
    for date, val in rows:
        if val is None:
            parts.append(f"{date}: -")
        else:
            parts.append(f"{date}: {val:.1f}%")
    return " / ".join(parts)


def build_prompt(detail: dict) -> str:
    return EARNINGS_PROMPT_TEMPLATE.format(
        name=detail.get("name", detail["ticker"]),
        ticker=detail["ticker"],
        sector=detail.get("sector") or "-",
        industry=detail.get("industry") or "-",
        earnings_date=detail.get("earnings_date"),
        eps_actual=detail.get("eps_actual"),
        eps_estimate=detail.get("eps_estimate"),
        surprise_pct=detail.get("surprise_pct"),
        revenues_str=fmt_quarters(detail.get("quarterly_revenue", [])),
        op_incomes_str=fmt_quarters(detail.get("quarterly_op_income", [])),
        net_incomes_str=fmt_quarters(detail.get("quarterly_net_income", [])),
        gross_margins_str=fmt_pct_list(detail.get("quarterly_gross_margin_pct", [])),
        yoy_revenue=detail.get("yoy_revenue_pct"),
        yoy_op_income=detail.get("yoy_op_income_pct"),
        yoy_net_income=detail.get("yoy_net_income_pct"),
        forward_eps=detail.get("forward_eps"),
        forward_pe=detail.get("forward_pe"),
        trailing_pe=detail.get("trailing_pe"),
        revenue_growth=detail.get("revenue_growth_pct"),
        earnings_growth=detail.get("earnings_growth_pct"),
        dividend_yield=detail.get("dividend_yield_pct"),
        react_1d=(detail.get("price_reaction") or {}).get("1d_pct"),
        react_5d=(detail.get("price_reaction") or {}).get("5d_pct"),
        business_summary=detail.get("long_business_summary") or "(なし)",
    )


def analyze_with_claude(detail: dict, client) -> str | None:
    import anthropic  # local import
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": build_prompt(detail)}],
        )
        return "".join(b.text for b in resp.content if b.type == "text").strip(), resp.usage.input_tokens, resp.usage.output_tokens
    except Exception as e:
        print(f"  {detail['ticker']}: Claude error: {e}", file=sys.stderr)
        return None, 0, 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "us").lower()
    if market not in ("jp", "us"):
        print(f"ERROR: market must be 'jp' or 'us'", file=sys.stderr)
        return 1

    universe = load_universe(market)
    if not universe:
        print(f"ERROR: no universe loaded for {market}", file=sys.stderr)
        return 1
    print(f"[{market.upper()}] Universe: {len(universe)} stocks (top by mcap/turnover)", file=sys.stderr)

    # Step 1: discover
    events = discover_recent_earnings(universe)
    print(f"[{market.upper()}] {len(events)} stocks with earnings in last {LOOKBACK_DAYS} days", file=sys.stderr)
    if not events:
        # Write empty result so frontend shows 'no earnings this week'
        out_path = REPO_ROOT / "web" / "data" / f"earnings_{market}.json"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({
            "market": market,
            "lookback_days": LOOKBACK_DAYS,
            "asof": pd.Timestamp.now(tz="Asia/Tokyo").strftime("%Y-%m-%d"),
            "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
            "analyses": [],
        }, ensure_ascii=False), encoding="utf-8")
        print(f"[{market.upper()}] Wrote empty {out_path}", file=sys.stderr)
        return 0

    # Step 2: detail fetch
    print(f"[{market.upper()}] Fetching detailed financials for {len(events)} stocks...", file=sys.stderr)
    detailed: list[dict] = []
    with ThreadPoolExecutor(max_workers=DATA_FETCH_THREADS) as ex:
        futs = {ex.submit(fetch_earnings_detail, e): e for e in events}
        for fut in as_completed(futs):
            try:
                detailed.append(fut.result())
            except Exception as e:
                print(f"    detail error: {e}", file=sys.stderr)

    # Step 3: Claude analysis
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    analyses: list[dict] = []
    total_in = total_out = 0
    if api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        print(f"[{market.upper()}] Calling Claude for {len(detailed)} analyses...", file=sys.stderr)
        for i, d in enumerate(detailed, 1):
            result = analyze_with_claude(d, client)
            if isinstance(result, tuple) and result[0]:
                text, ti, to = result
                total_in += ti
                total_out += to
                analyses.append({**d, "analysis": text, "analysis_model": MODEL})
                print(f"  [{i}/{len(detailed)}] {d['ticker']} {d['name']}: {len(text)} chars", file=sys.stderr)
            else:
                analyses.append({**d, "analysis": None})
    else:
        print(f"[{market.upper()}] ANTHROPIC_API_KEY not set — skipping AI analysis", file=sys.stderr)
        analyses = [{**d, "analysis": None} for d in detailed]

    # Sort: largest surprise % first (most newsworthy)
    analyses.sort(
        key=lambda a: abs(a.get("surprise_pct") or 0),
        reverse=True,
    )

    out_path = REPO_ROOT / "web" / "data" / f"earnings_{market}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "market": market,
        "lookback_days": LOOKBACK_DAYS,
        "asof": pd.Timestamp.now(tz="Asia/Tokyo").strftime("%Y-%m-%d"),
        "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "analyses": analyses,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[{market.upper()}] Wrote {out_path} ({len(analyses)} analyses). "
          f"Tokens: in={total_in} out={total_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
