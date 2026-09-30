"""
週次ブリーフ自動生成 — Claude API
==================================
market_data.json の統計をClaudeに渡し、週次総括ページ用の
brief.json(見出し・結論・シナリオ・イベント・リスク)を生成する。

使い方:
    pip install requests
    export ANTHROPIC_API_KEY=sk-ant-...
    python pipeline/generate_brief.py
"""

import json
import os
from datetime import datetime
from pathlib import Path

import requests

DATA = Path(__file__).resolve().parent.parent.parent / "web" / "data" / "sector"
MARKET = DATA / "market_data.json"
OUT = DATA / "brief.json"

SCHEMA_EXAMPLE = {
    "issue": "2026-06-29週 / テーマの短い要約",
    "headline": ["前段テキスト。", "強調部分", "後段テキスト。"],
    "conclusion_title": "結論: 一言で",
    "conclusions": [
        {"tag": "需要", "cls": "demand", "text": "…"},
        {"tag": "株価", "cls": "price", "text": "…"},
        {"tag": "ローテ", "cls": "rot", "text": "…"},
    ],
    "table_note": "騰落率テーブルの読み方のポイント(<b>強調可</b>)",
    "scenarios": [
        {"k": "基本線", "cls": "base", "v": "短い見出し", "d": "説明"},
        {"k": "強気", "cls": "bull", "v": "短い見出し", "d": "説明"},
        {"k": "弱気", "cls": "bear", "v": "短い見出し", "d": "説明"},
    ],
    "event_title": "イベント: 来週の焦点を一言で",
    "events": [{"date": "7/1", "text": "<b>イベント名</b>: 意味"}],
    "risk": {"head": "最大リスクの見出し", "body": "説明(<b>強調可</b>)"},
}


def summarize_market() -> str:
    """market_data.json をClaudeに渡すためのコンパクトな統計に要約する。"""
    m = json.loads(MARKET.read_text(encoding="utf-8"))
    lines = [f"データ基準日: {m['dates'][-1]} (対 {m['benchmark']})"]

    lines.append("\n[業種別 相対力の変化(TOPIX比)]")
    for s in m["sectors"]:
        rs = s["rs"]
        if len(rs) < 14:
            continue
        w1 = (rs[-1] / rs[-2] - 1) * 100
        w4 = (rs[-1] / rs[-5] - 1) * 100
        w13 = (rs[-1] / rs[-14] - 1) * 100
        lines.append(f"  {s['name']}: 1週 {w1:+.2f}% / 4週 {w4:+.2f}% / 13週 {w13:+.2f}%")

    if m.get("brief_table"):
        lines.append("\n[直近1週の騰落率(等ウェイト/時価総額ウェイト)]")
        for r in m["brief_table"]:
            cw = f"{r['cw']:+.2f}%" if r["cw"] is not None else "n/a"
            lines.append(f"  {r['name']}: EW {r['ew']:+.2f}% / CW {cw}")
    return "\n".join(lines)


def generate(api_key: str) -> dict:
    stats = summarize_market()
    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""あなたは機関投資家向けの週次マーケットブリーフを書くストラテジストです。
以下の実データ統計に基づき、来週の展望ページの内容をJSONで生成してください。

{stats}

要件:
- 相対力の変化から「どの業種からどの業種へ資金が動いているか」を軸に書く
- 等ウェイトと時価総額ウェイトの差(大型株への集中/分散)に言及する
- 断定を避け、確認ポイントを示す形で書く。投資助言ではなく状況整理として書く
- イベントは翌週の実際の日本・米国の主要経済指標/決算を挙げる(今日は{today})
- headlineは3要素合計で全角50字以内の簡潔な1文にする(詳細はconclusionsに書く)
- 次のJSONスキーマに厳密に従い、JSONのみを出力(前置き・コードブロック記号なし):
{json.dumps(SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}"""

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": "claude-haiku-4-5-20251001",
            "max_tokens": 4000,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    if not resp.ok: raise ValueError(f"API {resp.status_code}: {resp.text[:1000]}")
    body = resp.json()
    if body.get("stop_reason") == "max_tokens":
        raise ValueError("応答がmax_tokensで打ち切られました(JSON不完全)")
    text = "".join(b.get("text", "") for b in body["content"])
    text = text.replace("```json", "").replace("```", "").strip()
    brief = json.loads(text)
    brief["generated_at"] = datetime.now().isoformat(timespec="seconds")
    return brief


def main():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise SystemExit("環境変数 ANTHROPIC_API_KEY を設定してください")

    # ブリーフ生成が失敗しても、データ更新パイプライン全体は止めない。
    # 既存の brief.json を残して正常終了する(データのcommitを守るため)。
    try:
        brief = generate(api_key)
    except Exception as e:  # noqa: BLE001
        print(f"警告: ブリーフ生成に失敗しました({e})。既存のbrief.jsonを維持します。")
        return

    OUT.write_text(json.dumps(brief, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完了: {OUT}")


if __name__ == "__main__":
    main()
