"""Generate AI morning brief + AI's own day-trade picks via Claude API.

Reads web/data/morning_brief_jp.json (produced by build_morning_brief.py),
sends a rich prompt to Claude, and appends:
  - `ai_brief`         : narrative commentary (Japanese, ~400-500 chars)
  - `ai_picks`         : Claude's own curated 5-7 day-trade picks, each
                         with {ticker, name, type, rationale, entry, target, risk}
  - `ai_brief_model`   : model id used

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
BRIEF_PATH = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 3000


def build_prompt(data: dict) -> str:
    def fmt_macro(rows: list[dict]) -> str:
        return "\n".join(f"- {r['label']}: {r['value']:,.2f} 前日比 {r['change_pct']:+.2f}%" for r in rows)

    def fmt_sectors(rows: list[dict]) -> str:
        return "\n".join(
            f"- {r['label']} ({r['ticker']}): {r['change_pct']:+.2f}% → 日本の {', '.join(r['jp_sectors'])}"
            for r in rows
        )

    def fmt_gap(rows: list[dict]) -> str:
        return "\n".join(
            f"- {r['ticker']} {r['name']} ({r['sector17']}): 引け {r['close']:,}円 ← 米{r['trigger_label']} {r['trigger_change']:+.2f}%"
            for r in rows[:10]
        )

    def fmt_pool(rows: list[dict]) -> str:
        lines = []
        for r in rows:
            cats = "/".join(r.get("appears_in", [])) or "-"
            gap = f" [{r['gap_trigger']}]" if r.get("gap_trigger") else ""
            ret5 = f"{r['ret_5d']:+.1f}%" if r.get('ret_5d') is not None else "-"
            rsi = f"{r['rsi14']:.0f}" if r.get('rsi14') is not None else "-"
            lines.append(
                f"- {r['ticker']} {r['name']} ({r['sector17']}): "
                f"引け{r['close']:,}円 日次{r['daily_return']:+.2f}% 週次{ret5} "
                f"出来高×{r['volume_ratio']:.1f} 値幅{r['range_pct']:.1f}% "
                f"代金{r['turnover_oku']}億円 RSI{rsi} "
                f"20日高値比{r['pct_from_high_20']:+.1f}% 20日安値比{r['pct_from_low_20']:+.1f}% "
                f"複数カテゴリ:{cats}{gap}"
            )
        return "\n".join(lines)

    return f"""あなたは経験豊富な日本株デイトレ向けマーケットアナリストです。
以下のデータは {data['asof']} 終値ベース + 米国市場の夜間動向です。
本日(寄付以降)のデイトレ戦略について **2 つの成果物** を出してください。

----------------------------------------
【成果物1: 朝のマーケット解説(narrative)】
- 400〜500字程度の日本語
- 冒頭で夜間〜寄付の地合いを1文要約(日経先物・USD/JPY・SOX 等)
- 注目セクターと根拠を簡潔に
- 末尾に「注意点」を1〜2点
- 投資助言ではなく「観察と仮説」のトーン
- 見出し記号は使わない、自然な段落

----------------------------------------
【成果物2: あなたが独自に選ぶデイトレ推奨銘柄 5〜7 個(JSON形式)】

下記の【候補プール】から、**あなた自身の判断**で本日デイトレ向きと考える銘柄を 5〜7 個ピックしてください。

選定の観点(複数該当が望ましい):
- 出来高急増 × 複数カテゴリ重複 = 注目度
- セクター追い風(米セクターETF騰落と整合)
- 20日高値からの距離 = ブレイクアウト余地 or 過熱警戒
- RSI による過熱・売られすぎ
- 流動性(売買代金) - デイトレなら最低 10 億円/日 が目安

各銘柄について次の構造で出力:
- `ticker`: 文字列(例 "3687.T")
- `name`: 銘柄名
- `type`: "順張り(続伸)" / "順張り(ブレイクアウト)" / "逆張り(売られすぎ反発)" / "セクター連動" のいずれか
- `rationale`: 80〜120字の選定理由 (数値根拠を含む)
- `entry`: 寄付戦略の目安 (例: "寄付直後の押し目 1,920〜1,950 円拾い")
- `target`: 利食い目安 (例: "1,980 円超で利食い検討、2,030 円が次の節目")
- `risk`: 撤退ライン or 想定リスク (例: "1,880 円割れで損切り。地合い悪化で利食い前倒し")

JSONブロックは ```json と ``` で囲んでください。narrativeの後に配置してください。

----------------------------------------
【夜間マクロ指標】
{fmt_macro(data.get('macro_signals', []))}

【米セクター騰落 → 日本セクター対応】
{fmt_sectors(data.get('sector_signals', []))}

【ギャップアップ候補】
{fmt_gap(data.get('gap_candidates', {}).get('up', []))}

【ギャップダウン候補】
{fmt_gap(data.get('gap_candidates', {}).get('down', []))}

----------------------------------------
【候補プール (合算スコア順 / 主要指標つき)】
{fmt_pool(data.get('ai_input_pool', []))}
"""


JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```", re.IGNORECASE)


def parse_ai_picks(text: str) -> tuple[str, list[dict]]:
    """Split narrative from a trailing ```json``` block. Returns (narrative, picks)."""
    m = JSON_BLOCK_RE.search(text)
    if not m:
        return text.strip(), []
    narrative = text[: m.start()].strip()
    json_str = m.group(1).strip()
    try:
        parsed = json.loads(json_str)
        if isinstance(parsed, list):
            return narrative, parsed
        if isinstance(parsed, dict) and "picks" in parsed and isinstance(parsed["picks"], list):
            return narrative, parsed["picks"]
        return narrative, []
    except json.JSONDecodeError as e:
        print(f"WARN: failed to parse AI picks JSON: {e}", file=sys.stderr)
        return narrative, []


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
    narrative, picks = parse_ai_picks(text)

    data["ai_brief"] = narrative
    data["ai_picks"] = picks
    data["ai_brief_model"] = MODEL
    # Drop ai_input_pool from the published JSON — it's only needed at LLM
    # generation time and would bloat the file the frontend downloads.
    data.pop("ai_input_pool", None)
    BRIEF_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(
        f"AI brief: {len(narrative)} chars, picks: {len(picks)}. "
        f"Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
