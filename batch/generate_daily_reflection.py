"""Generate a daily reflection on the AI virtual portfolio's trades.

Reads portfolio_jp.json (latest trades + open positions) and
morning_brief_jp.json (the AI rationale that drove today's picks),
then asks Claude to write a 本日の振り返り in Japanese covering:

  - 本日の結果サマリー (損益・約定数・勝率)
  - 👍 良かった点 (うまくいったトレード・判断)
  - 🪞 反省点 (損切り発動・スキップ理由・改善余地)
  - 🎯 明日への学び・着眼点

Appends to web/data/reflections_jp.json (history of daily entries).

Requires env var: ANTHROPIC_API_KEY
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import anthropic
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
PORTFOLIO_PATH = REPO_ROOT / "web" / "data" / "portfolio_jp.json"
BRIEF_PATH = REPO_ROOT / "web" / "data" / "morning_brief_jp.json"
REFLECTIONS_PATH = REPO_ROOT / "web" / "data" / "reflections_jp.json"

MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1400
MAX_HISTORY = 60     # 直近 60 営業日分まで保持


def todays_activity(portfolio: dict, today: str) -> dict:
    """Summarize what happened today: closed trades + new open positions."""
    closed = [t for t in portfolio.get("trade_history", []) if t.get("exit_date") == today]
    opened_today = [
        p for p in portfolio.get("open_positions", []) if p.get("entry_date") == today
    ]
    # Also note open positions opened before today (carried)
    carried = [
        p for p in portfolio.get("open_positions", []) if p.get("entry_date") != today
    ]

    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    realized = sum(t["pnl"] for t in closed)
    unrealized_today = sum(p.get("unrealized_pnl", 0) for p in opened_today)
    win_rate = (len(wins) / len(closed) * 100) if closed else 0.0

    daily_eq = portfolio.get("daily_equity", [])
    today_row = next((d for d in daily_eq if d["date"] == today), None)

    return {
        "closed": closed,
        "opened_today": opened_today,
        "carried": carried,
        "wins_count": len(wins),
        "losses_count": len(losses),
        "realized_pnl": realized,
        "unrealized_today_pnl": unrealized_today,
        "win_rate_today": round(win_rate, 1),
        "equity": today_row.get("equity") if today_row else portfolio.get("equity"),
        "daily_pnl_pct": today_row.get("daily_pnl_pct") if today_row else None,
        "daily_pnl_yen": today_row.get("daily_pnl") if today_row else None,
        "total_pnl_pct": portfolio.get("total_pnl_pct", 0),
    }


def build_prompt(today: str, activity: dict, ai_picks: list[dict], macro_signals: list[dict]) -> str:
    def fmt_closed(t):
        return (
            f"- {t['ticker']} {t['name']}: {t['shares']}株 "
            f"entry ¥{t['entry_price']:,.0f} → exit ¥{t['exit_price']:,.0f}  "
            f"P&L ¥{t['pnl']:+,} ({t['pnl_pct']:+.2f}%)  [結果: {t['outcome']}, タイプ: {t.get('ai_pick_type','-')}]"
        )

    def fmt_open(p):
        return (
            f"- {p['ticker']} {p['name']}: {p['shares']}株 "
            f"取得 ¥{p['entry_price']:,.0f}  現値 ¥{p.get('current_price',0):,.0f}  "
            f"含み {p.get('unrealized_pnl',0):+,}円 ({p.get('unrealized_pnl_pct',0):+.2f}%)  "
            f"損切 ¥{p.get('stop_price',0):,.0f} / target ¥{p.get('target_next',0):,.0f}"
        )

    def fmt_pick_rationale(p):
        rationale = (p.get("rationale") or "")[:200]
        return (
            f"- {p['ticker']} {p['name']} [{p.get('type','-')} / {p.get('hold_horizon','-')}]: "
            f"\n    狙い: {rationale}"
            f"\n    寄付: {p.get('entry','-')[:120]}"
        )

    def fmt_macro(m):
        return f"  {m['label']}: {m['value']:,.2f} ({m['change_pct']:+.2f}%)"

    closed_str = "\n".join(fmt_closed(t) for t in activity["closed"]) or "  (なし)"
    opened_str = "\n".join(fmt_open(p) for p in activity["opened_today"]) or "  (なし)"
    carried_str = "\n".join(fmt_open(p) for p in activity["carried"]) or "  (なし)"
    rationale_str = "\n".join(fmt_pick_rationale(p) for p in ai_picks[:7])
    macro_str = "\n".join(fmt_macro(m) for m in macro_signals[:6])

    return f"""あなたは AI 投資シミュレーションの **本日の振り返り** を書く塾長秘書です。
仮想運用ポートフォリオ(元本 ¥1000万)が本日 {today} に執行したトレードを振り返り、**300〜500字の日本語で本日の反省と学びを書いてください**。

【書き方】
- 自然な段落 + 末尾に箇条書きでまとめ
- 「以下は仮想運用の観察記録です」のトーン
- 投資勧誘・確約・断定的な「次は買え/売れ」は禁止
- 見出し記号(##など)は使わず、自然な日本語で

【含めるべき要素】
1. 本日の損益サマリーを 1〜2文で
2. **👍 良かった点**(箇条書き 1〜3項目)
   - target に到達した銘柄、シナリオ通りに動いた点、よく設計されていた損切ライン等
3. **🪞 反省点**(箇条書き 1〜3項目)
   - 損切り発動した銘柄の事後分析(エントリー判断・損切ライン位置)
   - スキップされた銘柄がなぜ寄り付き後に伸びたか(分析できる範囲で)
4. **🎯 明日への学び**(2〜3文)
   - 改善の方向性、注視するシグナル、AIピック側で見直すべきこと

---

【本日 {today} の実績】
評価額: ¥{activity['equity']:,}  日次損益: {activity.get('daily_pnl_yen') and f"¥{activity['daily_pnl_yen']:+,}" or '-'} ({activity.get('daily_pnl_pct') and f"{activity['daily_pnl_pct']:+.2f}%" or '-'})
通算: {activity['total_pnl_pct']:+.2f}%
本日約定: {len(activity['closed'])}件(勝ち {activity['wins_count']} / 負け {activity['losses_count']} = 勝率 {activity['win_rate_today']}%)
実現損益: ¥{activity['realized_pnl']:+,}
本日新規買付け & 持越し中:
{opened_str}
前日以前から保有中:
{carried_str}

【本日決済された取引】
{closed_str}

【朝の AI ピック銘柄 7件(各銘柄の狙い)】
{rationale_str}

【夜間マクロ環境(寄付時点)】
{macro_str}
"""


def main() -> int:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set", file=sys.stderr)
        return 1
    if not PORTFOLIO_PATH.exists():
        print(f"ERROR: {PORTFOLIO_PATH} not found", file=sys.stderr)
        return 1

    portfolio = json.loads(PORTFOLIO_PATH.read_text(encoding="utf-8"))
    today = portfolio.get("last_simulated_date")
    if not today:
        print("ERROR: portfolio has no last_simulated_date", file=sys.stderr)
        return 1

    # If portfolio's last_simulated_date is the start date with no trades, skip
    activity = todays_activity(portfolio, today)
    if not activity["closed"] and not activity["opened_today"]:
        print(f"[REFLECTION] No trades on {today}; writing 'no activity' note.", file=sys.stderr)

    # Load morning brief for AI pick rationales + macro signals
    brief = {}
    if BRIEF_PATH.exists():
        brief = json.loads(BRIEF_PATH.read_text(encoding="utf-8"))
    ai_picks = brief.get("ai_picks") or []
    macro_signals = brief.get("macro_signals") or []

    client = anthropic.Anthropic(api_key=api_key)
    prompt = build_prompt(today, activity, ai_picks, macro_signals)
    print(f"[REFLECTION] Calling Claude for {today}...", file=sys.stderr)

    try:
        resp = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
    except Exception as e:
        print(f"[REFLECTION] Claude error: {e}", file=sys.stderr)
        return 1

    entry = {
        "date": today,
        "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
        "reflection": text,
        "stats": {
            "equity": activity["equity"],
            "daily_pnl_yen": activity.get("daily_pnl_yen"),
            "daily_pnl_pct": activity.get("daily_pnl_pct"),
            "total_pnl_pct": activity["total_pnl_pct"],
            "closed_count": len(activity["closed"]),
            "wins": activity["wins_count"],
            "losses": activity["losses_count"],
            "win_rate_today": activity["win_rate_today"],
            "realized_pnl": activity["realized_pnl"],
            "open_positions": len(portfolio.get("open_positions", [])),
        },
    }

    # Load existing history, replace entry for same date if any, prepend
    history: list[dict] = []
    if REFLECTIONS_PATH.exists():
        try:
            history = json.loads(REFLECTIONS_PATH.read_text(encoding="utf-8")).get("reflections", [])
        except Exception:
            history = []
    history = [h for h in history if h.get("date") != today]  # de-dup same date
    history.insert(0, entry)                                   # newest first
    history = history[:MAX_HISTORY]

    REFLECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REFLECTIONS_PATH.write_text(
        json.dumps({
            "asof": today,
            "generated_at_jst": pd.Timestamp.now(tz="Asia/Tokyo").isoformat(),
            "reflections": history,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[REFLECTION] Wrote {REFLECTIONS_PATH} ({len(history)} entries). "
          f"Tokens: in={resp.usage.input_tokens} out={resp.usage.output_tokens}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
