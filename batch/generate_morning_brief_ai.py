"""Generate AI-written morning brief for JP day-trade picks via Claude API.

Reads web/data/morning_brief_jp.json (produced by build_morning_brief.py),
sends a focused prompt to Claude, and appends a Japanese commentary as
`ai_brief` and `ai_brief_model` to the same file.

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
BRIEF_PATH = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1400


def build_prompt(data: dict) -> str:
    def fmt_macro(rows: list[dict]) -> str:
        return "\n".join(f"- {r['label']}: {r['value']:,.2f}  前日比 {r['change_pct']:+.2f}%" for r in rows)

    def fmt_sectors(rows: list[dict]) -> str:
        return "\n".join(
            f"- {r['label']} ({r['ticker']}): {r['change_pct']:+.2f}% → 日本の {', '.join(r['jp_sectors'])}"
            for r in rows
        )

    def fmt_gap(rows: list[dict]) -> str:
        return "\n".join(
            f"- {r['ticker']} {r['name']} ({r['sector17']}): 引け {r['close']:,}円 / 米{r['trigger_label']} {r['trigger_change']:+.2f}% を受けて"
            for r in rows[:10]
        )

    def fmt_picks(rows: list[dict], extra=False) -> str:
        lines = []
        for r in rows[:10]:
            base = f"- {r['ticker']} {r['name']}: 引け {r['close']:,}円  前日比 {r['daily_return']:+.2f}%  出来高×{r['volume_ratio']}  値幅 {r['range_pct']:.1f}%"
            if extra and r.get("rsi14") is not None:
                base += f"  RSI14={r['rsi14']}"
            lines.append(base)
        return "\n".join(lines) if lines else "(該当なし)"

    return f"""あなたは経験豊富な日本株デイトレ向けマーケットアナリストです。
以下のデータは{data['asof']}終値ベース + 米国市場の夜間動向です。
明日 (=本日寄付) のデイトレ向けに、約400〜500字の朝のブリーフを日本語で書いてください。

【書き方の指示】
- 冒頭で「夜間〜寄付の地合い」を一言で要約(日経先物・USD/JPY・SOX等から)
- 次に「注目セクター」を1〜2つ取り上げ、代表銘柄を1〜3個挙げる
- ギャップアップ/ダウン候補から「特に狙いどころ」を1〜2銘柄ピックアップして売買シナリオ(寄付高値追い・押し目待ち・利食い目安など)に短く触れる
- 末尾に「注意点」を1〜2点(イベント・想定リスク)
- 投資助言ではなく「観察と仮説」のトーン。「〜が期待される」「〜には警戒」程度
- 見出し記号 (## や **) は使わない、自然な段落 + 末尾の箇条書きでまとめる

---【夜間マクロ指標】---
{fmt_macro(data.get('macro_signals', []))}

---【米セクター騰落 (前日比) と日本セクター対応】---
{fmt_sectors(data.get('sector_signals', []))}

---【ギャップアップ候補(米セクター上昇 → 日本対応セクター)】---
{fmt_gap(data.get('gap_candidates', {}).get('up', []))}

---【ギャップダウン候補】---
{fmt_gap(data.get('gap_candidates', {}).get('down', []))}

---【日本株デイトレ・総合スコアTOP10】---
{fmt_picks(data.get('picks', {}).get('total_score', []))}

---【出来高急増TOP10】---
{fmt_picks(data.get('picks', {}).get('volume_surge', []))}

---【値幅大TOP10】---
{fmt_picks(data.get('picks', {}).get('high_range', []))}

---【続伸候補(上昇 + 出来高急増)】---
{fmt_picks(data.get('picks', {}).get('momentum_long', []))}

---【反転候補(下落 + 出来高 + RSI<30)】---
{fmt_picks(data.get('picks', {}).get('reversal_long', []), extra=True)}
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not BRIEF_PATH.exists():
        print(f"ERROR: {BRIEF_PATH} not found — run build_morning_brief.py first", file=sys.stderr)
        return 1

    data = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    prompt = build_prompt(data)

    client = anthropic.Anthropic(api_key=api_key)
    print(f"Calling Claude ({MODEL})...", file=sys.stderr)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    data["ai_brief"] = text
    data["ai_brief_model"] = MODEL
    BRIEF_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(
        f"AI brief added: {len(text)} chars. Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
