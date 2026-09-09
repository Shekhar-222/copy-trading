"""
Pushes a P&L + trade-log digest to Telegram every 60s. Activity-triggered, not clock-scheduled:
the loop only starts the first time an order is actually placed (see ensure_running, called from
trade_log.log_trade_event - the one choke point every broker's order path already runs through),
so nothing fires while the app is just sitting idle with no trades happening.

Uses Telegram's plain Bot API over HTTP (requests, already a dependency) rather than a
telegram-bot library - sendMessage is a single JSON POST, not worth a new dependency for.
"""
import datetime
import os
import threading
import time

import requests

import models
from database import SessionLocal
from pnl import compute_pnl

_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Comma-separated so the same digest can go to more than one place at once - e.g. your own DM
# plus a broadcast channel for an audience. A channel's id can be either its numeric id or, for
# a public channel, "@channelusername" - Telegram's sendMessage accepts both.
_CHAT_IDS = [c.strip() for c in os.getenv("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
_INTERVAL_SECONDS = 60
# NSE/BSE cash+F&O close - matches when this app's own market-protection order placement stops
# making sense too. Manual offset (not zoneinfo) to match the rest of the codebase's IST handling
# (see trade_log.today_ist_start_utc).
_MARKET_CLOSE_IST = datetime.time(15, 30)

_lock = threading.Lock()
_running = False
_last_log_id_sent = 0


def _past_market_close() -> bool:
    ist_now = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    return ist_now.time() >= _MARKET_CLOSE_IST


def _send(text: str) -> None:
    if not _BOT_TOKEN or not _CHAT_IDS:
        return
    for chat_id in _CHAT_IDS:
        try:
            requests.post(
                f"https://api.telegram.org/bot{_BOT_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
                timeout=10,
            )
        except Exception:  # noqa: BLE001 - a Telegram outage must never affect trading, and one
            pass          # recipient failing (e.g. bot removed from the channel) shouldn't stop the rest


_SEPARATOR = "—" * 16


def _format_digest(db) -> str:
    global _last_log_id_sent

    pnl = compute_pnl(db)
    accounts = {a.id: a for a in db.query(models.Account).all()}

    # "Available fund" is account.capital - each broker's own margin figure, refreshed on
    # login/manual refresh (see main.py's _refresh_capital) - not a fresh call here, since
    # polling every broker's margin API every 60s would add real load for no benefit (and Angel
    # One in particular is already known to rate-limit under much lighter polling than that).
    lines = ["<b>P&L Update</b>", _SEPARATOR]
    for row in pnl["accounts"]:
        acc = accounts.get(row["id"])
        label = acc.label if acc else f"#{row['id']}"
        value = f"{row['pnl']:.2f}" if row["pnl"] is not None else "-"
        fund = f"{acc.capital:.2f}" if acc and acc.capital is not None else "-"
        lines.append(f"{label}: {value}")
        lines.append(f"Available fund: {fund}")
        lines.append(_SEPARATOR)
    lines.append(f"<b>Total: {pnl['total']:.2f}</b>")

    new_logs = (
        db.query(models.TradeLog)
        .filter(models.TradeLog.id > _last_log_id_sent)
        .order_by(models.TradeLog.id.asc())
        .all()
    )
    if new_logs:
        lines.append("")
        lines.append("<b>Trades</b>")
        for log in new_logs:
            child = accounts[log.child_account_id].label if log.child_account_id in accounts else "?"
            lines.append(
                f"{log.status}: {child} {log.transaction_type} {log.replicated_quantity} "
                f"{log.tradingsymbol} - {log.message}"
            )
        _last_log_id_sent = new_logs[-1].id

    return "\n".join(lines)


def _loop() -> None:
    global _running
    while True:
        if _past_market_close():
            _send("Market closed for the day - pausing updates.")
            with _lock:
                _running = False
            return  # thread ends here; ensure_running() re-arms a fresh one on tomorrow's first order
        db = SessionLocal()
        try:
            _send(_format_digest(db))
        except Exception:  # noqa: BLE001 - never let a bad cycle kill the loop
            pass
        finally:
            db.close()
        time.sleep(_INTERVAL_SECONDS)


def ensure_running(trigger_log_id: int) -> None:
    """Starts the digest loop on first order activity. trigger_log_id is the TradeLog row that
    triggered this call - seeded as the baseline (minus one) so that very first trade is included
    in the first digest, rather than only trades placed after the loop happens to start."""
    global _running, _last_log_id_sent
    with _lock:
        if _running:
            return
        _running = True
        _last_log_id_sent = trigger_log_id - 1
    threading.Thread(target=_loop, daemon=True, name="telegram-digest").start()
