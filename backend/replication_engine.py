"""
Core copy-trading logic.

When the master account places/completes an order, this module works out the
proportional quantity for each active child account (based on capital ratio)
and places a matching order on that child's Kite account.
"""
import math
import datetime
from sqlalchemy.orm import Session

import models
import crypto_utils
from kite_auth import get_kite_client


def compute_child_quantity(master_qty: int, master_capital: float, child_capital: float,
                            lot_size: int, multiplier_override: float = None) -> int:
    """
    Scales master's quantity by the ratio of child capital to master capital,
    then rounds DOWN to the nearest whole lot so we never over-order.
    A manual multiplier_override, if set on the account, takes precedence over
    the capital-ratio calculation.
    """
    if multiplier_override is not None:
        raw_qty = master_qty * multiplier_override
    else:
        if not master_capital or master_capital <= 0:
            return 0
        ratio = child_capital / master_capital
        raw_qty = master_qty * ratio

    lots = math.floor(raw_qty / lot_size)
    return max(lots, 0) * lot_size


def replicate_order(db: Session, master_account: models.Account, order: dict, broadcast=None):
    """
    order: a dict from Kite's order update payload, expected to contain at least
    tradingsymbol, exchange, transaction_type, quantity, order_type, product,
    status, and (for completed orders) average_price.

    Only replicates orders whose status is COMPLETE, to avoid copying orders
    that may still get rejected or modified on the master side.
    """
    if order.get("status") != "COMPLETE":
        return

    children = (
        db.query(models.Account)
        .filter(models.Account.role == "child", models.Account.active == True)  # noqa: E712
        .all()
    )

    master_capital = master_account.capital or 0.0
    master_kite = get_kite_client(
        api_key=crypto_utils.decrypt(master_account.api_key_enc),
        access_token=crypto_utils.decrypt(master_account.access_token_enc),
    )
    lot_size = order.get("lot_size") or _get_lot_size(master_kite, order.get("exchange"), order.get("tradingsymbol", ""))

    for child in children:
        qty = compute_child_quantity(
            master_qty=order["quantity"],
            master_capital=master_capital,
            child_capital=child.capital or 0.0,
            lot_size=lot_size,
            multiplier_override=child.multiplier_override,
        )

        log = models.TradeLog(
            timestamp=datetime.datetime.utcnow(),
            master_order_id=order.get("order_id"),
            child_account_id=child.id,
            tradingsymbol=order.get("tradingsymbol"),
            exchange=order.get("exchange"),
            transaction_type=order.get("transaction_type"),
            master_quantity=order["quantity"],
            replicated_quantity=qty,
        )

        if qty <= 0:
            log.status = "SKIPPED"
            log.message = "Computed replicated quantity was 0 (child capital too small for one lot)."
            db.add(log)
            db.commit()
            if broadcast:
                broadcast(_log_to_dict(log, child.label))
            continue

        try:
            kite = get_kite_client(
                api_key=crypto_utils.decrypt(child.api_key_enc),
                access_token=crypto_utils.decrypt(child.access_token_enc),
            )
            exchange = order.get("exchange")
            tradingsymbol = order.get("tradingsymbol")
            transaction_type = order.get("transaction_type")
            limit_price = _protected_limit_price(kite, exchange, tradingsymbol, transaction_type)
            child_order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                product=order.get("product", kite.PRODUCT_MIS),
            )
            log.child_order_id = str(child_order_id)
            log.status = "SUCCESS"
            log.message = f"Order placed as LIMIT @ {limit_price} (market protection)."
        except Exception as e:  # noqa: BLE001 - we want to log any Kite/network error and continue to next child
            log.status = "FAILED"
            log.message = str(e)

        db.add(log)
        db.commit()
        if broadcast:
            broadcast(_log_to_dict(log, child.label))


MARKET_PROTECTION_PCT = 0.5  # % buffer around LTP so the LIMIT order fills like a market order


def _protected_limit_price(kite, exchange: str, tradingsymbol: str, transaction_type: str) -> float:
    """
    Kite Connect rejects plain MARKET orders placed via the API ("market orders without
    market protection are not allowed - please set market protection or use a limit order"),
    and there's no place_order parameter to set protection directly. We emulate it: fetch LTP
    and place a LIMIT order priced a small buffer beyond it in the trade's direction, which
    fills immediately like a market order under normal liquidity while capping slippage.
    """
    quote_key = f"{exchange}:{tradingsymbol}"
    ltp = kite.ltp(quote_key)[quote_key]["last_price"]
    buffer = ltp * (MARKET_PROTECTION_PCT / 100)
    raw_price = ltp + buffer if transaction_type == kite.TRANSACTION_TYPE_BUY else ltp - buffer
    return round(raw_price * 20) / 20  # snap to the 0.05 tick size


# Exchanges where lot size actually matters (equity is always 1, so skip the network round trip there).
_LOT_SIZE_EXCHANGES = {"NFO", "BFO", "MCX", "CDS"}
_INSTRUMENT_CACHE_TTL = datetime.timedelta(hours=12)
_instrument_lot_sizes = {}          # (exchange, tradingsymbol) -> lot_size
_instrument_cache_loaded_at = {}    # exchange -> datetime last refreshed


def _get_lot_size(kite, exchange: str, tradingsymbol: str) -> int:
    """
    NSE/NFO revise F&O lot sizes periodically (this broke NIFTY: the old hardcoded guess of 25
    is stale, Kite now requires multiples of 65). Rather than hardcode a number that will go
    stale again, pull the real lot size from Kite's instrument dump, cached per exchange for a
    few hours since it doesn't change intraday. Falls back to a best-effort guess only if the
    instrument dump can't be fetched (e.g. transient network error).
    """
    if exchange not in _LOT_SIZE_EXCHANGES:
        return 1

    key = (exchange, tradingsymbol)
    if key not in _instrument_lot_sizes:
        _refresh_instrument_cache(kite, exchange)
    return _instrument_lot_sizes.get(key) or _guess_lot_size(tradingsymbol)


def _refresh_instrument_cache(kite, exchange: str) -> None:
    now = datetime.datetime.utcnow()
    last_loaded = _instrument_cache_loaded_at.get(exchange)
    if last_loaded and now - last_loaded < _INSTRUMENT_CACHE_TTL:
        return
    try:
        for inst in kite.instruments(exchange):
            _instrument_lot_sizes[(inst["exchange"], inst["tradingsymbol"])] = inst["lot_size"]
        _instrument_cache_loaded_at[exchange] = now
    except Exception:  # noqa: BLE001 - leave cache as-is, _get_lot_size falls back to the guess
        pass


def _guess_lot_size(tradingsymbol: str) -> int:
    """Last-resort fallback if the instrument dump can't be fetched - always prefer the real lot
    size from Kite's instrument dump (_get_lot_size) over this."""
    symbol = tradingsymbol.upper()
    if symbol.startswith("BANKNIFTY"):
        return 15
    if symbol.startswith("NIFTY"):
        return 65
    return 1


def _log_to_dict(log: models.TradeLog, child_label: str) -> dict:
    return {
        "timestamp": log.timestamp.isoformat(),
        "child_account": child_label,
        "tradingsymbol": log.tradingsymbol,
        "exchange": log.exchange,
        "transaction_type": log.transaction_type,
        "master_quantity": log.master_quantity,
        "replicated_quantity": log.replicated_quantity,
        "status": log.status,
        "message": log.message,
    }
