"""Generate AI commentary for the Macro Fundamentals tab.

Reads web/data/nikkei_eps_history.json (NK225 close + 加重PER + 導出EPS),
asks Claude to produce a structured analysis covering:
  - PER水準の評価(割安/中立/割高)
  - EPSトレンド(増益局面か減益局面か)
  - 株価変動の要因分解(ファンダ主導 or センチメント主導)
  - 投資家としての留意点

Appends to the same JSON file:
  - `commentary`: 400-600字 narrative
  - `level_judgment`: {"band": "割安"|"中立"|"割高"|..., "label": str}
  - `commentary_model`: model id

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
EPS_PATH = REPO_ROOT / "web" / "data" / "nikkei_eps_history.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1500


# 日経225 加重平均PERの長期参照バンド(過去20年の経験値)
PER_BANDS = [
    ("極度の割安", 0,     12,    "過去20年でほぼ底値圏。リーマンショック・コロナショック時の水準"),
    ("割安",       12,    15,    "歴史的に魅力的な水準。配当利回りが相対的に高くなる局面"),
    ("中立",       15,    18,    "長期平均ゾーン。マーケットが落ち着いている時の標準値"),
    ("やや割高",   18,    21,    "業績拡大期待が織り込まれた水準。慎重に推移を見るべき"),
    ("割高",       21,    999,   "過去の天井圏に近い水準。反落リスクへの警戒が必要"),
]


def classify_per(per: float | None) -> dict:
    if per is None:
        return {"band": "判定不能", "label": "PERデータなし", "explanation": ""}
    for band, lo, hi, expl in PER_BANDS:
        if lo <= per < hi:
            return {"band": band, "label": f"{band}({per:.2f}倍)", "explanation": expl}
    return {"band": "判定不能", "label": f"{per:.2f}倍(範囲外)", "explanation": ""}


def build_prompt(data: dict) -> str:
    history = data.get("history") or []
    if not history:
        return ""
    latest = history[-1]
    earliest = history[0]
    n = len(history)

    # Recent 30 days as condensed list
    tail = history[-30:]
    timeline_lines = []
    for h in tail:
        timeline_lines.append(
            f"  {h.get('date')}: close ¥{h.get('close','-'):,.2f}, "
            f"加重PER {h.get('per_weighted','-')}倍, EPS ¥{h.get('eps_weighted','-'):,.0f}"
        )

    latest_per = latest.get("per_weighted")
    judgment = classify_per(latest_per)

    # Price vs EPS deltas (over the available history)
    if n >= 2:
        first_close = next((h["close"] for h in history if h.get("close")), None)
        first_eps = next((h["eps_weighted"] for h in history if h.get("eps_weighted")), None)
        close_delta_pct = (
            (latest.get("close", 0) / first_close - 1) * 100
            if first_close else None
        )
        eps_delta_pct = (
            (latest.get("eps_weighted", 0) / first_eps - 1) * 100
            if first_eps else None
        )
    else:
        close_delta_pct = None
        eps_delta_pct = None

    bands_table = "\n".join(
        f"- {b}: {lo}〜{hi}倍 — {expl}"
        for b, lo, hi, expl in PER_BANDS[:-1]
    ) + f"\n- 割高: 21倍超 — 過去の天井圏に近い水準"

    return f"""あなたは日本株のシニアアナリストです。日経225の予想EPSと加重平均PERの推移から、現在のマーケット位置を**400〜600字の観察記録**として解説してください。

【最新時点 {latest.get('date')}】
- 終値: ¥{latest.get('close', 0):,.2f}
- 加重平均 PER: {latest.get('per_weighted', '-')}倍
- 指数ベース PER: {latest.get('per_index_based', '-')}倍
- 予想 EPS: ¥{latest.get('eps_weighted', '-'):,.0f}
- EPS 前年比: {latest.get('eps_weighted_yoy_pct', '蓄積中')}

【蓄積期間】 {earliest.get('date')} 〜 {latest.get('date')}({n}日分)
- 期間中の終値変化: {f"{close_delta_pct:+.2f}%" if close_delta_pct is not None else "-"}
- 期間中のEPS変化:  {f"{eps_delta_pct:+.2f}%" if eps_delta_pct is not None else "-"}

【直近30日の推移】
{chr(10).join(timeline_lines)}

【日経225 加重平均PERの長期参照バンド(過去20年の経験則)】
{bands_table}

【解説で必ず触れるべき4点】
1. 現在のPER水準の評価(上記バンドのどこに位置し、過去平均15〜18倍と比較してどうか)
2. EPSのトレンド(増益局面か、減益局面か、横ばいか — 「蓄積中」の場合は方向感のみ)
3. 直近の株価変動の主因分析(EPS主導=ファンダ主導 or PER主導=センチメント主導)
   - 例:「株価上昇の◯%はEPS増加で説明され、残りは PER 拡大によるバリュエーション上昇」
4. 投資家として留意すべきポイント(過熱警戒 or 押し目妙味 or 様子見等)

【書き方】
- 見出し記号(#や##や**)は使わず、自然な段落構成で
- 投資勧誘・断定的な「買え/売れ」は禁止、観察記録のトーン
- 数値根拠を必ず明記(感覚論禁止)
- 塾長スタイル(冷静・教育的・分析的)
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not EPS_PATH.exists():
        print(f"ERROR: {EPS_PATH} not found", file=sys.stderr)
        return 1

    data = json.loads(EPS_PATH.read_text(encoding="utf-8"))
    history = data.get("history") or []
    if not history:
        print("ERROR: no history yet", file=sys.stderr)
        return 1

    latest = history[-1]
    judgment = classify_per(latest.get("per_weighted"))
    print(f"[MACRO] judgment: {judgment['label']}", file=sys.stderr)

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt(data)
    print(f"[MACRO] Calling Claude ({MODEL})...", file=sys.stderr)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = re.sub(r"^#+\s*[^\n]*\n+", "", text)  # strip leading heading
    except Exception as e:
        print(f"[MACRO] Claude error: {e}", file=sys.stderr)
        return 1

    data["commentary"] = text
    data["level_judgment"] = judgment
    data["commentary_model"] = MODEL
    EPS_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    print(f"[MACRO] Wrote commentary ({len(text)} chars). "
          f"Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
