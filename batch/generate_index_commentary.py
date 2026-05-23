"""Generate per-index AI commentary for the index-monitor tab.

Reads web/data/indices.json (produced by fetch_indices.py), sends each
index's full indicator set to Claude, and appends:
  - `commentary` field on each index (200-300 chars JP)
  - top-level `overall_commentary` summarizing cross-asset picture

Each commentary explains:
  1. current technical state synthesis
  2. nuance behind the headline judgement (e.g. why 失速 in an uptrend)
  3. the specific threshold/condition that would flip the regime

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
INDICES_PATH = REPO_ROOT / "web" / "data" / "indices.json"

MODEL = "claude-haiku-4-5-20251001"
PER_INDEX_TOKENS = 600
OVERALL_TOKENS = 800


def _ind_block(ix: dict) -> str:
    ind = ix["indicators"]
    cur = ix["current"]

    def f(v):
        if v is None: return "-"
        if isinstance(v, float): return f"{v:,.2f}"
        if isinstance(v, int):   return f"{v:,}"
        return str(v)

    return (
        f"指数: {ix['label']} ({ix['yf_symbol']})\n"
        f"現在値: {f(cur['value'])}  前日比 {cur['change_pct']:+.2f}% ({f(cur['change'])})\n"
        f"トレンド判定: {ind['trend']}\n"
        f"RSI(14): {f(ind['rsi14'])}\n"
        f"SMA20:  {f(ind['sma20'])}  (現在値との差 {f(cur['value'] - (ind['sma20'] or 0))})\n"
        f"SMA50:  {f(ind['sma50'])}  (現在値との差 {f(cur['value'] - (ind['sma50'] or 0))})\n"
        f"SMA200: {f(ind['sma200'])}  (現在値との差 {f(cur['value'] - (ind['sma200'] or 0))} = {f(ind['pct_from_sma200'])}%)\n"
        f"MACDフェーズ: {ind.get('macd_phase','-')}  (line={f(ind['macd'])} / signal={f(ind['macd_signal'])} / hist={f(ind['macd_hist'])})\n"
        f"ボリンジャー位置: {f(ind['bb_position_pct'])}%  (下端=0/上端=100, 上下バンド: {f(ind['bb_lower'])} ~ {f(ind['bb_upper'])})\n"
        f"ATR(14): {f(ind['atr14'])} ({f(ind['atr_pct'])}% of price)"
    )


def build_per_index_prompt(ix: dict) -> str:
    return f"""あなたは技術派マーケットアナリストです。以下の指数のテクニカル状態を見て、**約200〜300字の日本語解説**を書いてください。

【要件】
- 機械的判定(トレンド・MACDフェーズ・RSI・ボリンジャー位置・SMA200比)を**統合的に解釈**
- 「強気だが過熱気味」「失速はあるが下落入りではない」等、ニュアンスを言語化
- **次にトレンド転換のサインとなる具体的水準・条件を1つ示す**(例: 「SMA50 を割れば短期調整局面入り」)
- 投資勧誘ではなく、観察的・客観的なトーン
- 見出し記号(##や**)は使わず、自然な段落で

---テクニカル指標---
{_ind_block(ix)}
"""


def build_overall_prompt(indices: list[dict], correlations: list[dict]) -> str:
    def fmt_idx(ix):
        ind = ix["indicators"]
        return (
            f"- {ix['label']}: {ix['current']['value']:,} ({ix['current']['change_pct']:+.2f}%) "
            f"トレンド={ind['trend']} MACDフェーズ={ind.get('macd_phase','-')} RSI={ind['rsi14']} "
            f"SMA200比={ind.get('pct_from_sma200','-')}% BB={ind.get('bb_position_pct','-')}%"
        )

    def fmt_corr(c):
        return f"- {c['a']} ↔ {c['b']} ({c['period_days']}日): {c['value']:+.2f}"

    return f"""あなたは経験豊富な技術派マーケットアナリストです。日経225・NASDAQ100・USD/JPY の現状を統合的に分析し、**約300〜400字の日本語解説**を書いてください。

【要件】
- 3つの市場(日本株・米テック・為替)を関連付けて解釈
- 相関データから「連動しているか/逆連動か」のヒントを読み取る
- 「全体としてリスクオン継続中」「警戒すべき分岐点」等、マクロ的な視点を提供
- EA・自動売買の判断材料として有用な観察を含める
- 見出し記号は使わず、自然な段落で

---各指数の現状---
{chr(10).join(fmt_idx(ix) for ix in indices)}

---相関(30日)---
{chr(10).join(fmt_corr(c) for c in correlations)}
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not INDICES_PATH.exists():
        print(f"ERROR: {INDICES_PATH} not found", file=sys.stderr)
        return 1

    data = json.loads(INDICES_PATH.read_text(encoding="utf-8"))
    client = anthropic.Anthropic(api_key=api_key)

    total_in = 0
    total_out = 0
    for ix in data.get("indices", []):
        try:
            print(f"  Claude → {ix['label']}...", file=sys.stderr)
            resp = client.messages.create(
                model=MODEL,
                max_tokens=PER_INDEX_TOKENS,
                messages=[{"role": "user", "content": build_per_index_prompt(ix)}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            ix["commentary"] = text
            ix["commentary_model"] = MODEL
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens
        except Exception as e:
            print(f"    ERROR for {ix.get('id')}: {e}", file=sys.stderr)

    # Cross-asset overall commentary
    try:
        print(f"  Claude → overall...", file=sys.stderr)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=OVERALL_TOKENS,
            messages=[{"role": "user", "content": build_overall_prompt(data.get("indices", []), data.get("correlations", []))}],
        )
        data["overall_commentary"] = "".join(b.text for b in resp.content if b.type == "text").strip()
        data["commentary_model"] = MODEL
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
    except Exception as e:
        print(f"  ERROR overall: {e}", file=sys.stderr)

    INDICES_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Done. Tokens used: in={total_in} out={total_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
