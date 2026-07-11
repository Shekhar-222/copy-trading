"""
Shared TradeLog creation/broadcast helper, used by both the Zerodha and Kotak Neo
order-placement/exit paths so trade-feed formatting stays consistent across brokers.
Kept separate from replication_engine.py so the Kotak Neo adapter can use it without
creating a circular import (replication_engine imports the Kotak adapter to place child
orders on Kotak Neo accounts).
"""
import datetime
from sqlalchemy.orm import Session

import models


def log_trade_event(db: Session, account: models.Account, exchange, tradingsymbol, transaction_type,
                     quantity, status: str, message: str, broadcast=None,
                     master_order_id: str = None, child_order_id: str = None) -> dict:
    log = models.TradeLog(
        timestamp=datetime.datetime.utcnow(),
        master_order_id=master_order_id,
        child_account_id=account.id,
        child_order_id=child_order_id,
        tradingsymbol=tradingsymbol,
        exchange=exchange,
        transaction_type=transaction_type,
        master_quantity=quantity,
        replicated_quantity=quantity,
        status=status,
        message=message,
    )
    db.add(log)
    db.commit()
    payload = to_dict(log, account.label)
    if broadcast:
        broadcast(payload)
    return payload


def today_ist_start_utc(now_utc: datetime.datetime = None) -> datetime.datetime:
    """Start of the current day in IST (where this app's users actually trade), expressed as a
    naive UTC datetime so it can be compared directly against TradeLog.timestamp (naive UTC,
    see to_dict's "Z"-suffix comment) - used to scope the live feed to just today's trades
    without deleting older rows. `now_utc` defaults to the real current time; overridable for
    tests."""
    now_utc = now_utc or datetime.datetime.utcnow()
    ist_now = now_utc + datetime.timedelta(hours=5, minutes=30)
    ist_midnight = ist_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return ist_midnight - datetime.timedelta(hours=5, minutes=30)


def to_dict(log: "models.TradeLog", account_label: str) -> dict:
    # log.timestamp is always naive UTC (set via datetime.utcnow()) - append "Z" explicitly so
    # the frontend's `new Date(...)` parses it as UTC and converts to the viewer's local time
    # correctly, instead of misreading an offset-less ISO string as already being local time.
    return {
        "timestamp": log.timestamp.isoformat() + "Z",
        "child_account": account_label,
        "tradingsymbol": log.tradingsymbol,
        "exchange": log.exchange,
        "transaction_type": log.transaction_type,
        "master_quantity": log.master_quantity,
        "replicated_quantity": log.replicated_quantity,
        "status": log.status,
        "message": log.message,
    }
