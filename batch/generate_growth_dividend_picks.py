"""AIが選ぶ増配バリュー銘柄。

配当スクリーナーの上部に出す少数精鋭のピックアップ。機械的な判定（増配・
増配余地・割安・業績）を通過した候補の中から、Claude に理由付きで選ばせる。

**候補は機械で絞ってから渡す。**
829銘柄をそのまま投げても、モデルは数字を読み比べきれないし費用も嵩む。
増配実績・増益・配当性向・PER の条件で数十件まで落としてから渡す。
選定の再現性は機械側に持たせ、モデルには「なぜこれか」の言語化を任せる。

**過去の採用銘柄を渡して重複を避ける。**
毎日同じ顔ぶれが並ぶと読まれなくなる。既存の generate_value_commentary.py と
同じ考え方で、直近の採用履歴を渡して新規発掘を優先させる。

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
SCREENER_PATH = REPO_ROOT / "web" / "data" / "dividend_screener.json"
HISTORY_PATH = REPO_ROOT / "data" / "growth_dividend_picks_history.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 4000
HISTORY_LOOKBACK = 5
HISTORY_KEEP = 14

# 候補に残す条件（機械側の絞り込み）
MAX_CANDIDATES = 40
MIN_YIELD = float(os.getenv("GDP_MIN_YIELD", "2.5"))
MAX_PER = float(os.getenv("GDP_MAX_PER", "18"))
MAX_PAYOUT = float(os.getenv("GDP_MAX_PAYOUT", "60"))

JSON_BLOCK_RE = re.compile(r"```json\\s*([\\s\\S]+?)\\s*```", re.IGNORECASE)


def _num(v) -> bool:
    return isinstance(v, (int, float))


def has_record(s: dict | None) -> bool:
    if not s:
        return False
    return s.get("years", 0) >= 1 or s.get("non_decreasing", 0) >= 5


def select_candidates(stocks: list[dict]) -> list[dict]:
    """増配バリューの候補を機械的に絞る。

    ここを通らなかった銘柄はモデルに見せない。モデルが「候補外から選ぶ」
    余地を残すと、選定基準が説明できなくなる。
    """
    out = []
    for s in stocks:
        if not has_record(s.get("streak")):
            continue
        if not (_num(s.get("yield")) and s["yield"] >= MIN_YIELD):
            continue
        if not (_num(s.get("per")) and 0 < s["per"] <= MAX_PER):
            continue
        if not (_num(s.get("payout")) and 0 < s["payout"] <= MAX_PAYOUT):
            continue
        if s.get("headroom") in (None, 1):     # 増配余地なし・× は除外
            continue
        out.append(s)

    # 増配余地 → 連続増配期数 → 利回り の順で優先
    out.sort(key=lambda s: (
        -(s.get("headroom") or 0),
        -((s.get("streak") or {}).get("years") or 0),
        -(s.get("yield") or 0),
    ))
    return out[:MAX_CANDIDATES]

def fmt(s: dict) -> str:
    st = s.get("streak") or {}
    rev = s.get("rev")
    rev_txt = f"{rev:+.0f}%" if _num(rev) and s.get("revOk") else "—"
    return (
        f"- {s.get('code')} {s.get('name')}（{s.get('sector')}）"
        f" 利回り{s.get('yield')}% / PER{s.get('per')} / PBR{s.get('pbr')}"
        f" / ROE{s.get('roe')}% / 配当性向{s.get('payout')}%"
        f" / 予想増益率{rev_txt} / {st.get('label', '増配実績なし')}"
        f" / 時価総額{s.get('mcap')}億円"
    )


def _load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        return json.loads(HISTORY_PATH.read_text(encoding="utf-8")).get("runs", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_history(runs: list[dict], asof: str, picks: list[dict]) -> None:
    runs = [{"date": asof, "tickers": [p.get("code") for p in picks]}] + [
        r for r in runs if r.get("date") != asof
    ]
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    HISTORY_PATH.write_text(
        json.dumps({"runs": runs[:HISTORY_KEEP]}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _recent(runs: list[dict]) -> str:
    seen = []
    for r in runs[:HISTORY_LOOKBACK]:
        seen.extend(r.get("tickers") or [])
    return ", ".join(dict.fromkeys(seen)) or "（履歴なし）"


def build_prompt(candidates: list[dict], runs: list[dict]) -> str:
    return f"""あなたは増配株投資を教える投資塾の分析担当です。
下の候補から、**増配バリュー銘柄を5銘柄**選び、理由を書いてください。

増配バリューとは「今の利回りは突出していなくても、増配が続くことで
将来の取得利回りが育つ割安株」です。今の利回りの高さだけで選ばないでください。

重視する順:
1. 増配の継続性（連続増配期数、非減配の長さ）
2. 増配余地（配当性向の低さ、利益成長）
3. 割安さ（PER・PBR）
4. 現在の利回り

直近で既に取り上げた銘柄: {_recent(runs)}
可能なら重複を避け、新しい銘柄を発掘してください。ただし優れた銘柄が
続けて選ばれること自体は問題ありません。

候補:
{chr(10).join(fmt(s) for s in candidates)}

次のJSON形式だけを ```json ブロックで返してください。

```json
{{
  "picks": [
    {{
      "code": "1234",
      "name": "銘柄名",
      "headline": "20字以内の一言。何が魅力かを端的に",
      "reason": "150字程度。連続増配の実績、配当性向から見た余地、割安さを具体的な数字を挙げて説明する",
      "watch": "60字程度。注意すべき点。業績の変調、性向の上昇余地の乏しさなど"
    }}
  ],
  "summary": "150字程度。今回の5銘柄に共通する傾向、または今の相場での位置づけ"
}}
```

注意:
- 候補リストにない銘柄を選ばないこと
- 「買い推奨」ではなく「注目に値する理由」として書くこと
- watch は必ず書くこと。良い面だけを並べないこと\n- JSON以外の文章を前後に書かないこと。文字列内に改行を入れないこと
"""

def extract_json(text: str) -> dict | None:
    m = JSON_BLOCK_RE.search(text)
    raw = m.group(1) if m else text
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def main() -> int:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        print("ANTHROPIC_API_KEY が未設定です。スキップします。", file=sys.stderr)
        return 0
    if not SCREENER_PATH.exists():
        print(f"{SCREENER_PATH} がありません。", file=sys.stderr)
        return 0

    data = json.loads(SCREENER_PATH.read_text(encoding="utf-8"))
    candidates = select_candidates(data.get("stocks", []))
    print(f"候補 {len(candidates)} 銘柄", file=sys.stderr)
    if len(candidates) < 5:
        # 候補が足りないまま無理に選ばせると、条件を満たさない銘柄が混ざる。
        print("候補が5銘柄に満たないため生成しません。", file=sys.stderr)
        return 0

    runs = _load_history()
    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": build_prompt(candidates, runs)}],
    )
    raw_text = "".join(b.text for b in resp.content if b.type == "text")
    parsed = extract_json(raw_text)
    if not parsed or not parsed.get("picks"):
        # AI生成の失敗でパイプライン全体を落とさない。既存の内容を維持して
        # 正常終了させる。ここで exit 1 を返すと後続の commit まで巻き添えになり、
        # 他のスクリプトが正しく作った出力まで反映されなくなる。
        print("JSONを解釈できませんでした。既存の内容を維持します。", file=sys.stderr)
        print("応答冒頭: " + raw_text[:300].replace(chr(10), " "), file=sys.stderr)
        return 0

    # 候補外の銘柄が混ざっていないか検証する。モデルが候補リストにない
    # 銘柄を返すことがあり、その場合は選定基準が説明できなくなる。
    allowed = {str(s.get("code")) for s in candidates}
    picks = [p for p in parsed["picks"] if str(p.get("code")) in allowed]
    dropped = len(parsed["picks"]) - len(picks)
    if dropped:
        print(f"候補外の {dropped} 銘柄を除外しました。", file=sys.stderr)
    if not picks:
        print("有効な銘柄が残りませんでした。", file=sys.stderr)
        return 0

    # 数値は元データで上書きする。モデルの転記ミスを画面に出さないため。
    by_code = {str(s.get("code")): s for s in candidates}
    for p in picks:
        s = by_code[str(p["code"])]
        st = s.get("streak") or {}
        p.update({
            "name": s.get("name"), "sector": s.get("sector"),
            "yield": s.get("yield"), "per": s.get("per"), "pbr": s.get("pbr"),
            "payout": s.get("payout"), "roe": s.get("roe"), "mcap": s.get("mcap"),
            "streak_label": st.get("label"), "headroom": s.get("headroom"),
        })

    data["ai_growth_dividend"] = {
        "picks": picks,
        "summary": parsed.get("summary", ""),
        "model": MODEL,
        "candidate_count": len(candidates),
        "criteria": (f"利回り{MIN_YIELD}%以上・PER{MAX_PER}倍以下・"
                     f"配当性向{MAX_PAYOUT}%以下・増配実績あり・増配余地◎○△"),
    }
    SCREENER_PATH.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _save_history(runs, data.get("asof", ""), picks)
    print(f"{len(picks)} 銘柄を書き出しました。", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
