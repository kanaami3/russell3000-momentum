"""Post a notification to Discord via webhook.

Reads the latest data files and builds a kind-specific embed message.
Sent to env var DISCORD_WEBHOOK_URL. Exits 0 silently if env not set
(so workflows don't fail on the notification step).

Usage:
  python batch/notify_discord.py <kind>

  kind ∈ {morning_brief, daily_jp, daily_us, value_jp, earnings}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "web" / "data"
SITE_URL = "https://russell3000-momentum.vercel.app"

# Discord embed colors
COLOR_BULL = 0x16A34A  # green
COLOR_BEAR = 0xDC2626  # red
COLOR_INFO = 0x2563EB  # blue
COLOR_GOLD = 0xCA8A04  # gold


def _load(name: str) -> dict | None:
    p = DATA_DIR / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[notify] failed to read {name}: {e}", file=sys.stderr)
        return None


def _post(payload: dict) -> int:
    url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not url:
        print("[notify] DISCORD_WEBHOOK_URL not set — skipping", file=sys.stderr)
        return 0
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Content-Type": "application/json",
                # Discord rejects requests without a recognizable User-Agent
                "User-Agent": "kana-juku-ai-navi (https://russell3000-momentum.vercel.app, 1.0)",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[notify] sent ({resp.status})", file=sys.stderr)
    except Exception as e:
        print(f"[notify] ERROR: {e}", file=sys.stderr)
        return 1
    return 0


def _trunc(s: str, n: int) -> str:
    s = s or ""
    return s if len(s) <= n else s[: n - 1] + "…"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_morning_brief() -> dict:
    """Posted ~08:00 JST after morning_jp.yml — AI daytrade brief is ready."""
    brief = _load("morning_brief_jp.json") or {}
    portfolio = _load("portfolio_jp.json") or {}

    date = brief.get("asof", "")
    picks = brief.get("ai_picks", [])[:7]
    macro = brief.get("macro_signals", [])[:3]

    equity = portfolio.get("equity", 0)
    total_pct = portfolio.get("total_pnl_pct", 0)
    open_n = len(portfolio.get("open_positions", []))

    pick_lines = []
    for p in picks:
        ticker = p.get("ticker", "")
        name = _trunc(p.get("name", ""), 14)
        typ = p.get("type", "-")
        horiz = p.get("hold_horizon", "-")
        pick_lines.append(f"• `{ticker}` **{name}** _{typ}/{horiz}_")

    macro_lines = []
    for m in macro:
        macro_lines.append(f"  {m.get('label')}: {m.get('value',0):,.2f} ({m.get('change_pct',0):+.2f}%)")

    color = COLOR_BULL if total_pct >= 0 else COLOR_BEAR
    embed = {
        "title": f"🌅 朝の AI 寄付ピック({date})",
        "description": (
            f"**評価額** ¥{equity:,} / **通算** {total_pct:+.2f}% / 持越し {open_n}銘柄\n\n"
            f"**AI ピック 7銘柄**\n" + ("\n".join(pick_lines) if pick_lines else "_（なし）_")
            + (("\n\n**マクロ環境**\n" + "\n".join(macro_lines)) if macro_lines else "")
        ),
        "color": color,
        "url": f"{SITE_URL}/#daytrade",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_daily_jp() -> dict:
    """Posted ~17:00 JST after daily_jp.yml — close + reflection."""
    portfolio = _load("portfolio_jp.json") or {}
    reflections = _load("reflections_jp.json") or {}

    date = portfolio.get("last_simulated_date", "")
    equity = portfolio.get("equity", 0)
    total_pct = portfolio.get("total_pnl_pct", 0)
    daily_eq = portfolio.get("daily_equity", [])
    today_row = next((d for d in daily_eq if d.get("date") == date), None)
    daily_yen = today_row.get("daily_pnl") if today_row else None
    daily_pct = today_row.get("daily_pnl_pct") if today_row else None

    # Today's trades
    closed = [t for t in portfolio.get("trade_history", []) if t.get("exit_date") == date]
    wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
    losses = sum(1 for t in closed if t.get("pnl", 0) < 0)
    open_n = len(portfolio.get("open_positions", []))

    trade_lines = []
    for t in closed[:6]:
        emoji = "🟢" if t.get("pnl", 0) > 0 else "🔴"
        trade_lines.append(
            f"{emoji} `{t.get('ticker')}` {_trunc(t.get('name',''),10)}: "
            f"{t.get('pnl_pct',0):+.2f}% ({t.get('outcome','-')})"
        )

    # Latest reflection (newest first)
    refl_list = reflections.get("reflections", []) if isinstance(reflections, dict) else []
    refl_text = ""
    if refl_list and refl_list[0].get("date") == date:
        refl_text = _trunc(refl_list[0].get("reflection", ""), 350)

    daily_str = (
        f"¥{daily_yen:+,} ({daily_pct:+.2f}%)" if daily_yen is not None and daily_pct is not None else "-"
    )
    color = COLOR_BULL if (daily_pct or 0) >= 0 else COLOR_BEAR

    description = (
        f"**評価額** ¥{equity:,} / **本日** {daily_str} / **通算** {total_pct:+.2f}%\n"
        f"約定 {len(closed)}件(勝ち {wins} / 負け {losses}) / 持越し {open_n}銘柄\n"
    )
    if trade_lines:
        description += "\n**本日の決済**\n" + "\n".join(trade_lines)
    if refl_text:
        description += f"\n\n**🪞 振り返り**\n_{refl_text}_"

    embed = {
        "title": f"📊 大引け振り返り({date})",
        "description": description,
        "color": color,
        "url": f"{SITE_URL}/#daytrade",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_daily_us() -> dict:
    """Posted ~07:30 JST after daily_us.yml — US close + indices + JP swing picks."""
    indices = _load("indices.json") or {}
    swing_us = _load("swing_picks_us.json") or {}

    date = indices.get("asof", "")
    idx_lines = []
    for ix in indices.get("indices", []):
        cur = ix.get("current", {})
        ind = ix.get("indicators", {})
        emoji = "🟢" if cur.get("change_pct", 0) >= 0 else "🔴"
        trend = ind.get("trend", "-")
        idx_lines.append(
            f"{emoji} **{ix.get('label')}** {cur.get('value',0):,.2f} "
            f"({cur.get('change_pct',0):+.2f}%) _{trend}_"
        )

    swing_lines = []
    for p in (swing_us.get("picks") or [])[:5]:
        swing_lines.append(f"• `{p.get('ticker')}` {_trunc(p.get('name',''),20)}")

    description = "**指数モニター**\n" + ("\n".join(idx_lines) if idx_lines else "_(なし)_")
    if swing_lines:
        description += "\n\n**US スウィングピック Top5**\n" + "\n".join(swing_lines)

    embed = {
        "title": f"🇺🇸 米国引け & 指数更新({date})",
        "description": description,
        "color": COLOR_INFO,
        "url": f"{SITE_URL}/#indices",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_value_jp() -> dict:
    """Posted ~21:00 JST after weekly_value_jp.yml (now daily weekdays)."""
    v = _load("value_jp.json") or {}
    date = v.get("asof", "")
    picks = v.get("ai_picks", [])[:5]
    overall = v.get("overall_commentary", "")

    pick_lines = []
    for p in picks:
        appeal = _trunc(p.get("appeal", ""), 80)
        pick_lines.append(
            f"• `{p.get('ticker')}` **{_trunc(p.get('name',''),12)}**\n  _{appeal}_"
        )

    description = (
        f"対象 {v.get('scored_count',0)}社 / フィルタ通過 {v.get('filtered_count',0)}社\n\n"
        f"**AI 厳選ピック Top5**\n" + ("\n".join(pick_lines) if pick_lines else "_(なし)_")
    )
    if overall:
        description += f"\n\n**塾長コメンタリー**\n_{_trunc(overall, 250)}_"

    embed = {
        "title": f"💎 バリュー投資スクリーン更新({date})",
        "description": description,
        "color": COLOR_GOLD,
        "url": f"{SITE_URL}/#value",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_earnings() -> dict:
    """Posted after weekly_earnings.yml."""
    e = _load("earnings_analysis.json") or {}
    date = e.get("asof", "")
    items = e.get("analyses", []) or e.get("items", [])

    lines = []
    for it in items[:6]:
        ticker = it.get("ticker", "")
        name = _trunc(it.get("name", ""), 16)
        verdict = it.get("verdict") or it.get("rating") or "-"
        lines.append(f"• `{ticker}` **{name}** — _{verdict}_")

    description = (
        f"今週分析した銘柄 {len(items)}件\n\n"
        + ("\n".join(lines) if lines else "_(なし)_")
    )
    embed = {
        "title": f"📈 決算分析更新({date})",
        "description": description,
        "color": COLOR_INFO,
        "url": f"{SITE_URL}/#earnings",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

BUILDERS = {
    "morning_brief": build_morning_brief,
    "daily_jp": build_daily_jp,
    "daily_us": build_daily_us,
    "value_jp": build_value_jp,
    "earnings": build_earnings,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in BUILDERS:
        print(f"usage: notify_discord.py {{{','.join(BUILDERS)}}}", file=sys.stderr)
        return 1
    kind = sys.argv[1]
    payload = BUILDERS[kind]()
    return _post(payload)


if __name__ == "__main__":
    sys.exit(main())
