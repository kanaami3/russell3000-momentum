"""Generate daily market commentary using the Claude API for the chosen market.

Usage:
    ANTHROPIC_API_KEY=... python batch/generate_summary.py us
    ANTHROPIC_API_KEY=... python batch/generate_summary.py jp
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

MARKET_LABELS = {
    "us": {
        "title": "ラッセル3000",
        "intro": "あなたは経験豊富な米国株市場アナリストです。",
        "sectors_hint": "セクター傾向(半導体・テクノロジー・小売・ヘルスケア・エネルギー・金融など)",
    },
    "jp": {
        "title": "東証プライム",
        "intro": "あなたは経験豊富な日本株市場アナリストです。",
        "sectors_hint": "セクター傾向(自動車・電機・小売・銀行・商社・素材・医薬品・サービスなど)",
    },
}


def build_prompt(data: dict, market: str) -> str:
    labels = MARKET_LABELS[market]

    def fmt_list(rows: list[dict], label: str) -> str:
        lines = [f"- {r['ticker']} ({r['name']}): {r['value']:+.2f}%" for r in rows[:10]]
        return f"## {label}\n" + "\n".join(lines)

    asof = data["asof"]
    n = data["ticker_count"]
    blocks = [
        fmt_list(data["yesterday_top10"], "本日 上昇TOP10"),
        fmt_list(data["yesterday_worst10"], "本日 下落ワースト10"),
        fmt_list(data["mom_1w_top10"], "1週間モメンタムTOP10"),
        fmt_list(data["mom_1m_top10"], "1ヶ月モメンタムTOP10"),
        fmt_list(data["mom_3m_top10"], "3ヶ月モメンタムTOP10"),
    ]

    return f"""{labels['intro']}以下は{labels['title']}({n}銘柄)の {asof} 終値ベースのモメンタムデータです。

これを元に、日本の個人投資家向けに**約400〜500字**の市場サマリーを日本語で書いてください。

要件:
- {labels['sectors_hint']}を読み取って言及
- 注目すべき個別銘柄を2〜4つ、固有名と数値を引用しながら紹介
- 上昇/下落の双方に触れる
- 「注目ポイント」として箇条書きで2〜3点まとめる
- 投資助言ではなく事実と観察の整理に徹する
- 見出し記号(##や**)は使わず、自然な日本語の段落 + 末尾の箇条書きで

---データ---

{chr(10).join(blocks)}
"""


def main() -> int:
    market = (sys.argv[1] if len(sys.argv) > 1 else "us").lower()
    if market not in ("us", "jp"):
        print(f"ERROR: market must be 'us' or 'jp', got '{market}'", file=sys.stderr)
        return 1

    momentum_path = REPO_ROOT / "web" / "data" / f"momentum_{market}.json"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1

    if not momentum_path.exists():
        print(f"ERROR: {momentum_path} not found — run calc_momentum.py first", file=sys.stderr)
        return 1

    data = json.loads(momentum_path.read_text(encoding="utf-8"))
    prompt = build_prompt(data, market)

    client = anthropic.Anthropic(api_key=api_key)
    print(f"[{market.upper()}] Calling Claude ({MODEL})...", file=sys.stderr)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    summary = "".join(block.text for block in resp.content if block.type == "text").strip()

    data["market_summary"] = summary
    data["market_summary_model"] = MODEL
    momentum_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    chars = len(summary)
    print(f"[{market.upper()}] Summary added ({chars} chars). Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
