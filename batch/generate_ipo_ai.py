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
PROFILES = REPO_ROOT / "data" / "ipo_profiles_jp.json"

# 1回の実行で日本語の一言を作る上限。全銘柄ぶんを一度に投げるとプロンプトが
# 長くなりすぎて1件あたりが雑になる。キャッシュするので数日で全件埋まる。
MAX_BIZ = int(os.getenv("IPO_BIZ_PER_RUN", "30"))

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


def biz_prompt(rows: list[dict]) -> str:
    """英文の会社概要から、日本語の一言を作らせるプロンプト。

    翻訳ではなく要約を頼む。英文をそのまま訳すと「〜を所有し運営する
    オンラインコミュニティプラットフォーム」のような読みにくい文になる。
    """
    lines = []
    for r in rows:
        lines.append(
            f"- {r['code']} {r['name']}（{r.get('sector_ja') or r.get('sector_en') or '業種不明'}"
            f" / {r.get('industry_en') or '—'}）\n"
            f"  {(r.get('summary_en') or '')[:600]}"
        )
    return f"""次の会社について、**何をしている会社か**を日本語で一言にしてください。

原文は英語の会社概要です。逐語訳ではなく、日本語として自然な短い説明に
してください。「〜を所有し運営する」のような直訳調は避けてください。

会社:
{chr(10).join(lines)}

約束:
- 各社40字以内。何で稼いでいるかが分かること
- 業界用語をそのまま並べない。初めて聞く人にも伝わる言葉にする
- 原文に書かれていないことを足さない。分からなければ "情報不足" と書く
- 株価や投資判断には触れない

次のJSON形式だけを ```json ブロックで返してください。

```json
{{"biz": [{{"code": "621A", "text": "音楽素材を作り手から集めて企業に売るサイトを運営"}}]}}
```
"""


def fill_biz_ja(client, page: dict) -> int:
    """会社プロフィールに日本語の一言（biz_ja）を足す。作った分だけ返す。

    キャッシュは data/ipo_profiles_jp.json に書く。web/data 側ではなく
    ここに置くのは、これが「取り直さなくてよい情報」だから。画面用の
    JSONは build_ipo_page.py が毎回作り直すので、そこに書くと消える。
    """
    if not PROFILES.exists():
        print("プロフィールのキャッシュが無いため、日本語の一言は作りません。",
              file=sys.stderr)
        return 0
    try:
        cache = json.loads(PROFILES.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        print("プロフィールのキャッシュが読めませんでした。", file=sys.stderr)
        return 0

    profiles = cache.get("profiles") or {}
    by_code = {str(r["code"]): r for r in page.get("ipos", [])}

    todo = []
    for code, p in profiles.items():
        if not p.get("ok") or p.get("biz_ja"):
            continue
        if not p.get("summary_en"):
            continue
        row = by_code.get(code)
        todo.append({
            "code": code,
            "name": (row or {}).get("name") or code,
            "sector_ja": p.get("sector_ja"), "sector_en": p.get("sector_en"),
            "industry_en": p.get("industry_en"), "summary_en": p.get("summary_en"),
        })
    todo = todo[:MAX_BIZ]
    if not todo:
        print("日本語の一言は全銘柄ぶん揃っています。", file=sys.stderr)
        return 0

    print(f"日本語の一言を {len(todo)} 件作ります。", file=sys.stderr)
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": biz_prompt(todo)}],
        )
    except Exception as e:
        print(f"一言の生成に失敗しました: {e}", file=sys.stderr)
        return 0

    parsed = extract_json("".join(b.text for b in resp.content if b.type == "text"))
    rows = (parsed or {}).get("biz") or []
    allowed = {r["code"] for r in todo}

    n = 0
    for r in rows:
        code, text = str(r.get("code") or ""), (r.get("text") or "").strip()
        # 候補外・空・「情報不足」は書き込まない。書くと再挑戦されなくなる。
        if code not in allowed or not text or "情報不足" in text:
            continue
        profiles[code]["biz_ja"] = text
        n += 1

    if n:
        cache["profiles"] = profiles
        tmp = PROFILES.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, PROFILES)
    print(f"  {n} 件を書き込みました。", file=sys.stderr)
    return n


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

    client = anthropic.Anthropic(api_key=key)

    # 会社の一言は上場予定・直近とは別に、キャッシュが空いている銘柄を埋める。
    # こちらが失敗しても下の解説生成は続ける。
    try:
        fill_biz_ja(client, data)
    except Exception as e:
        print(f"一言の生成でエラー: {e}", file=sys.stderr)

    try:
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
