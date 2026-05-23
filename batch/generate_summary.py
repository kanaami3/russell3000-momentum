"""Generate daily market commentary using the Claude API.

Reads web/data/momentum.json (already produced by calc_momentum.py),
constructs a focused prompt from the top movers, and asks Claude to write
a concise Japanese market summary. The summary is written back into the
same JSON under the "market_summary" key so the frontend can render it.

Requires environment variable: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
MOMENTUM_PATH = REPO_ROOT / "web" / "data" / "momentum.json"

MODEL = "claude-haiku-4-5-20251001"  # cheap & fast; sufficient for short JP summaries
MAX_TOKENS = 1200


def build_prompt(data: dict) -> str:
    def fmt_list(rows: list[dict], label: str) -> str:
        lines = [f"- {r['ticker']} ({r['name']}): {r['value']:+.2f}%" for r in rows[:10]]
        return f"## {label}\n" + "\n".join(lines)

    asof = data["asof"]
    n = data["ticker_count"]
    blocks = [
        fmt_list(data["yesterday_top10"], "昨日 上昇TOP10"),
        fmt_list(data["yesterday_worst10"], "昨日 下落ワースト10"),
        fmt_list(data["mom_1w_top10"], "1週間モメンタムTOP10"),
        fmt_list(data["mom_1m_top10"], "1ヶ月モメンタムTOP10"),
        fmt_list(data["mom_3m_top10"], "3ヶ月モメンタムTOP10"),
    ]

    return f"""あなたは経験豊富な米国株市場アナリストです。以下はラッセル3000({n}銘柄)の {asof} 終値ベースのモメンタムデータです。

これを元に、日本の個人投資家向けに**約400〜500字**の市場サマリーを日本語で書いてください。

要件:
- セクター傾向(半導体・テクノロジー・小売・ヘルスケア・エネルギー・金融など)を読み取って言及
- 注目すべき個別銘柄を2〜4つ、固有名と数値を引用しながら紹介
- 上昇/下落の双方に触れる
- 「注目ポイント」として箇条書きで2〜3点まとめる
- 投資助言ではなく事実と観察の整理に徹する
- 見出し記号(##など)は使わず、自然な日本語の段落 + 末尾の箇条書きで

---データ---

{chr(10).join(blocks)}
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    if not MOMENTUM_PATH.exists():
        print(f"ERROR: {MOMENTUM_PATH} not found — run calc_momentum.py first", file=sys.stderr)
        return 1

    data = json.loads(MOMENTUM_PATH.read_text(encoding="utf-8"))
    prompt = build_prompt(data)

    client = anthropic.Anthropic(api_key=api_key)
    print(f"Calling Claude ({MODEL})...", file=sys.stderr)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = "".join(block.text for block in resp.content if block.type == "text").strip()

    data["market_summary"] = summary
    data["market_summary_model"] = MODEL
    MOMENTUM_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    chars = len(summary)
    print(f"Summary added ({chars} chars). Token usage: in={resp.usage.input_tokens} out={resp.usage.output_tokens}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
