"""
Telegram alerting — the "accessible from any device" interface. Telegram's
app is identical on phone, laptop, tablet, so pushing messages here means
you see trade alerts and reports wherever you are, with nothing to install
beyond Telegram itself.

Setup (no coding, ~2 minutes):
  1. In Telegram, message @BotFather -> /newbot -> follow the prompts.
     BotFather gives you a bot token (looks like 123456789:AAg...).
  2. Message your new bot anything once (so it can reply to you), then visit
     https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates in a browser and
     find your numeric "chat":{"id": ...} — that's your chat id.
  3. Put both in config.yaml under alerts: telegram_bot_token / telegram_chat_id
     (or, for the GitHub Actions automated runs, as repo secrets
     TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID instead — see README).
  4. Set alerts.telegram_enabled: true.

If disabled, missing credentials, or the network call fails, every function
here silently no-ops (prints a note) instead of crashing the pipeline —
alerting is a convenience layer, never a dependency for the pipeline to run.
"""
import urllib.request
import urllib.parse
import urllib.error
import json
import os


def _credentials(cfg: dict) -> tuple[str | None, str | None]:
    alert_cfg = cfg.get("alerts", {})
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or alert_cfg.get("telegram_bot_token") or None
    chat_id = os.environ.get("TELEGRAM_CHAT_ID") or alert_cfg.get("telegram_chat_id") or None
    return token, chat_id


def is_enabled(cfg: dict) -> bool:
    token, chat_id = _credentials(cfg)
    return bool(cfg.get("alerts", {}).get("telegram_enabled")) and bool(token) and bool(chat_id)


def send_telegram_message(text: str, cfg: dict) -> bool:
    """Sends a message via the Telegram Bot API using only the standard
    library (no extra dependency) since this is a handful of calls a day,
    not a high-throughput integration. Returns True on success, False (and
    prints why) otherwise — never raises, so a bad token can't crash a run."""
    if not cfg.get("alerts", {}).get("telegram_enabled"):
        return False

    token, chat_id = _credentials(cfg)
    if not token or not chat_id:
        print("  [telegram] enabled but token/chat_id missing; skipping alert. "
              "Set them in config.yaml or as TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_web_page_preview": "true",
    }).encode()

    try:
        req = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode())
            if not body.get("ok"):
                print(f"  [telegram] API returned an error: {body}")
                return False
            return True
    except urllib.error.URLError as e:
        print(f"  [telegram] send failed (network/credentials issue): {e}")
        return False


def format_decision_message(symbol: str, bar_ts, decision, sizing: dict, outcome: dict | None) -> str:
    status = decision.trade.upper()
    lines = [f"*{symbol}* @ `{bar_ts}` — *{status}*"]
    if decision.trade != "no_trade":
        lines.append(
            f"entry `{decision.entry}`  stop `{decision.stop_loss}`  "
            f"target `{decision.take_profit}`  R:R `{decision.risk_reward}`  "
            f"contracts `{sizing.get('contracts', 0)}`"
        )
        if outcome:
            lines.append(f"result: *{outcome['outcome']}* @ `{outcome['exit_price']}`")
    lines.append(f"_{decision.reasoning}_")
    return "\n".join(lines)


def format_summary_message(summary: dict) -> str:
    lines = ["*Session summary*"]
    lines.append(f"Decisions logged: {summary['total']}  |  Trades taken: {summary['traded']}")
    if summary["traded"] > 0:
        lines.append(f"Win rate: {summary['win_rate']:.1f}%  |  Avg confidence: {summary['avg_confidence']:.1f}")
    lines.append(f"Total paper P&L: ${summary['total_pnl']:.2f}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.append(str(Path(__file__).parent.parent))
    from config.loader import load_config

    cfg = load_config()
    if not is_enabled(cfg):
        print("Telegram is not enabled/configured. Set alerts.telegram_enabled: true and "
              "provide a token + chat_id (config.yaml or env vars) before running this test.")
    else:
        ok = send_telegram_message("Test message from the order flow system ✅", cfg)
        print("Sent OK" if ok else "Send failed — see message above.")
