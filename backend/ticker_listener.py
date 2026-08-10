"""
Listens to the master account's order updates in real time via KiteTicker and
triggers replication onto child accounts as soon as an order completes.

Also carries a shared live-P&L tick subscription on the same connection: market data
isn't user-scoped, so one master's authenticated KiteTicker can stream LTPs for every
account's open positions (master and children alike). See _refresh_and_resubscribe /
_on_ticks below.
"""
import datetime
import logging
import threading
import time

from kiteconnect import KiteTicker
from twisted.internet import reactor

from database import SessionLocal
import models
import crypto_utils
from kite_auth import get_kite_client
from replication_engine import replicate_order

logger = logging.getLogger(__name__)

_active_tickers = {}  # master_account_id -> KiteTicker instance

# ---------------------------- Live tick-driven P&L ----------------------------
# anchor-and-delta: each REST positions() refresh records anchor_pnl/anchor_ltp (Kite's own
# pnl and the last_price it was computed from, both linear in last price), then every tick
# just adjusts that anchor by (tick_ltp - anchor_ltp) * quantity - exact as long as quantity/
# realized haven't changed since the last anchor refresh, no multiplier/average-price guessing.
_cache_lock = threading.Lock()
_position_cache = {}  # account_id -> {"role": str, "positions": [{instrument_token, quantity, anchor_pnl, anchor_ltp}]}
_ltp_cache = {}  # instrument_token -> last live_price seen on a tick

_tick_source_id = None  # master_account_id whose kws currently owns the shared tick subscription
_refresh_thread_started = False
_last_pnl_broadcast = 0.0
_PNL_BROADCAST_THROTTLE_SEC = 0.3
_POSITION_REFRESH_INTERVAL_SEC = 12


def _token_is_fresh(acc) -> bool:
    if not acc.token_generated_at or not acc.access_token_enc:
        return False
    return acc.token_generated_at.date() == datetime.datetime.utcnow().date()


def _compute_pnl_payload() -> dict:
    """Must be called with _cache_lock held."""
    out = []
    total = 0.0
    for account_id, entry in _position_cache.items():
        pnl = 0.0
        for p in entry["positions"]:
            ltp = _ltp_cache.get(p["instrument_token"], p["anchor_ltp"])
            pnl += p["anchor_pnl"] + (ltp - p["anchor_ltp"]) * p["quantity"]
        pnl = round(pnl, 2)
        out.append({"id": account_id, "role": entry["role"], "pnl": pnl})
        total += pnl
    return {"event": "pnl", "accounts": out, "total": round(total, 2)}


def _refresh_and_resubscribe(broadcast=None):
    """REST-refreshes every Zerodha account's open positions, re-anchors the live-P&L cache,
    and updates the shared tick subscription to match. Safe to call from any thread - the DB/
    REST work runs inline (on the caller's thread), but the actual subscribe/set_mode/
    unsubscribe calls are handed to Twisted's reactor thread via callFromThread, since
    KiteTicker's websocket send isn't safe to call from an arbitrary thread."""
    if _tick_source_id is None:
        return
    kws = _active_tickers.get(_tick_source_id)
    if kws is None:
        return

    db = SessionLocal()
    try:
        accounts = db.query(models.Account).filter(models.Account.broker == "zerodha").all()
        new_cache = {}
        for acc in accounts:
            if not _token_is_fresh(acc):
                continue
            try:
                kite = get_kite_client(
                    crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc)
                )
                net_positions = kite.positions().get("net", [])
            except Exception:
                logger.warning("pnl tick refresh: failed to fetch positions for account %s", acc.id, exc_info=True)
                continue

            positions = []
            for p in net_positions:
                token = p.get("instrument_token")
                last_price = p.get("last_price")
                pnl = p.get("pnl")
                if token is None or last_price is None or pnl is None:
                    continue  # no instrument_token to key ticks on - falls back to REST-only P&L
                positions.append(
                    {
                        "instrument_token": token,
                        "quantity": p.get("quantity", 0),
                        "anchor_pnl": pnl,
                        "anchor_ltp": last_price,
                    }
                )
            new_cache[acc.id] = {"role": acc.role, "positions": positions}
    finally:
        db.close()

    desired_tokens = {
        p["instrument_token"] for entry in new_cache.values() for p in entry["positions"] if p["quantity"] != 0
    }

    global _position_cache
    with _cache_lock:
        _position_cache = new_cache
        for token in list(_ltp_cache.keys()):
            if token not in desired_tokens:
                _ltp_cache.pop(token, None)  # position closed - stop carrying its stale LTP
        payload = _compute_pnl_payload()

    current_tokens = set(kws.subscribed_tokens.keys())
    to_subscribe = list(desired_tokens - current_tokens)
    to_unsubscribe = list(current_tokens - desired_tokens)

    def _apply_subscriptions():
        try:
            if to_unsubscribe:
                kws.unsubscribe(to_unsubscribe)
            if to_subscribe:
                kws.subscribe(to_subscribe)
            if desired_tokens:
                kws.set_mode(kws.MODE_LTP, list(desired_tokens))
        except Exception:
            logger.warning("pnl tick refresh: failed to update tick subscriptions", exc_info=True)

    reactor.callFromThread(_apply_subscriptions)

    if broadcast:
        broadcast(payload)


def _on_ticks(broadcast):
    def handler(ws, ticks):
        global _last_pnl_broadcast
        with _cache_lock:
            changed = False
            for t in ticks:
                token = t.get("instrument_token")
                ltp = t.get("last_price")
                if token is None or ltp is None:
                    continue
                if _ltp_cache.get(token) != ltp:
                    _ltp_cache[token] = ltp
                    changed = True
            if not changed:
                return
            payload = _compute_pnl_payload()

        now = time.monotonic()
        if now - _last_pnl_broadcast < _PNL_BROADCAST_THROTTLE_SEC:
            return  # coalesce a tick burst - another tick will land after the window and catch up
        _last_pnl_broadcast = now
        if broadcast:
            broadcast(payload)

    return handler


def _start_refresh_loop():
    global _refresh_thread_started
    if _refresh_thread_started:
        return
    _refresh_thread_started = True

    def loop():
        while True:
            time.sleep(_POSITION_REFRESH_INTERVAL_SEC)
            try:
                _refresh_and_resubscribe(broadcast=_last_broadcast_fn[0])
            except Exception:
                logger.warning("pnl periodic refresh failed", exc_info=True)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()


_last_broadcast_fn = [None]  # holds the current broadcast callable for the periodic refresh loop


# ---------------------------- Master order-update listener ----------------------------
def start_master_listener(master_account_id: int, broadcast=None):
    """Starts (or restarts) the order-update WebSocket for a given master account."""
    stop_master_listener(master_account_id)

    db = SessionLocal()
    master = db.query(models.Account).get(master_account_id)
    if not master:
        db.close()
        raise ValueError("Master account not found")

    api_key = crypto_utils.decrypt(master.api_key_enc)
    access_token = crypto_utils.decrypt(master.access_token_enc)
    db.close()

    kws = KiteTicker(api_key, access_token)

    def on_order_update(ws, data):
        session = SessionLocal()
        try:
            m = session.query(models.Account).get(master_account_id)
            # When the master's app is shared with family-linked child accounts (same api_key,
            # multiple permitted client IDs for the static-IP setup - see kite_auth.py), Kite
            # delivers order-update events for EVERY permitted client on this one connection,
            # not just the master's own orders. Without this check, a child's own mirrored order
            # gets misread as a new master fill and replicated again - a self-feeding loop that
            # placed ~20 duplicate orders in production. Only ever replicate the master's own.
            if not data.get("user_id") or data["user_id"] == m.client_id:
                replicate_order(session, m, data, broadcast=broadcast)
        finally:
            session.close()
        # A fill may have opened/closed a position - re-anchor and resubscribe right away
        # instead of waiting for the next periodic refresh.
        _refresh_and_resubscribe(broadcast=broadcast)

    def on_connect(ws, response):
        global _tick_source_id
        if _tick_source_id is None:
            _tick_source_id = master_account_id
        _last_broadcast_fn[0] = broadcast
        _start_refresh_loop()
        if _tick_source_id == master_account_id:
            _refresh_and_resubscribe(broadcast=broadcast)
        if broadcast:
            broadcast({"event": "master_connected", "master_account_id": master_account_id})

    def on_close(ws, code, reason):
        global _tick_source_id
        if _tick_source_id == master_account_id:
            _tick_source_id = None
        if broadcast:
            broadcast({"event": "master_disconnected", "master_account_id": master_account_id, "reason": reason})

    def on_error(ws, code, reason):
        if broadcast:
            broadcast({"event": "master_error", "master_account_id": master_account_id, "reason": reason})

    kws.on_order_update = on_order_update
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error
    kws.on_ticks = _on_ticks(broadcast)

    thread = threading.Thread(target=kws.connect, kwargs={"threaded": False}, daemon=True)
    thread.start()

    _active_tickers[master_account_id] = kws


def stop_master_listener(master_account_id: int):
    global _tick_source_id
    kws = _active_tickers.pop(master_account_id, None)
    if kws:
        try:
            kws.close()
        except Exception:
            pass
    if _tick_source_id == master_account_id:
        _tick_source_id = None


def is_running(master_account_id: int) -> bool:
    return master_account_id in _active_tickers
