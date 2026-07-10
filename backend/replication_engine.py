"""
Core copy-trading logic.

When the master account places/completes an order, this module works out the
proportional quantity for each active child account (based on capital ratio)
and places a matching order on that child's Kite (Zerodha) or Kotak Neo account -
the master is always Zerodha (that's where the order-update feed comes from), but
children can be either broker. See kotak_client.py for the Kotak Neo side.
"""
import math
import datetime
from sqlalchemy.orm import Session

import models
import crypto_utils
import kotak_client
from kite_auth import get_kite_client
from trade_log import log_trade_event


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
    exchange = order.get("exchange")
    tradingsymbol = order.get("tradingsymbol", "")
    transaction_type = order.get("transaction_type")
    instrument = _get_instrument_detail(master_kite, exchange, tradingsymbol)
    lot_size = order.get("lot_size") or (instrument or {}).get("lot_size") or _guess_lot_size(tradingsymbol)

    for child in children:
        qty = compute_child_quantity(
            master_qty=order["quantity"],
            master_capital=master_capital,
            child_capital=child.capital or 0.0,
            lot_size=lot_size,
            multiplier_override=child.multiplier_override,
        )

        if qty <= 0:
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, qty, "SKIPPED",
                "Computed replicated quantity was 0 (child capital too small for one lot).",
                broadcast, master_order_id=order.get("order_id"),
            )
            continue

        if child.broker == "kotak_neo":
            try:
                child_order_id = kotak_client.place_child_order(
                    child, exchange, tradingsymbol, transaction_type, qty,
                    order.get("product", "MIS"), instrument,
                )
                log_trade_event(
                    db, child, exchange, tradingsymbol, transaction_type, qty, "SUCCESS",
                    "Order placed on Kotak Neo (market protection).",
                    broadcast, master_order_id=order.get("order_id"), child_order_id=str(child_order_id),
                )
            except Exception as e:  # noqa: BLE001 - log and move on to the next child
                log_trade_event(
                    db, child, exchange, tradingsymbol, transaction_type, qty, "FAILED",
                    str(e), broadcast, master_order_id=order.get("order_id"),
                )
            continue

        try:
            kite = get_kite_client(
                api_key=crypto_utils.decrypt(child.api_key_enc),
                access_token=crypto_utils.decrypt(child.access_token_enc),
            )
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
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, qty, "SUCCESS",
                f"Order placed as LIMIT @ {limit_price} (market protection).",
                broadcast, master_order_id=order.get("order_id"), child_order_id=str(child_order_id),
            )
        except Exception as e:  # noqa: BLE001 - we want to log any Kite/network error and continue to next child
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, qty, "FAILED",
                str(e), broadcast, master_order_id=order.get("order_id"),
            )


_OPEN_ORDER_STATUSES = {"OPEN", "TRIGGER PENDING", "MODIFY PENDING", "OPEN PENDING", "VALIDATION PENDING"}


def exit_account(db: Session, account: models.Account, broadcast=None) -> list:
    """
    Flattens a single account on demand (the dashboard's per-account "Exit" button):
    cancels every pending order first (so nothing can fill after we've squared off), then
    places an opposite protected LIMIT order against every open net position. Independent of
    the master/child replication flow - works the same for a master or a child account.

    Kotak Neo accounts are delegated to kotak_client.exit_account, which only cancels pending
    orders and does not auto-square-off positions - see that function's docstring for why.
    """
    if account.broker == "kotak_neo":
        return kotak_client.exit_account(db, account, broadcast=broadcast)

    kite = get_kite_client(
        api_key=crypto_utils.decrypt(account.api_key_enc),
        access_token=crypto_utils.decrypt(account.access_token_enc),
    )
    results = []

    try:
        orders = kite.orders()
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Could not fetch order book: {e}", broadcast))
        orders = []

    for o in orders:
        if o.get("status") not in _OPEN_ORDER_STATUSES:
            continue
        try:
            kite.cancel_order(variety=o["variety"], order_id=o["order_id"])
            results.append(log_trade_event(
                db, account, o.get("exchange"), o.get("tradingsymbol"), o.get("transaction_type"), 0,
                "SUCCESS", "Pending order cancelled.", broadcast,
            ))
        except Exception as e:  # noqa: BLE001
            results.append(log_trade_event(
                db, account, o.get("exchange"), o.get("tradingsymbol"), o.get("transaction_type"), 0,
                "FAILED", f"Cancel failed: {e}", broadcast,
            ))

    try:
        positions = kite.positions().get("net", [])
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Could not fetch positions: {e}", broadcast))
        return results

    for pos in positions:
        qty = pos.get("quantity", 0)
        if qty == 0:
            continue
        exchange = pos["exchange"]
        tradingsymbol = pos["tradingsymbol"]
        transaction_type = kite.TRANSACTION_TYPE_SELL if qty > 0 else kite.TRANSACTION_TYPE_BUY
        exit_qty = abs(qty)
        try:
            limit_price = _protected_limit_price(kite, exchange, tradingsymbol, transaction_type)
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=exit_qty,
                order_type=kite.ORDER_TYPE_LIMIT,
                price=limit_price,
                product=pos.get("product", kite.PRODUCT_MIS),
            )
            results.append(log_trade_event(
                db, account, exchange, tradingsymbol, transaction_type, exit_qty,
                "SUCCESS", f"Exited @ {limit_price} (order {order_id}).", broadcast,
            ))
        except Exception as e:  # noqa: BLE001
            results.append(log_trade_event(
                db, account, exchange, tradingsymbol, transaction_type, exit_qty,
                "FAILED", str(e), broadcast,
            ))

    return results


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
_instrument_details = {}            # (exchange, tradingsymbol) -> full instrument dict from kite.instruments()
_instrument_cache_loaded_at = {}    # exchange -> datetime last refreshed


def _get_instrument_detail(kite, exchange: str, tradingsymbol: str):
    """
    Full instrument record (name/expiry/strike/instrument_type/lot_size) for an F&O contract,
    pulled from Kite's instrument dump and cached per exchange for a few hours since it doesn't
    change intraday. Returns None for non-F&O exchanges or if the dump can't be fetched.

    Used both for lot-size lookups (NSE/NFO revise F&O lot sizes periodically - this broke
    NIFTY once already, see _guess_lot_size) and to translate a Kite F&O symbol into the
    equivalent contract on another broker: kotak_client.py's F&O symbol resolution needs the
    underlying/expiry/strike/type rather than Kite's own tradingsymbol string, since brokers
    format symbols differently and string-parsing Kite's format would risk resolving the wrong
    contract.
    """
    if exchange not in _LOT_SIZE_EXCHANGES:
        return None
    key = (exchange, tradingsymbol)
    if key not in _instrument_details:
        _refresh_instrument_cache(kite, exchange)
    return _instrument_details.get(key)


def _refresh_instrument_cache(kite, exchange: str) -> None:
    now = datetime.datetime.utcnow()
    last_loaded = _instrument_cache_loaded_at.get(exchange)
    if last_loaded and now - last_loaded < _INSTRUMENT_CACHE_TTL:
        return
    try:
        for inst in kite.instruments(exchange):
            _instrument_details[(inst["exchange"], inst["tradingsymbol"])] = inst
        _instrument_cache_loaded_at[exchange] = now
    except Exception:  # noqa: BLE001 - leave cache as-is, callers fall back to a guess
        pass


def _guess_lot_size(tradingsymbol: str) -> int:
    """Last-resort fallback if the instrument dump can't be fetched - always prefer the real lot
    size from Kite's instrument dump (_get_instrument_detail) over this."""
    symbol = tradingsymbol.upper()
    if symbol.startswith("BANKNIFTY"):
        return 15
    if symbol.startswith("NIFTY"):
        return 65
    return 1
