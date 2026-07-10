"""
Listens to the master account's order updates in real time via KiteTicker and
triggers replication onto child accounts as soon as an order completes.
"""
import threading
from kiteconnect import KiteTicker
from database import SessionLocal
import models
import crypto_utils
from replication_engine import replicate_order

_active_tickers = {}  # master_account_id -> KiteTicker instance


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
            replicate_order(session, m, data, broadcast=broadcast)
        finally:
            session.close()

    def on_connect(ws, response):
        if broadcast:
            broadcast({"event": "master_connected", "master_account_id": master_account_id})

    def on_close(ws, code, reason):
        if broadcast:
            broadcast({"event": "master_disconnected", "master_account_id": master_account_id, "reason": reason})

    def on_error(ws, code, reason):
        if broadcast:
            broadcast({"event": "master_error", "master_account_id": master_account_id, "reason": reason})

    kws.on_order_update = on_order_update
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.on_error = on_error

    thread = threading.Thread(target=kws.connect, kwargs={"threaded": False}, daemon=True)
    thread.start()

    _active_tickers[master_account_id] = kws


def stop_master_listener(master_account_id: int):
    kws = _active_tickers.pop(master_account_id, None)
    if kws:
        try:
            kws.close()
        except Exception:
            pass


def is_running(master_account_id: int) -> bool:
    return master_account_id in _active_tickers
