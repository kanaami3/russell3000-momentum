"""Post a Discord notification ONLY when something significant happens.

Reads the latest data files and decides whether to fire. If conditions
are not met, exits silently with status 0 (no message sent).

Usage:
  python batch/notify_discord.py <kind>

  kind ∈ {
    market_alert  – fires only when N225 or NDX daily change ≥ ±2%
    value_alert   – fires only when a NEW ticker enters AI value picks
    earnings      – posts the weekly earnings update (always)
  }

Notification state (which value picks we've already announced) is kept
in data/notify_state.json — committed alongside the data files.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "web" / "data"
STATE_PATH = REPO_ROOT / "data" / "notify_state.json"
SITE_URL = "https://russell3000-momentum.vercel.app"

# Thresholds
MARKET_THRESHOLD_PCT = 2.0        # N225/NDX daily change to fire alert
VALUE_MIN_NEW_PICKS = 1           # at least N new tickers in AI picks to fire

# Discord embed colors
COLOR_BULL = 0x16A34A  # green
COLOR_BEAR = 0xDC2626  # red
COLOR_ALERT = 0xF59E0B  # amber
COLOR_GOLD = 0xCA8A04  # gold
COLOR_INFO = 0x2563EB  # blue


def _load(name: str) -> dict | None:
    p = DATA_DIR / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[notify] failed to read {name}: {e}", file=sys.stderr)
        return None


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


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
# Conditional builders — return None if nothing significant to send
# ---------------------------------------------------------------------------

def build_market_alert() -> dict | None:
    """Fire only when N225 or NDX daily change ≥ ±2%."""
    indices = _load("indices.json") or {}
    big = []
    for ix in indices.get("indices", []):
        if ix.get("id") not in ("N225", "NDX"):
            continue
        cur = ix.get("current", {})
        cp = cur.get("change_pct", 0) or 0
        if abs(cp) >= MARKET_THRESHOLD_PCT:
            big.append(ix)

    if not big:
        print(
            f"[notify] no market alert — all moves under ±{MARKET_THRESHOLD_PCT}%",
            file=sys.stderr,
        )
        return None

    # Build embed: list big movers + show all indices as context
    date = indices.get("asof", "")
    lines = []
    for ix in indices.get("indices", []):
        cur = ix.get("current", {})
        ind = ix.get("indicators", {})
        cp = cur.get("change_pct", 0) or 0
        emoji = "🔴" if cp < 0 else "🟢"
        flag = " 🚨" if any(b["id"] == ix["id"] for b in big) else ""
        trend = ind.get("trend", "-")
        lines.append(
            f"{emoji} **{ix.get('label')}** {cur.get('value', 0):,.2f} "
            f"({cp:+.2f}%){flag} _{trend}_"
        )

    # Direction of the biggest move sets the color
    biggest = max(big, key=lambda x: abs(x["current"].get("change_pct", 0)))
    color = COLOR_BEAR if biggest["current"].get("change_pct", 0) < 0 else COLOR_BULL

    embed = {
        "title": f"🚨 マーケット急変アラート({date})",
        "description": (
            f"日経 / NDX が ±{MARKET_THRESHOLD_PCT}% 以上動いたため通知しています。\n\n"
            + "\n".join(lines)
        ),
        "color": color,
        "url": f"{SITE_URL}/#indices",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_value_alert() -> dict | None:
    """Fire only when a NEW ticker enters AI value picks vs previous state."""
    v = _load("value_jp.json") or {}
    picks = v.get("ai_picks", [])
    current_tickers = [p.get("ticker") for p in picks if p.get("ticker")]

    state = _load_state()
    seen = set(state.get("value_picks", []))
    new_ones = [t for t in current_tickers if t not in seen]

    # Always persist the latest pick set so future diffs are correct
    state["value_picks"] = current_tickers
    _save_state(state)

    if len(new_ones) < VALUE_MIN_NEW_PICKS:
        print(
            f"[notify] no value alert — {len(new_ones)} new picks (need ≥{VALUE_MIN_NEW_PICKS})",
            file=sys.stderr,
        )
        return None

    # First-ever run: don't spam — mark baseline only
    if not seen:
        print("[notify] first-time baseline — state saved, no notification", file=sys.stderr)
        return None

    new_details = [p for p in picks if p.get("ticker") in new_ones]
    date = v.get("asof", "")

    lines = []
    for p in new_details:
        appeal = _trunc(p.get("appeal", ""), 100)
        lines.append(
            f"• `{p.get('ticker')}` **{_trunc(p.get('name', ''), 14)}**\n  _{appeal}_"
        )

    embed = {
        "title": f"💎 新規バリュー銘柄発見({date})",
        "description": (
            f"AI が新たに **{len(new_ones)}銘柄** をピックしました(前回未掲載)。\n\n"
            + "\n".join(lines)
        ),
        "color": COLOR_GOLD,
        "url": f"{SITE_URL}/#value",
        "footer": {"text": "かな塾長秘書AI投資ナビ"},
    }
    return {"username": "塾長秘書AI", "embeds": [embed]}


def build_earnings() -> dict:
    """Weekly earnings update — always posts (週1で頻度低いため)."""
    e = _load("earnings_analysis.json") or {}
    # Fallback: separate JP/US files
    if not e:
        us = _load("earnings_us.json") or {}
        jp = _load("earnings_jp.json") or {}
        items = (us.get("analyses") or us.get("items") or []) + \
                (jp.get("analyses") or jp.get("items") or [])
        date = us.get("asof") or jp.get("asof") or ""
    else:
        items = e.get("analyses") or e.get("items") or []
        date = e.get("asof", "")

    lines = []
    for it in items[:8]:
        ticker = it.get("ticker", "")
        name = _trunc(it.get("name", ""), 16)
        verdict = it.get("verdict") or it.get("rating") or "-"
        lines.append(f"• `{ticker}` **{name}** — _{verdict}_")

    description = (
        f"今週分析した銘柄 {len(items)}件\n\n"
        + ("\n".join(lines) if lines else "_(分析対象なし)_")
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
    "market_alert": build_market_alert,
    "value_alert":  build_value_alert,
    "earnings":     build_earnings,
}


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] not in BUILDERS:
        print(f"usage: notify_discord.py {{{','.join(BUILDERS)}}}", file=sys.stderr)
        return 1
    kind = sys.argv[1]
    payload = BUILDERS[kind]()
    if payload is None:
        return 0  # silent no-op when nothing significant
    return _post(payload)


if __name__ == "__main__":
    sys.exit(main())
