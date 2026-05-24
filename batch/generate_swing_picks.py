"""AI-curated SWING picks (6-12 month holding, 順張りバイアス).

For each market (jp / us):
  1. Build a candidate pool from momentum_{market}.json + chart_data_{market}.json
     by computing swing-health metrics:
       - 200日線の上にいるか / 200日線の傾き(1ヶ月変化率)
       - 75日線・25日線とのパーフェクトオーダー
       - 52週高値からの距離(伸び代 or 過熱)
       - 25日線への直近押し目深さ(エントリ機会の有無)
       - 中期モメンタム(m3 / m1 リターン)
       - 流動性(時価総額・売買代金)
  2. Pre-rank by composite swing score → top 30
  3. Claude picks 5-7 best swing buys with structured rationale:
       ticker / name / appeal / entry_zone / mid_target / watch_signals

Outputs: web/data/swing_picks_{market}.json

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 3500
CANDIDATE_POOL_SIZE = 30
MIN_HISTORY_DAYS = 60


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def slope_pct(values: list[float], lookback: int = 21) -> float | None:
    if len(values) < lookback + 1:
        return None
    prev = values[-lookback - 1]
    if prev <= 0:
        return None
    return round((values[-1] / prev - 1) * 100, 2)


def build_snapshot(ticker: str, history: list, momentum_row: dict | None) -> dict | None:
    """history rows are [date, O, H, L, C, V] (JP) or [date, C] (US)."""
    if not history or len(history) < MIN_HISTORY_DAYS:
        return None
    has_ohlc = isinstance(history[0], list) and len(history[0]) >= 6
    closes = [r[4] if has_ohlc else r[1] for r in history]
    if any(c is None or c <= 0 for c in closes):
        return None

    cur = closes[-1]
    sma25 = sma(closes, 25)
    sma75 = sma(closes, 75)
    sma200 = sma(closes, 200)
    if sma200 is None:
        # try a shorter long-term proxy when 200 days isn't available
        sma200 = sma(closes, min(126, len(closes)))

    # SMA200 slope (1-month change)
    sma200_history = [sma(closes[: i + 1], 200) for i in range(max(0, len(closes) - 22), len(closes))]
    sma200_history = [v for v in sma200_history if v is not None]
    sma200_slope = slope_pct(sma200_history, lookback=min(21, len(sma200_history) - 1)) if len(sma200_history) >= 2 else None

    # 52-week high/low
    h52 = max(closes[-min(252, len(closes)):])
    l52 = min(closes[-min(252, len(closes)):])
    pct_from_52w_high = round((cur / h52 - 1) * 100, 2) if h52 else None

    # Pullback magnitude: how far below recent peak
    pct_below_20d_high = round((cur / max(closes[-20:]) - 1) * 100, 2)

    # Distance from MAs
    pct_from_sma25 = round((cur / sma25 - 1) * 100, 2) if sma25 else None
    pct_from_sma75 = round((cur / sma75 - 1) * 100, 2) if sma75 else None
    pct_from_sma200 = round((cur / sma200 - 1) * 100, 2) if sma200 else None

    # Trend booleans
    above_sma200 = bool(sma200 and cur > sma200)
    above_sma75 = bool(sma75 and cur > sma75)
    perfect_order = bool(sma25 and sma75 and sma200 and cur > sma25 > sma75 > sma200)

    # Composite swing score (pre-filter, before sending to LLM)
    score = 0
    if above_sma200: score += 2
    if above_sma75: score += 1.5
    if perfect_order: score += 1.5
    if sma200_slope is not None and sma200_slope > 0: score += min(sma200_slope, 5) / 2  # cap contribution
    if pct_from_52w_high is not None and -10 < pct_from_52w_high <= 0: score += 1.5  # at/near 52w high
    if pct_below_20d_high is not None and -7 <= pct_below_20d_high <= -2: score += 1.5  # healthy pullback
    if pct_from_sma200 is not None:
        # boost moderate distance from 200d (5-30%), penalize extreme overextension (>50%)
        if 5 <= pct_from_sma200 <= 30: score += 1
        elif pct_from_sma200 > 50: score -= 1.5

    # Add momentum boost from momentum_row (already-computed returns)
    m3 = momentum_row.get("m3") if momentum_row else None
    m1 = momentum_row.get("m1") if momentum_row else None
    if m3 is not None and m3 > 0: score += min(m3 / 30, 2)  # cap at +2 (60% gain)
    if m1 is not None and m1 < 0 and pct_below_20d_high and pct_below_20d_high > -10:
        score += 0.5  # short-term pullback within longer uptrend = entry opportunity

    # Average 20-day turnover (¥ or $) — computed from OHLCV when available;
    # JP momentum data doesn't carry market_cap, so this is our liquidity proxy.
    avg_turnover_20d = None
    if has_ohlc:
        try:
            recent = history[-20:]
            tvals = [r[5] * r[4] for r in recent if len(r) >= 6 and r[5] and r[4]]
            if tvals:
                avg_turnover_20d = sum(tvals) / len(tvals)
        except Exception:
            pass

    return {
        "ticker": ticker,
        "name": momentum_row.get("name") if momentum_row else ticker,
        "sector17": momentum_row.get("sector17") if momentum_row else "",
        "close": round(cur, 2),
        "sma25": round(sma25, 2) if sma25 else None,
        "sma75": round(sma75, 2) if sma75 else None,
        "sma200": round(sma200, 2) if sma200 else None,
        "sma200_slope_1m_pct": sma200_slope,
        "pct_from_sma25": pct_from_sma25,
        "pct_from_sma75": pct_from_sma75,
        "pct_from_sma200": pct_from_sma200,
        "pct_from_52w_high": pct_from_52w_high,
        "pct_below_20d_high": pct_below_20d_high,
        "high_52w": round(h52, 2),
        "low_52w": round(l52, 2),
        "above_sma200": above_sma200,
        "above_sma75": above_sma75,
        "perfect_order": perfect_order,
        "m1": m1,
        "m3": m3,
        "m12_1": momentum_row.get("m12_1") if momentum_row else None,
        "market_cap": momentum_row.get("market_cap") if momentum_row else None,
        "exchange": momentum_row.get("exchange") if momentum_row else None,
        "avg_turnover_20d": avg_turnover_20d,
        "score": round(score, 2),
    }


def build_prompt(market: str, pool: list[dict]) -> str:
    label = "東証プライム" if market == "jp" else "米国(NASDAQ/NYSE)"
    cur_symbol = "¥" if market == "jp" else "$"

    def fmt(p):
        po = "✓" if p["perfect_order"] else "-"
        slope_str = f"{p['sma200_slope_1m_pct']}%/月" if p['sma200_slope_1m_pct'] is not None else "-"
        m3 = f"{p['m3']:+.1f}%" if p["m3"] is not None else "-"
        mcap_str = ""
        if market == "us" and p.get("market_cap"):
            mcap_str = f"時価総額${p['market_cap']/1e9:.1f}B"
        elif market == "jp" and p.get("avg_turnover_20d"):
            mcap_str = f"売買代金{p['avg_turnover_20d']/1e8:.1f}億/日"
        return (
            f"- {p['ticker']} {p['name']} ({p['sector17']}): "
            f"終値{cur_symbol}{p['close']:,} "
            f"3M{m3} "
            f"200日線比{p['pct_from_sma200']}% (傾き{slope_str}) "
            f"52週高値比{p['pct_from_52w_high']}% "
            f"20日高値比{p['pct_below_20d_high']}%(押し目目安) "
            f"パーフェクトオーダー{po} "
            f"{mcap_str} swing_score={p['score']}"
        )

    return f"""あなたは長年の経験を持つ**スウィングトレード(6ヶ月〜1年保有・順張り)**専門の投資塾長です。

下記の{label}スウィング候補プール(中長期トレンド健全な銘柄を事前選定)から、**今エントリーに最適な銘柄を5〜7個**ピックしてください。

【選定の最優先基準】
- 200日線の上にいて、200日線が上向き(傾きプラス)
- 「パーフェクトオーダー」(価格 > 25日線 > 75日線 > 200日線)が成立している、もしくは崩れる兆候なし
- 52週高値からの距離が **−12%以内**(まだ上昇余地ある or 直近高値接近で順張り)
- 20日高値から **−2%〜−10%程度の健全な押し目**(極端な過熱・暴落でない)
- 3ヶ月モメンタムがプラス(順張りバイアス)
- 流動性十分(時価総額大きい銘柄優先)

【選定で避けること】
- パーフェクトオーダーが崩れている(下落トレンド入り)
- 200日線が下向き
- 52週高値から大きく下(−20%以下)= 中期下落トレンド
- 異常な過熱(20日高値から+5%以上の伸び切り)

【出力形式】 必ず ```json と ``` で囲んだ JSON 配列のみ返してください(narrative不要)。

各銘柄の構造:
- `ticker`: 文字列
- `name`: 銘柄名
- `appeal`: 80〜130字の選定理由(具体的な指標数値を引用しスウィング視点で)
- `entry_zone`: 押し目買いゾーンの提示(25日線・75日線等の水準で2〜3水準、{cur_symbol}単位)
- `mid_target`: 半年〜1年の上値ターゲット(52週高値ブレイク後の節目・過去レジスタンス等)
- `watch_signals`: 保有中の警戒シグナル(75日線割れ・週次出来高急減・パーフェクトオーダー崩壊等)

---スウィング候補プール (composite swing score 順)---
{chr(10).join(fmt(p) for p in pool)}
"""


JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```", re.IGNORECASE)


def parse_picks(text: str) -> list[dict]:
    m = JSON_BLOCK_RE.search(text)
    candidate = m.group(1).strip() if m else text.strip().strip("`").strip()
    if candidate.startswith("json"):
        candidate = candidate[4:].strip()
    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, list): return parsed
        if isinstance(parsed, dict) and "picks" in parsed: return parsed["picks"]
    except json.JSONDecodeError as e:
        print(f"WARN: swing picks JSON parse failed: {e}", file=sys.stderr)
    return []


def load_inputs(market: str):
    mom_path = REPO_ROOT / "web" / "data" / f"momentum_{market}.json"
    chart_path = REPO_ROOT / "web" / "data" / f"chart_data_{market}.json"
    if not mom_path.exists() or not chart_path.exists():
        return None, None
    mom = json.loads(mom_path.read_text(encoding="utf-8"))
    chart = json.loads(chart_path.read_text(encoding="utf-8"))
    return mom, chart


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "jp").lower()
    if market not in ("jp", "us"):
        print(f"ERROR: market must be 'jp' or 'us'", file=sys.stderr)
        return 1

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    momentum, chart = load_inputs(market)
    if not momentum or not chart:
        print(f"ERROR: missing inputs for {market}", file=sys.stderr)
        return 1

    # Build per-ticker map of momentum row for quick lookup
    mom_map = {r["ticker"]: r for r in momentum.get("all_tickers", [])}

    # Compute swing snapshots for all stocks with chart data
    snapshots = []
    for ticker, history in chart.items():
        snap = build_snapshot(ticker, history, mom_map.get(ticker))
        if snap is None:
            continue
        # Hard filters: need long-term trend healthy
        if not snap["above_sma200"]:
            continue
        if snap["sma200_slope_1m_pct"] is not None and snap["sma200_slope_1m_pct"] < -1:
            continue
        # Liquidity filter
        if market == "jp":
            # JPは momentum data に market_cap が無いので、売買代金 20日平均で判定
            turnover = snap.get("avg_turnover_20d") or 0
            if turnover < 1_000_000_000:  # 10億円/日未満は除外
                continue
        else:
            mc = snap.get("market_cap") or 0
            if mc < 1_000_000_000:  # $1B 未満は対象外
                continue
        snapshots.append(snap)

    snapshots.sort(key=lambda s: s["score"], reverse=True)
    pool = snapshots[:CANDIDATE_POOL_SIZE]
    print(f"[{market.upper()}] candidate pool: {len(pool)} (from {len(snapshots)} filtered)", file=sys.stderr)
    if not pool:
        print(f"[{market.upper()}] no valid swing candidates", file=sys.stderr)
        return 1

    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{market.upper()}] Calling Claude...", file=sys.stderr)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": build_prompt(market, pool)}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    picks = parse_picks(text)
    print(f"[{market.upper()}] {len(picks)} swing picks. Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}", file=sys.stderr)

    # Enrich picks with the pre-computed snapshot data so frontend can show
    # the indicator basis alongside Claude's narrative
    snap_by_ticker = {s["ticker"]: s for s in pool}
    enriched = []
    for p in picks:
        t = p.get("ticker")
        snap = snap_by_ticker.get(t, {})
        enriched.append({**p, "snapshot": snap})

    output = {
        "market": market,
        "asof": momentum.get("asof"),
        "generated_at_jst": __import__("datetime").datetime.now().astimezone().isoformat(),
        "candidate_pool_size": len(pool),
        "filtered_universe_size": len(snapshots),
        "model": MODEL,
        "picks": enriched,
    }
    out_path = REPO_ROOT / "web" / "data" / f"swing_picks_{market}.json"
    out_path.write_text(json.dumps(output, ensure_ascii=False), encoding="utf-8")
    print(f"[{market.upper()}] Wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
