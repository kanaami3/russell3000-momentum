"""Per-stock AI technical commentary in '塾長スタイル'.

For each selected ticker, computes a rich indicator snapshot (RSI,
MAs, ATR, divergence detection) and asks Claude to write a 300-500 char
Japanese technical analysis in mentor-tone. Output is a lookup table
{ticker: commentary_obj} read by the frontend chart modal.

CLI:
    python batch/generate_stock_commentary.py jp
    python batch/generate_stock_commentary.py us

Output: web/data/stock_commentary_{market}.json

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1200
MAX_CONCURRENT = 4  # Claude API: be polite


# ---------------------------------------------------------------------------
# Indicator helpers
# ---------------------------------------------------------------------------

def sma(values: list[float], period: int) -> list[float | None]:
    out: list[float | None] = []
    for i in range(len(values)):
        if i < period - 1:
            out.append(None)
        else:
            out.append(sum(values[i - period + 1: i + 1]) / period)
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) <= period:
        return [None] * len(closes)
    out: list[float | None] = [None] * period
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
    out.append(100 - 100 / (1 + rs) if rs != float("inf") else 100.0)
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        rs = avg_gain / avg_loss if avg_loss > 0 else float("inf")
        out.append(100 - 100 / (1 + rs) if rs != float("inf") else 100.0)
    return out


def detect_divergence(closes: list[float], rsi_vals: list[float | None],
                      window: int = 5, lookback: int = 40) -> dict | None:
    """Find RSI divergence on the last `lookback` bars.

    Bearish: last 2 price peaks → higher high; RSI peaks → lower high.
    Bullish: last 2 price troughs → lower low; RSI troughs → higher low.

    Returns None if no divergence detected.
    """
    if len(closes) < lookback or all(r is None for r in rsi_vals[-lookback:]):
        return None
    cs = closes[-lookback:]
    rs = rsi_vals[-lookback:]

    def is_peak(i):
        if i < window or i > len(cs) - 1 - window:
            return False
        return cs[i] == max(cs[i - window: i + window + 1])

    def is_trough(i):
        if i < window or i > len(cs) - 1 - window:
            return False
        return cs[i] == min(cs[i - window: i + window + 1])

    peaks = [i for i in range(len(cs)) if is_peak(i)]
    troughs = [i for i in range(len(cs)) if is_trough(i)]

    result: dict = {}

    if len(peaks) >= 2:
        p1, p2 = peaks[-2], peaks[-1]
        rsi1, rsi2 = rs[p1], rs[p2]
        if rsi1 is not None and rsi2 is not None:
            if cs[p2] > cs[p1] * 1.005 and rsi2 < rsi1 - 3:
                result["bearish"] = {
                    "earlier_price": round(cs[p1], 2),
                    "later_price": round(cs[p2], 2),
                    "earlier_rsi": round(rsi1, 1),
                    "later_rsi": round(rsi2, 1),
                    "days_ago_earlier": lookback - p1,
                    "days_ago_later": lookback - p2,
                }

    if len(troughs) >= 2 and "bearish" not in result:
        t1, t2 = troughs[-2], troughs[-1]
        rsi1, rsi2 = rs[t1], rs[t2]
        if rsi1 is not None and rsi2 is not None:
            if cs[t2] < cs[t1] * 0.995 and rsi2 > rsi1 + 3:
                result["bullish"] = {
                    "earlier_price": round(cs[t1], 2),
                    "later_price": round(cs[t2], 2),
                    "earlier_rsi": round(rsi1, 1),
                    "later_rsi": round(rsi2, 1),
                    "days_ago_earlier": lookback - t1,
                    "days_ago_later": lookback - t2,
                }

    return result or None


def build_snapshot(ticker: str, history: list[list]) -> dict | None:
    """Build a structured technical snapshot from OHLCV (or close-only) history.

    history rows are [date, open, high, low, close, volume] for JP or
    [date, close] for US.
    """
    if not history or len(history) < 30:
        return None
    has_ohlc = isinstance(history[0], list) and len(history[0]) >= 6
    closes = [r[4] if has_ohlc else r[1] for r in history]
    dates = [r[0] for r in history]

    sma5 = sma(closes, 5)
    sma25 = sma(closes, 25)
    sma75 = sma(closes, 75)
    sma200 = sma(closes, 200)
    rsi14 = rsi(closes, 14)

    cur = closes[-1]
    prev = closes[-2] if len(closes) > 1 else cur
    div = detect_divergence(closes, rsi14, window=5, lookback=40)

    # Volume pattern (last day vs avg of last 20) — only meaningful with OHLCV
    vol_ratio = None
    if has_ohlc:
        vols = [r[5] for r in history if len(r) >= 6]
        if len(vols) >= 20 and sum(vols[-20:]) > 0:
            avg20 = sum(vols[-20:]) / 20
            vol_ratio = round(vols[-1] / avg20, 2) if avg20 > 0 else None

    # 20日高値・安値 + 60日高値・安値
    high20 = max(closes[-20:])
    low20 = min(closes[-20:])
    high60 = max(closes[-60:]) if len(closes) >= 60 else high20
    low60 = min(closes[-60:]) if len(closes) >= 60 else low20

    snap = {
        "ticker": ticker,
        "date": dates[-1],
        "close": round(cur, 2),
        "change_pct": round((cur / prev - 1) * 100, 2) if prev else 0.0,
        "sma5": round(sma5[-1], 2) if sma5[-1] is not None else None,
        "sma25": round(sma25[-1], 2) if sma25[-1] is not None else None,
        "sma75": round(sma75[-1], 2) if sma75[-1] is not None else None,
        "sma200": round(sma200[-1], 2) if sma200[-1] is not None else None,
        "rsi14": round(rsi14[-1], 1) if rsi14[-1] is not None else None,
        "rsi14_prev5": [round(v, 1) if v is not None else None for v in rsi14[-6:-1]],
        "vol_ratio_vs_20d": vol_ratio,
        "high_20d": round(high20, 2),
        "low_20d": round(low20, 2),
        "high_60d": round(high60, 2),
        "low_60d": round(low60, 2),
        "divergence": div,
    }
    return snap


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

def build_prompt(name: str, snap: dict) -> str:
    div = snap.get("divergence") or {}
    div_section = ""
    if "bearish" in div:
        d = div["bearish"]
        div_section = (
            f"\n【⚠️ 弱気ダイバージェンス検出】\n"
            f"  価格: {d['days_ago_earlier']}日前 高値 ¥{d['earlier_price']:,} → "
            f"{d['days_ago_later']}日前 高値 ¥{d['later_price']:,}(高値更新)\n"
            f"  RSI: {d['earlier_rsi']} → {d['later_rsi']}(高値下落)\n"
            f"  → 上昇局面ながらモメンタムが減衰、**売りサイン候補**\n"
        )
    elif "bullish" in div:
        d = div["bullish"]
        div_section = (
            f"\n【💡 強気ダイバージェンス検出】\n"
            f"  価格: {d['days_ago_earlier']}日前 安値 ¥{d['earlier_price']:,} → "
            f"{d['days_ago_later']}日前 安値 ¥{d['later_price']:,}(安値更新)\n"
            f"  RSI: {d['earlier_rsi']} → {d['later_rsi']}(安値上昇)\n"
            f"  → 下落局面ながら売り圧力が減衰、**反転買いサイン候補**\n"
        )

    vol = f"出来高 平均比×{snap['vol_ratio_vs_20d']}" if snap.get("vol_ratio_vs_20d") else "出来高情報なし"

    return f"""あなたは長年の経験を持つ株式投資の塾長です。塾生に向けて、以下の銘柄を**300〜500字で技術分析解説**してください。

【口調】親しみやすく丁寧、要所要所で「〜だ」「〜だね」「〜は要注意」のような塾長らしい口調を入れてOK。

【必ず含めること】
1. 移動平均線(5日・25日・75日・200日)の位置関係と意味
2. RSI(14)の水準と過熱・売られすぎ判定
3. **ダイバージェンスが検出されている場合、その重要性を強調**(売りサイン or 反転買いサイン)
4. 直近20日・60日高値からの距離(ブレイクアウト余地 or 過熱警戒)
5. 出来高/売買代金の状況(信頼度の根拠)
6. 売買シナリオ(エントリ目安・利食い・損切ライン)

【避けること】
- 見出し記号(##や**)は使わず、自然な段落で
- 投資助言にならないよう「観察」「ヒント」レベル
- 抽象論ではなく具体的な数値を引用

---銘柄---
{name} ({snap['ticker']})

---テクニカル指標---
本日終値: ¥{snap['close']:,} (前日比 {snap['change_pct']:+.2f}%)
5日線: {snap['sma5']} / 25日線: {snap['sma25']} / 75日線: {snap['sma75']} / 200日線: {snap['sma200']}
RSI(14): {snap['rsi14']}  直近5日推移: {snap['rsi14_prev5']}
20日高値 ¥{snap['high_20d']:,} / 20日安値 ¥{snap['low_20d']:,}
60日高値 ¥{snap['high_60d']:,} / 60日安値 ¥{snap['low_60d']:,}
{vol}
{div_section}
"""


# ---------------------------------------------------------------------------
# Stock selection per market
# ---------------------------------------------------------------------------

def collect_jp_targets() -> list[tuple[str, str]]:
    """JP: AI picks + top10 of momentum panels + gap candidates (deduped)."""
    brief_path = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"
    if not brief_path.exists():
        return []
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}  # ticker → name
    for p in brief.get("ai_picks", []):
        out[p["ticker"]] = p.get("name", p["ticker"])
    for key in ("yesterday_top10", "mom_1w_top10", "mom_1m_top10", "mom_3m_top10",
                "total_score", "matsui_fitness", "momentum_long"):
        for r in (brief.get("picks", {}).get(key, []) if key in brief.get("picks", {}) else brief.get(key, []))[:10]:
            out.setdefault(r["ticker"], r.get("name", r["ticker"]))
    for r in brief.get("gap_candidates", {}).get("up", [])[:10]:
        out.setdefault(r["ticker"], r.get("name", r["ticker"]))
    return list(out.items())


def collect_us_targets() -> list[tuple[str, str]]:
    """US: top10 of momentum panels (deduped)."""
    mom_path = REPO_ROOT / "web" / "data" / "momentum_us.json"
    if not mom_path.exists():
        return []
    mom = json.loads(mom_path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for key in ("yesterday_top10", "mom_1w_top10", "mom_1m_top10", "mom_3m_top10", "mom_12_1_top10"):
        for r in mom.get(key, [])[:10]:
            out.setdefault(r["ticker"], r.get("name", r["ticker"]))
    return list(out.items())


def load_chart_data(market: str) -> dict[str, list]:
    path = REPO_ROOT / "web" / "data" / f"chart_data_{market}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "jp").lower()
    if market not in ("jp", "us"):
        print(f"ERROR: market must be 'jp' or 'us'", file=sys.stderr)
        return 1

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    targets = collect_jp_targets() if market == "jp" else collect_us_targets()
    if not targets:
        print(f"ERROR: no targets found for market {market}", file=sys.stderr)
        return 1
    print(f"[{market.upper()}] {len(targets)} stocks to analyze", file=sys.stderr)

    chart = load_chart_data(market)
    client = anthropic.Anthropic(api_key=api_key)

    output: dict[str, dict] = {}
    total_in = 0
    total_out = 0
    div_count = 0

    for i, (ticker, name) in enumerate(targets, 1):
        history = chart.get(ticker)
        if not history:
            print(f"  [{i}/{len(targets)}] {ticker} {name}: skip (no chart data)", file=sys.stderr)
            continue
        snap = build_snapshot(ticker, history)
        if not snap:
            print(f"  [{i}/{len(targets)}] {ticker} {name}: skip (insufficient history)", file=sys.stderr)
            continue
        if snap.get("divergence"):
            div_count += 1
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                messages=[{"role": "user", "content": build_prompt(name, snap)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            output[ticker] = {
                "name": name,
                "asof": snap["date"],
                "commentary": text,
                "snapshot": snap,
            }
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens
            div_label = ""
            if snap.get("divergence", {}).get("bearish"):  div_label = " 🚨弱気DV"
            elif snap.get("divergence", {}).get("bullish"): div_label = " 💡強気DV"
            print(f"  [{i}/{len(targets)}] {ticker} {name}: {len(text)} chars{div_label}", file=sys.stderr)
        except Exception as e:
            print(f"  [{i}/{len(targets)}] {ticker} {name}: ERROR {e}", file=sys.stderr)

    out_path = REPO_ROOT / "web" / "data" / f"stock_commentary_{market}.json"
    out_path.write_text(json.dumps({
        "market": market,
        "asof": next(iter(output.values()))["asof"] if output else None,
        "generated_at_jst": __import__("datetime").datetime.now().astimezone().isoformat(),
        "commentary": output,
    }, ensure_ascii=False), encoding="utf-8")
    print(f"[{market.upper()}] Wrote {len(output)} commentaries ({div_count} with divergence) "
          f"to {out_path}. Tokens: in={total_in} out={total_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
