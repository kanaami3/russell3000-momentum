"""Generate AI commentary for the value-investing tab.

Reads web/data/value_jp.json, sends ranking summaries to Claude, and appends:
  - `overall_commentary`: cross-cutting analysis (current value-investing landscape
    in JP, what categories look most attractive, value-trap warnings)
  - `commentary` field on each of the 6 ranking categories
    (brief 150-200 char interpretation of that category's current picks)
  - `ai_picks`: 5-7 stocks Claude selects as 'most attractive value buys',
    each with rationale + risks + price-target observation

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
VALUE_PATH = REPO_ROOT / "web" / "data" / "value_jp.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS_NARRATIVE = 1500
MAX_TOKENS_PICKS = 2500
MAX_TOKENS_CATEGORY = 500

CATEGORY_LABELS = {
    "high_dividend": "💰 高配当 TOP30",
    "low_pe":        "💎 低PER TOP30",
    "low_pbr":       "📉 低PBR TOP30",
    "high_roe":      "⚡ 高ROE TOP30",
    "growth":        "🌱 成長性 TOP30",
    "composite":     "🏆 総合バリュースコア TOP30",
}


def fmt_pick(p: dict) -> str:
    return (
        f"- {p['ticker']} {p['name']} ({p.get('sector17','')}): "
        f"配当{p.get('dividend_yield','-')}% PER{p.get('trailing_pe','-')} "
        f"PBR{p.get('price_to_book','-')} ROE{p.get('return_on_equity','-')}% "
        f"売上成長{p.get('revenue_growth','-')}% 営業益率{p.get('operating_margins','-')}% "
        f"配当性向{p.get('payout_ratio','-')}% "
        f"時価総額{p.get('market_cap_oku','-')}億円 "
        f"スコア{p.get('value_score','-')}"
    )


def _ranking_block(data: dict) -> str:
    rk = data["rankings"]
    def lst(name, n=10):
        return "\n".join(fmt_pick(p) for p in rk.get(name, [])[:n])
    return f"""---【総合スコアTOP10】---
{lst('composite')}

---【高配当TOP10】---
{lst('high_dividend')}

---【低PER TOP10】---
{lst('low_pe')}

---【低PBR TOP10】---
{lst('low_pbr')}

---【高ROE TOP10】---
{lst('high_roe')}

---【成長性TOP10】---
{lst('growth')}"""


def build_narrative_prompt(data: dict) -> str:
    return f"""あなたは経験豊富なバリュー投資アナリストです。以下は本日 ({data['asof']}) 時点の東証プライム約{data['filtered_count']}銘柄のバリュー投資スクリーニング結果です。

「現在のバリュー局面分析」を**約400〜500字**の日本語で書いてください。

【要件】
- 6カテゴリ全体を俯瞰した観察(現在の市場でバリュー的に魅力的なセクター・特徴)
- 注目すべき1〜3銘柄を固有名で言及
- 「バリュートラップの可能性」として注意喚起(高配当・低PBRに潜むリスク)
- 投資勧誘ではなく、観察・分析のトーン
- **見出し記号(#や##や**)は一切使わず、自然な段落で**

{_ranking_block(data)}
"""


def build_picks_prompt(data: dict) -> str:
    return f"""あなたは経験豊富なバリュー投資アナリストです。以下は本日 ({data['asof']}) のバリュー投資スクリーニング結果です。

**配当 + 割安 + 売上継続 + 成長見込み** の4条件にバランス良く該当する銘柄を 5〜7個ピックし、JSON形式で出力してください。

【選定基準(複数該当)】
- 配当 3%以上(できれば 4%以上)、配当性向 80%以下(無理のない配当)
- PER 15以下(できれば 10以下)、PBR 1.5以下
- ROE 10%以上(資本効率良好)
- 売上成長 プラス(できれば 5%以上)、過度な成長(>100%)は M&A 起因の可能性
- 営業利益率 5%以上が望ましい(本業の収益性)
- 業界が衰退局面でない(セクター分散好ましい)

【出力形式】 **必ず ```json と ``` で囲んだ単独の JSON 配列のみ** を返してください(narrative や見出しは一切含めない)。

各銘柄の構造:
- `ticker`: 文字列(例 "1911.T")
- `name`: 銘柄名
- `appeal`: 80〜120字の選定理由(具体的数値で)
- `growth_evidence`: 売上・利益の継続性根拠(40〜80字)
- `risk`: 注意点・バリュートラップ可能性(40〜80字)
- `target_observation`: 「現在PBR X倍は割安水準」「配当利回り X% は同業平均より高い」等の客観観察(40〜80字)

{_ranking_block(data)}
"""


def build_category_prompt(category_key: str, picks: list[dict]) -> str:
    label = CATEGORY_LABELS.get(category_key, category_key)
    return f"""以下は東証プライムの「{label}」上位10銘柄です。
このカテゴリの**現状を150〜200字で簡潔に解説**してください。

- カテゴリ全体の特徴(セクター傾向、業界共通要因)
- 注目すべき1〜2銘柄を簡単に言及
- このカテゴリ固有の注意点を1つ
- 投資勧誘ではなく、観察的トーン
- 見出し記号は使わず、1〜2段落で

---銘柄---
{chr(10).join(fmt_pick(p) for p in picks[:10])}
"""


JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```", re.IGNORECASE)


def parse_picks(text: str) -> tuple[str, list[dict]]:
    m = JSON_BLOCK_RE.search(text)
    if not m:
        return text.strip(), []
    narrative = text[: m.start()].strip()
    try:
        parsed = json.loads(m.group(1).strip())
        if isinstance(parsed, list):
            return narrative, parsed
        if isinstance(parsed, dict) and "picks" in parsed:
            return narrative, parsed["picks"]
    except json.JSONDecodeError as e:
        print(f"WARN: AI picks JSON parse error: {e}", file=sys.stderr)
    return narrative, []


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not VALUE_PATH.exists():
        print(f"ERROR: {VALUE_PATH} not found", file=sys.stderr)
        return 1

    data = json.loads(VALUE_PATH.read_text(encoding="utf-8"))
    client = anthropic.Anthropic(api_key=api_key)

    total_in = 0
    total_out = 0

    # Narrative
    print(f"Claude → overall narrative ({MODEL})...", file=sys.stderr)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_NARRATIVE,
            messages=[{"role": "user", "content": build_narrative_prompt(data)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        # Strip any leading markdown heading line (the AI sometimes ignores the no-heading rule)
        text = re.sub(r"^#+\s*[^\n]*\n+", "", text)
        data["overall_commentary"] = text
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        print(f"  narrative: {len(text)} chars", file=sys.stderr)
    except Exception as e:
        print(f"  ERROR narrative: {e}", file=sys.stderr)

    # AI picks (separate call for reliable JSON parsing)
    print(f"Claude → AI picks ({MODEL})...", file=sys.stderr)
    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS_PICKS,
            messages=[{"role": "user", "content": build_picks_prompt(data)}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        _, picks = parse_picks(text)
        # Fallback: if JSON block wasn't fenced, try direct parse of whole text
        if not picks:
            try:
                stripped = text.strip().strip("`").strip()
                if stripped.startswith("json"):
                    stripped = stripped[4:].strip()
                parsed = json.loads(stripped)
                if isinstance(parsed, list):
                    picks = parsed
            except Exception:
                pass
        data["ai_picks"] = picks
        total_in += resp.usage.input_tokens
        total_out += resp.usage.output_tokens
        print(f"  AI picks: {len(picks)}", file=sys.stderr)
    except Exception as e:
        print(f"  ERROR picks: {e}", file=sys.stderr)

    # Per-category short commentary
    cat_commentary = {}
    for key in data["rankings"].keys():
        try:
            print(f"  Claude → {key}...", file=sys.stderr)
            resp = client.messages.create(
                model=MODEL,
                max_tokens=MAX_TOKENS_CATEGORY,
                messages=[{"role": "user", "content": build_category_prompt(key, data["rankings"][key])}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            cat_commentary[key] = text
            total_in += resp.usage.input_tokens
            total_out += resp.usage.output_tokens
        except Exception as e:
            print(f"    ERROR {key}: {e}", file=sys.stderr)

    data["category_commentary"] = cat_commentary
    data["commentary_model"] = MODEL

    VALUE_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(f"Tokens used: in={total_in} out={total_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
