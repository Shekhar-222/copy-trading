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


def to_dict(log: "models.TradeLog", account_label: str) -> dict:
    return {
        "timestamp": log.timestamp.isoformat(),
        "child_account": account_label,
        "tradingsymbol": log.tradingsymbol,
        "exchange": log.exchange,
        "transaction_type": log.transaction_type,
        "master_quantity": log.master_quantity,
        "replicated_quantity": log.replicated_quantity,
        "status": log.status,
        "message": log.message,
    }
