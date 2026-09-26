#!/usr/bin/env python3
"""IPO銘柄の見どころをClaudeに言語化させる。

対象は「これから上場する銘柄」と「上場して間もない銘柄」。2年前の銘柄まで
毎日コメントを付け直しても読まれないし、費用も嵩む。

**売買のタイミングは書かせない。**
エントリー価格や「押し目」といった水準は出さない。塾生が数字をそのまま
使ってしまうし、朝のデイトレ・ピックアップと役割が重なる。ここで欲しいのは
「この会社は何をしていて、どこを見ておくべきか」であって、買い場ではない。

**数字はモデルに書かせず、元データで上書きする。**
公開価格や初値をモデルの記憶から書かせると転記ミスが混ざる。文章だけを
任せ、画面に出る数値は build_ipo_page.py が計算したものを使う。

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic

REPO_ROOT = Path(__file__).resolve().parent.parent
PAGE_PATH = REPO_ROOT / "web" / "data" / "ipo_jp.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 3000

JST = timezone(timedelta(hours=9))

# 上場からこの日数以内を「間もない」とみなす
RECENT_DAYS = 90
# モデルに渡す上限。多すぎると1件あたりが薄くなる。
MAX_UPCOMING = 6
MAX_RECENT = 8

JSON_BLOCK_RE = re.compile(r"```json\s*([\s\S]+?)\s*```", re.IGNORECASE)


def _f(v, unit="", nd=1):
    return f"{v:.{nd}f}{unit}" if isinstance(v, (int, float)) else "—"


def pick_targets(ipos: list[dict], today) -> tuple[list[dict], list[dict]]:
    upcoming = [r for r in ipos if r.get("status") == "upcoming"]
    upcoming.sort(key=lambda r: r.get("listing_date") or "")

    recent = []
    for r in ipos:
        if r.get("status") != "listed":
            continue
        d = r.get("listing_date")
        if not d:
            continue
        try:
            age = (today - datetime.fromisoformat(d).date()).days
        except ValueError:
            continue
        if 0 <= age <= RECENT_DAYS:
            recent.append(r)
    recent.sort(key=lambda r: r.get("listing_date") or "", reverse=True)

    return upcoming[:MAX_UPCOMING], recent[:MAX_RECENT]


def fmt_upcoming(r: dict) -> str:
    band = "—"
    if isinstance(r.get("band_low"), (int, float)):
        band = f"{r['band_low']:.0f}〜{r['band_high']:.0f}円"
    return (
        f"- {r['code']} {r['name']}（{r.get('market')}）"
        f" 上場予定 {r.get('listing_date')}"
        f" / 仮条件 {band}"
        f" / 公開価格 {_f(r.get('offer_price'), '円', 0)}"
        f" / 公募 {_f(r.get('public_shares_k'), '千株')}"
        f" 売出 {_f(r.get('secondary_shares_k'), '千株')}"
    )


def fmt_recent(r: dict) -> str:
    return (
        f"- {r['code']} {r['name']}（{r.get('market')}）"
        f" 上場 {r.get('listing_date')}"
        f" / 公開価格 {_f(r.get('offer_price'), '円', 0)}"
        f" → 初値 {_f(r.get('first_price'), '円', 0)}"
        f"（{_f(r.get('first_pop_pct'), '%')}）"
        f" / 現在 {_f(r.get('last_close'), '円', 0)}"
        f" / 初値比 {_f(r.get('from_first_pct'), '%')}"
        f" / 上場来高値から {_f(r.get('drawdown_pct'), '%')}"
    )


def build_prompt(upcoming: list[dict], recent: list[dict]) -> str:
    return f"""あなたは投資塾でIPOを解説する分析担当です。
下の銘柄について、塾生向けの短い解説を書いてください。

【これから上場する銘柄】
{chr(10).join(fmt_upcoming(r) for r in upcoming) or "（今のところありません）"}

【上場して間もない銘柄】
{chr(10).join(fmt_recent(r) for r in recent) or "（該当なし）"}

書き方の約束:
- 事業内容が分かるように、何で稼いでいる会社かを一言で示す
- 公募・売出の規模から、需給が締まりやすいか緩みやすいかに触れてよい
- **買い時や売り時、目標株価、エントリー価格は書かないこと**
- 「必ず上がる」「狙い目」のような断定や煽りは使わない
- 会社について確信が持てない場合は、推測で埋めず「情報が少ない」と書く

次のJSON形式だけを ```json ブロックで返してください。

```json
{{
  "upcoming": [
    {{
      "code": "648A",
      "headline": "20字以内。どんな会社かを一言で",
      "note": "120字程度。事業内容と、規模から見た需給の特徴。注意点も含める"
    }}
  ],
  "recent": [
    {{
      "code": "621A",
      "headline": "20字以内",
      "note": "120字程度。初値後の値動きが何を示しているか。今後見ておくべき点"
    }}
  ],
  "summary": "150字程度。足元のIPO市場の雰囲気。件数、規模、初値の付き方の傾向など"
}}
```
"""


def extract_json(text: str) -> dict | None:
    m = JSON_BLOCK_RE.search(text)
    candidates = [m.group(1)] if m else []
    candidates.append(text)
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        candidates.insert(0 if not m else 1, text[i:j + 1])
    for c in candidates:
        try:
            return json.loads(c)
        except json.JSONDecodeError:
            continue
    return None


def main() -> int:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY が未設定です。スキップします。", file=sys.stderr)
        return 0
    if not PAGE_PATH.exists():
        print(f"{PAGE_PATH} がありません。", file=sys.stderr)
        return 0

    data = json.loads(PAGE_PATH.read_text(encoding="utf-8"))
    ipos = data.get("ipos", [])
    today = datetime.now(JST).date()
    upcoming, recent = pick_targets(ipos, today)
    print(f"上場予定 {len(upcoming)} 件 / 直近上場 {len(recent)} 件", file=sys.stderr)

    if not upcoming and not recent:
        print("対象がないため生成しません。", file=sys.stderr)
        return 0

    try:
        client = anthropic.Anthropic(api_key=key)
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": build_prompt(upcoming, recent)}],
        )
    except Exception as e:
        # 失敗しても既存の ai_ipo をそのまま残す。消すと画面から
        # セクションごと消えてしまう。
        print(f"API呼び出しに失敗しました: {e}", file=sys.stderr)
        print("既存の内容を維持して正常終了します。", file=sys.stderr)
        return 0

    parsed = extract_json("".join(b.text for b in resp.content if b.type == "text"))
    if not parsed:
        print("JSONを解釈できませんでした。既存の内容を維持します。", file=sys.stderr)
        return 0

    # 候補外のコードが混ざっていないか検証する。存在しない銘柄の解説が
    # 画面に出ると、どの銘柄の話なのか追えなくなる。
    allowed = {str(r["code"]) for r in upcoming + recent}
    out = {}
    for section in ("upcoming", "recent"):
        rows = parsed.get(section) or []
        kept = [r for r in rows if str(r.get("code")) in allowed]
        dropped = len(rows) - len(kept)
        if dropped:
            print(f"  {section}: 候補外の {dropped} 件を除外", file=sys.stderr)
        out[section] = kept

    if not out["upcoming"] and not out["recent"]:
        print("採用できる解説がありませんでした。", file=sys.stderr)
        return 0

    data["ai_ipo"] = {
        **out,
        "summary": parsed.get("summary", ""),
        "model": MODEL,
        "generated_at_jst": datetime.now(JST).isoformat(timespec="seconds"),
        "recent_days": RECENT_DAYS,
    }

    tmp = PAGE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, PAGE_PATH)

    print(f"解説 {len(out['upcoming'])} + {len(out['recent'])} 件を書き出しました。",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
