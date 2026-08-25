"""
Core copy-trading logic.

When the master account places/completes an order, this module works out the
proportional quantity for each active child account (based on capital ratio)
and places a matching order on that child's Kite (Zerodha), Kotak Neo, Angel
One, or Groww account - the master is always Zerodha (that's where the order-update feed
comes from), but children can be any of the four. See kotak_client.py / angel_client.py /
groww_client.py for the non-Zerodha sides.
"""
import collections
import concurrent.futures
import math
import datetime
from sqlalchemy.orm import Session

import models
import crypto_utils
import kotak_client
import angel_client
import groww_client
from database import SessionLocal
from kite_auth import get_kite_client
from trade_log import log_trade_event

# One master order can fan out to several children - each placed in its own thread (see
# _replicate_one_child) so a slow one (Kotak Neo's full re-login every action, Angel
# One's/Groww's post-placement rejection-check sleep) no longer holds up every other child's
# log entry behind it in a sequential loop. Confirmed live (2026-07-24): with children processed
# one at a time, the dashboard's "Live replication feed" showed logs trailing several seconds
# behind the master's actual fill, worst for whichever child happened to be last in the loop.
_CHILD_REPLICATION_WORKERS = 8

# Order IDs of every child order WE have placed as a mirror, regardless of which path placed it
# (fill-then-copy or resting-order). When the master's Kite Connect app is shared across family
# accounts (see kite_auth.py / the static-IP family setup), Kite's order-update WebSocket has been
# observed delivering order-update events for the OTHER permitted client's own orders too, not just
# the connecting master's - despite the client library's docs claiming it's scoped to "the connected
# user". Without this guard, a child's own mirrored order comes back around as if it were a brand
# new master fill and gets replicated again - a self-feeding loop that placed 150+ duplicate live
# orders in production on 2026-08-03. Checked at the top of replicate_order() before anything else.
# Capped (oldest evicted first) so a long-running process doesn't grow this forever - an
# order-update echo for our own placement arrives within moments, never days later.
_OWN_ORDER_ID_CAP = 5000
_own_placed_order_ids = set()
_own_placed_order_id_queue = collections.deque()


def _mark_own_order(order_id) -> None:
    order_id = str(order_id)
    _own_placed_order_ids.add(order_id)
    _own_placed_order_id_queue.append(order_id)
    if len(_own_placed_order_id_queue) > _OWN_ORDER_ID_CAP:
        _own_placed_order_ids.discard(_own_placed_order_id_queue.popleft())


_NON_ZERODHA_PLACERS = {
    "kotak_neo": (kotak_client.place_child_order, "Order placed on Kotak Neo (market protection)"),
    "angel_one": (angel_client.place_child_order, "Order placed on Angel One (market protection)"),
    "groww": (groww_client.place_child_order, "Order placed on Groww (market protection)"),
}


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


_FREEZE_QUANTITY = {
    # NSE/BSE "freeze quantity" - the exchange rejects any single F&O order placed above this,
    # per contract. Index values below are the latest published NSE/BSE circular figures; like
    # lot sizes (see _guess_lot_size) these are revised periodically, so treat this as a
    # best-effort table rather than a source of truth.
    "NIFTY": 1800,
    "BANKNIFTY": 900,
    "FINNIFTY": 1800,
    "MIDCPNIFTY": 3400,
    "SENSEX": 1000,
    "BANKEX": 900,
}
# Conservative fallback for any F&O/commodity/currency contract not listed above (mostly
# individual stock derivatives, whose freeze quantity NSE sets per-stock and revises quarterly) -
# errs toward slicing more than strictly necessary rather than risking an exchange rejection.
_DEFAULT_FREEZE_QUANTITY = 900


def _freeze_quantity(exchange: str, tradingsymbol: str):
    """Max quantity the exchange allows in a single order for this contract, or None if there's
    no freeze limit to worry about (equity cash trades have none)."""
    if exchange not in _LOT_SIZE_EXCHANGES:
        return None
    symbol = tradingsymbol.upper()
    for prefix, limit in _FREEZE_QUANTITY.items():
        if symbol.startswith(prefix):
            return limit
    return _DEFAULT_FREEZE_QUANTITY


def _slice_quantity(qty: int, lot_size: int, freeze_qty) -> list:
    """Splits qty into a list of per-order quantities, each within the exchange's freeze limit
    and a whole number of lots, so a single order never gets rejected for exceeding it."""
    if not freeze_qty or qty <= freeze_qty:
        return [qty]
    lots_per_slice = max(freeze_qty // lot_size, 1)
    slice_qty = lots_per_slice * lot_size
    slices = []
    remaining = qty
    while remaining > 0:
        chunk = min(slice_qty, remaining)
        slices.append(chunk)
        remaining -= chunk
    return slices


_STOPLOSS_ORDER_TYPES = {"SL", "SL-M"}
_MIRRORABLE_ORDER_TYPES = {"LIMIT"} | _STOPLOSS_ORDER_TYPES

# Exchanges copy-trading is turned off for right now - master orders on these are never
# mirrored to any child, regardless of order type or capital/multiplier settings.
_DISABLED_EXCHANGES = {"MCX"}


def replicate_order(db: Session, master_account: models.Account, order: dict, broadcast=None):
    """
    order: a dict from Kite's order update payload, expected to contain at least
    tradingsymbol, exchange, transaction_type, quantity, order_type, variety, product,
    status, and (for completed orders) average_price.

    LIMIT and stoploss (SL/SL-M) orders are mirrored onto children as a live resting order the
    moment they're actually resting on the master's book (see _handle_order_lifecycle) - this
    also covers AMO orders placed after hours, which sit resting (status OPEN) until the next
    session instead of filling right away, so they get queued on children immediately too
    rather than only once the master's AMO order fills the next morning.

    MARKET orders (AMO or not) are the one exception, kept on the original fill-then-copy
    path: Kite's API rejects a raw MARKET order outright (see _protected_limit_price), so there
    is no broker-supported "resting" order to place ahead of time - and pre-computing a
    protected limit price hours before an overnight AMO order would even reach the exchange
    would risk pricing it off a stale LTP that may have gapped by the next morning's open.
    Waiting for the master's own fill and pricing off the LTP at that moment avoids both
    problems, at the cost of the child's order landing a beat after the master's instead of
    resting independently.
    """
    order_id = order.get("order_id")
    if order_id and str(order_id) in _own_placed_order_ids:
        return  # our own child mirror looping back through the shared app's order-update
                # stream - never treat our own placement as a new master trigger

    exchange = order.get("exchange")
    status = order.get("status")
    if exchange in _DISABLED_EXCHANGES:
        # Copy-trading is turned off for this exchange for now - log once the master's own
        # order reaches a final state so there's a visible trace, but never mirror it to
        # children, regardless of order type.
        if status == "COMPLETE" or status in _TERMINAL_CANCEL_STATUSES:
            log_trade_event(
                db, master_account, exchange, order.get("tradingsymbol"),
                order.get("transaction_type"), order.get("quantity"), "SKIPPED",
                f"{exchange} copy-trading is currently disabled - order not mirrored to any child.",
                broadcast, master_order_id=order.get("order_id"),
            )
        return

    if order.get("order_type") in _MIRRORABLE_ORDER_TYPES:
        _handle_order_lifecycle(db, master_account, order, broadcast)
        return

    if status != "COMPLETE":
        if status in _TERMINAL_CANCEL_STATUSES:
            # Otherwise a rejected/cancelled master order (e.g. commodity margin shortfall,
            # MCX segment not enabled, MIS not allowed on that contract) vanishes with no
            # trace anywhere - surface it against the master so it's clear nothing was
            # copied because there was nothing to copy.
            reason = order.get("status_message") or f"Master order was {status.lower()}."
            log_trade_event(
                db, master_account, order.get("exchange"), order.get("tradingsymbol"),
                order.get("transaction_type"), order.get("quantity"), "SKIPPED", reason,
                broadcast, master_order_id=order.get("order_id"),
            )
        return

    _replicate_fill(db, master_account, order, broadcast)


def _replicate_one_child(child_id: int, exchange: str, tradingsymbol: str, transaction_type: str,
                          slices: list, product: str, instrument, note: str, master_order_id,
                          broadcast=None) -> None:
    """Places every freeze-limit slice for ONE child and logs the result. Runs in its own
    worker thread with its own DB session - SQLAlchemy sessions aren't safe to share across
    threads, so this re-fetches the child by id rather than being passed the ORM object loaded
    on the caller's session. See _replicate_fill for why this is concurrent at all."""
    db = SessionLocal()
    try:
        child = db.query(models.Account).get(child_id)
        if child is None:
            return

        placer = _NON_ZERODHA_PLACERS.get(child.broker)
        if placer:
            place_fn, label = placer
            for slice_qty in slices:
                try:
                    child_order_id = place_fn(
                        child, exchange, tradingsymbol, transaction_type, slice_qty, product, instrument,
                    )
                    log_trade_event(
                        db, child, exchange, tradingsymbol, transaction_type, slice_qty, "SUCCESS",
                        f"{label}{note}.",
                        broadcast, master_order_id=master_order_id, child_order_id=str(child_order_id),
                    )
                except Exception as e:  # noqa: BLE001 - log and move on to the next slice
                    log_trade_event(
                        db, child, exchange, tradingsymbol, transaction_type, slice_qty, "FAILED",
                        str(e), broadcast, master_order_id=master_order_id,
                    )
            return

        try:
            kite = get_kite_client(
                api_key=crypto_utils.decrypt(child.api_key_enc),
                access_token=crypto_utils.decrypt(child.access_token_enc),
            )
        except Exception as e:  # noqa: BLE001
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, sum(slices), "FAILED",
                str(e), broadcast, master_order_id=master_order_id,
            )
            return

        try:
            # One LTP fetch per child, shared across slices - the slices go out back-to-back
            # within the same moment, and Kite rate-limits quote calls.
            limit_price = _protected_limit_price(kite, exchange, tradingsymbol, transaction_type)
        except Exception as e:  # noqa: BLE001
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, sum(slices), "FAILED",
                str(e), broadcast, master_order_id=master_order_id,
            )
            return

        for slice_qty in slices:
            try:
                child_order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=slice_qty,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=limit_price,
                    product=product,
                )
                _mark_own_order(child_order_id)
                log_trade_event(
                    db, child, exchange, tradingsymbol, transaction_type, slice_qty, "SUCCESS",
                    f"Order placed as LIMIT @ {limit_price} (market protection){note}.",
                    broadcast, master_order_id=master_order_id, child_order_id=str(child_order_id),
                )
            except Exception as e:  # noqa: BLE001 - we want to log any Kite/network error and continue to next slice
                log_trade_event(
                    db, child, exchange, tradingsymbol, transaction_type, slice_qty, "FAILED",
                    str(e), broadcast, master_order_id=master_order_id,
                )
    finally:
        db.close()


def _replicate_fill(db: Session, master_account: models.Account, order: dict, broadcast=None):
    """The original fill-then-copy path: places an immediate market-protected order on each
    child. Used for completed regular (LIMIT/MARKET) orders, and as a fallback if a stoploss
    order completes without ever having been mirrored (see _handle_stoploss_update).

    Quantity/slicing is computed here (fast, no network), then each child's actual placement
    runs concurrently in _replicate_one_child - see that function's docstring and the
    _CHILD_REPLICATION_WORKERS comment above for why."""
    children = (
        db.query(models.Account)
        .filter(models.Account.role == "child", models.Account.active == True,  # noqa: E712
                models.Account.id != master_account.id)
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
    freeze_qty = _freeze_quantity(exchange, tradingsymbol)
    product = order.get("product", "MIS")
    master_order_id = order.get("order_id")

    with concurrent.futures.ThreadPoolExecutor(max_workers=_CHILD_REPLICATION_WORKERS) as pool:
        futures = []
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
                    broadcast, master_order_id=master_order_id, master_quantity=order["quantity"],
                )
                continue

            slices = _slice_quantity(qty, lot_size, freeze_qty)
            note = f" (sliced into {len(slices)} orders to stay within the exchange freeze limit)" if len(slices) > 1 else ""
            futures.append(pool.submit(
                _replicate_one_child, child.id, exchange, tradingsymbol, transaction_type,
                slices, product, instrument, note, master_order_id, broadcast,
            ))
        concurrent.futures.wait(futures)


_RESTING_STATUSES = {"OPEN", "TRIGGER PENDING", "AMO REQ RECEIVED"}
_TERMINAL_CANCEL_STATUSES = {"CANCELLED", "REJECTED"}


def _price_kwargs(order_type: str, price, trigger_price) -> dict:
    """Kite only accepts price/trigger_price for the order types that actually use them -
    passing price to a MARKET order (or trigger_price to a plain LIMIT order) is rejected."""
    kwargs = {}
    if order_type in ("LIMIT", "SL"):
        kwargs["price"] = price
    if order_type in _STOPLOSS_ORDER_TYPES:
        kwargs["trigger_price"] = trigger_price
    return kwargs


def _cancel_child_mirrors(db: Session, child: models.Account, mirrors: list, exchange, tradingsymbol,
                           transaction_type, reason: str, master_order_id, broadcast=None) -> None:
    """Cancels every given MirroredOrder row for one child (a single master order can be split
    into several freeze-limit slices, each its own row) and marks each CANCELLED."""
    try:
        kite = get_kite_client(
            api_key=crypto_utils.decrypt(child.api_key_enc),
            access_token=crypto_utils.decrypt(child.access_token_enc),
        )
    except Exception as e:  # noqa: BLE001
        for m in mirrors:
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, m.quantity, "FAILED",
                f"Could not cancel mirrored order: {e}", broadcast,
                master_order_id=master_order_id, child_order_id=m.child_order_id,
            )
        return

    for m in mirrors:
        try:
            kite.cancel_order(variety=m.variety, order_id=m.child_order_id)
            m.status = "CANCELLED"
            db.commit()
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, m.quantity, "SUCCESS",
                reason, broadcast, master_order_id=master_order_id, child_order_id=m.child_order_id,
            )
        except Exception as e:  # noqa: BLE001
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, m.quantity, "FAILED",
                f"Could not cancel mirrored order: {e}", broadcast,
                master_order_id=master_order_id, child_order_id=m.child_order_id,
            )


def _place_child_slices(db: Session, child: models.Account, master_order_id, exchange, tradingsymbol,
                         transaction_type, order_type, variety, price, trigger_price, product,
                         slices: list, broadcast=None) -> None:
    """Places one resting order per entry in `slices` on a Zerodha child, each staying within the
    exchange's freeze-quantity limit, and records a MirroredOrder row per slice so later
    modify/cancel events on the master order can find and act on all of them."""
    try:
        kite = get_kite_client(
            api_key=crypto_utils.decrypt(child.api_key_enc),
            access_token=crypto_utils.decrypt(child.access_token_enc),
        )
    except Exception as e:  # noqa: BLE001
        log_trade_event(
            db, child, exchange, tradingsymbol, transaction_type, sum(slices), "FAILED",
            str(e), broadcast, master_order_id=master_order_id,
        )
        return

    for i, slice_qty in enumerate(slices, start=1):
        try:
            child_order_id = kite.place_order(
                variety=variety,
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                transaction_type=transaction_type,
                quantity=slice_qty,
                order_type=order_type,
                product=product,
                **_price_kwargs(order_type, price, trigger_price),
            )
            _mark_own_order(child_order_id)
            db.add(models.MirroredOrder(
                master_order_id=master_order_id, child_account_id=child.id, child_order_id=str(child_order_id),
                exchange=exchange, tradingsymbol=tradingsymbol, transaction_type=transaction_type,
                order_type=order_type, variety=variety, trigger_price=trigger_price, price=price,
                quantity=slice_qty, status="OPEN",
            ))
            db.commit()
            detail = f"trigger @ {trigger_price}" if order_type in _STOPLOSS_ORDER_TYPES else f"@ {price}"
            amo_note = " (queued as AMO for next session)" if variety == "amo" else ""
            slice_note = f" (slice {i}/{len(slices)}, exchange freeze limit)" if len(slices) > 1 else ""
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, slice_qty, "SUCCESS",
                f"Order mirrored as pending {order_type} {detail}{amo_note}{slice_note}.",
                broadcast, master_order_id=master_order_id, child_order_id=str(child_order_id),
            )
        except Exception as e:  # noqa: BLE001
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, slice_qty, "FAILED",
                str(e), broadcast, master_order_id=master_order_id,
            )


def _handle_order_lifecycle(db: Session, master_account: models.Account, order: dict, broadcast=None):
    """
    Mirrors a LIMIT or stoploss (SL/SL-M) order onto each active Zerodha child while it's still
    resting on the master's book, instead of waiting for it to fill like the old fill-then-copy
    model (_replicate_fill) - so a child's own order book actually reflects the master's: a
    stoploss's protection or an AMO order's overnight queue position exists on the child too,
    not just after the master's already acted on it. Kotak Neo, Angel One, and Groww children
    are skipped for now (see kotak_client.py / angel_client.py / groww_client.py headers) -
    their place_child_order only knows how to submit an immediate market(-protected) order.
    """
    master_order_id = order.get("order_id")
    status = order.get("status")
    exchange = order.get("exchange")
    tradingsymbol = order.get("tradingsymbol", "")
    transaction_type = order.get("transaction_type")
    order_type = order.get("order_type")
    variety = order.get("variety") or "regular"
    trigger_price = order.get("trigger_price")
    price = order.get("price")

    mirrors = (
        db.query(models.MirroredOrder)
        .filter(models.MirroredOrder.master_order_id == master_order_id, models.MirroredOrder.status == "OPEN")
        .all()
    )

    if status in _TERMINAL_CANCEL_STATUSES:
        mirrors_by_child = {}
        for m in mirrors:
            mirrors_by_child.setdefault(m.child_account_id, []).append(m)
        for child_id, child_mirrors in mirrors_by_child.items():
            child = db.query(models.Account).get(child_id)
            _cancel_child_mirrors(
                db, child, child_mirrors, exchange, tradingsymbol, transaction_type,
                f"Master order {status.lower()} - mirrored order cancelled on child.",
                master_order_id, broadcast,
            )
        return

    if status == "COMPLETE":
        if not mirrors:
            # No live mirror existed (e.g. this order was already resting before this feature
            # shipped, or mirroring failed for every child) - fall back to the old
            # fill-then-copy behaviour so the trade isn't silently dropped.
            _replicate_fill(db, master_account, order, broadcast)
            return
        for m in mirrors:
            child = db.query(models.Account).get(m.child_account_id)
            m.status = "COMPLETE"
            db.commit()
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, m.quantity, "SUCCESS",
                "Master order filled - child's mirrored order should fill independently.",
                broadcast, master_order_id=master_order_id, child_order_id=m.child_order_id,
            )
        return

    if status not in _RESTING_STATUSES:
        return  # OPEN PENDING / MODIFY PENDING / VALIDATION PENDING - wait for the next event

    master_capital = master_account.capital or 0.0
    master_kite = get_kite_client(
        api_key=crypto_utils.decrypt(master_account.api_key_enc),
        access_token=crypto_utils.decrypt(master_account.access_token_enc),
    )
    instrument = _get_instrument_detail(master_kite, exchange, tradingsymbol)
    lot_size = order.get("lot_size") or (instrument or {}).get("lot_size") or _guess_lot_size(tradingsymbol)
    freeze_qty = _freeze_quantity(exchange, tradingsymbol)

    if mirrors:
        # Already mirrored - this update is a re-confirmation or the result of a modify on the
        # master. Bring each child's resting order(s) in line if the price/trigger/qty changed.
        mirrors_by_child = {}
        for m in mirrors:
            mirrors_by_child.setdefault(m.child_account_id, []).append(m)

        for child_id, child_mirrors in mirrors_by_child.items():
            child = db.query(models.Account).get(child_id)
            child_mirrors = sorted(child_mirrors, key=lambda m: m.id)
            qty = compute_child_quantity(
                master_qty=order.get("quantity", 0), master_capital=master_capital,
                child_capital=child.capital or 0.0, lot_size=lot_size,
                multiplier_override=child.multiplier_override,
            )

            if qty <= 0:
                _cancel_child_mirrors(
                    db, child, child_mirrors, exchange, tradingsymbol, transaction_type,
                    "Master order modified down to a size this child can no longer cover "
                    "with one lot - mirrored order cancelled.",
                    master_order_id, broadcast,
                )
                continue

            new_slices = _slice_quantity(qty, lot_size, freeze_qty)

            if len(new_slices) != len(child_mirrors):
                # The slice count itself changed (the order grew/shrank across a freeze-limit
                # boundary) - simplest correct fix is to cancel every existing slice for this
                # child and re-place fresh ones, rather than lining up modify() calls against a
                # shifted count.
                _cancel_child_mirrors(
                    db, child, child_mirrors, exchange, tradingsymbol, transaction_type,
                    "Master order modified - re-mirroring with updated slice count.",
                    master_order_id, broadcast,
                )
                _place_child_slices(
                    db, child, master_order_id, exchange, tradingsymbol, transaction_type,
                    order_type, variety, price, trigger_price, order.get("product", "MIS"),
                    new_slices, broadcast,
                )
                continue

            try:
                kite = get_kite_client(
                    api_key=crypto_utils.decrypt(child.api_key_enc),
                    access_token=crypto_utils.decrypt(child.access_token_enc),
                )
            except Exception as e:  # noqa: BLE001
                log_trade_event(
                    db, child, exchange, tradingsymbol, transaction_type, qty, "FAILED",
                    f"Could not update mirrored order: {e}", broadcast,
                    master_order_id=master_order_id,
                )
                continue

            for m, slice_qty in zip(child_mirrors, new_slices):
                if slice_qty == m.quantity and trigger_price == m.trigger_price and price == m.price:
                    continue  # nothing changed for this slice - avoid a redundant modify call
                try:
                    kite.modify_order(
                        variety=m.variety, order_id=m.child_order_id, quantity=slice_qty, order_type=order_type,
                        **_price_kwargs(order_type, price, trigger_price),
                    )
                    m.quantity, m.trigger_price, m.price = slice_qty, trigger_price, price
                    db.commit()
                    log_trade_event(
                        db, child, exchange, tradingsymbol, transaction_type, slice_qty, "SUCCESS",
                        "Master order modified - mirrored order updated to match.",
                        broadcast, master_order_id=master_order_id, child_order_id=m.child_order_id,
                    )
                except Exception as e:  # noqa: BLE001
                    log_trade_event(
                        db, child, exchange, tradingsymbol, transaction_type, slice_qty, "FAILED",
                        f"Could not update mirrored order: {e}", broadcast,
                        master_order_id=master_order_id, child_order_id=m.child_order_id,
                    )
        return

    # No mirror yet - this is a brand-new resting order (including a freshly-queued AMO order).
    # Place a matching one on every active child right away instead of waiting for it to fill.
    children = (
        db.query(models.Account)
        .filter(models.Account.role == "child", models.Account.active == True,  # noqa: E712
                models.Account.id != master_account.id)
        .all()
    )
    for child in children:
        qty = compute_child_quantity(
            master_qty=order.get("quantity", 0), master_capital=master_capital,
            child_capital=child.capital or 0.0, lot_size=lot_size,
            multiplier_override=child.multiplier_override,
        )
        if qty <= 0:
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, qty, "SKIPPED",
                "Computed replicated quantity was 0 (child capital too small for one lot).",
                broadcast, master_order_id=master_order_id, master_quantity=order.get("quantity", 0),
            )
            continue

        if child.broker in ("kotak_neo", "angel_one", "groww"):
            broker_label = {"kotak_neo": "Kotak Neo", "angel_one": "Angel One", "groww": "Groww"}[child.broker]
            log_trade_event(
                db, child, exchange, tradingsymbol, transaction_type, qty, "SKIPPED",
                f"Live order mirroring isn't supported for {broker_label} children yet - this order "
                "will only be copied to this child once it fills on the master.",
                broadcast, master_order_id=master_order_id,
            )
            continue

        slices = _slice_quantity(qty, lot_size, freeze_qty)
        _place_child_slices(
            db, child, master_order_id, exchange, tradingsymbol, transaction_type,
            order_type, variety, price, trigger_price, order.get("product", "MIS"),
            slices, broadcast,
        )


_OPEN_ORDER_STATUSES = {"OPEN", "TRIGGER PENDING", "MODIFY PENDING", "OPEN PENDING", "VALIDATION PENDING"}


def exit_account(db: Session, account: models.Account, broadcast=None) -> list:
    """
    Flattens a single account on demand (the dashboard's per-account "Exit" button):
    cancels every pending order first (so nothing can fill after we've squared off), then
    places an opposite protected LIMIT order against every open net position. Independent of
    the master/child replication flow - works the same for a master or a child account.

    Kotak Neo, Angel One, and Groww accounts are delegated to kotak_client.exit_account /
    angel_client.exit_account / groww_client.exit_account, which only cancel pending orders
    and do not auto-square-off positions - see those functions' docstrings for why.
    """
    if account.broker == "kotak_neo":
        return kotak_client.exit_account(db, account, broadcast=broadcast)
    if account.broker == "angel_one":
        return angel_client.exit_account(db, account, broadcast=broadcast)
    if account.broker == "groww":
        return groww_client.exit_account(db, account, broadcast=broadcast)

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

        instrument = _get_instrument_detail(kite, exchange, tradingsymbol)
        lot_size = (instrument or {}).get("lot_size") or _guess_lot_size(tradingsymbol)
        freeze_qty = _freeze_quantity(exchange, tradingsymbol)
        slices = _slice_quantity(exit_qty, lot_size, freeze_qty)
        note = f" (sliced into {len(slices)} orders to stay within the exchange freeze limit)" if len(slices) > 1 else ""

        try:
            # One LTP fetch per position, shared across slices (same reasoning as _replicate_fill).
            limit_price = _protected_limit_price(kite, exchange, tradingsymbol, transaction_type)
        except Exception as e:  # noqa: BLE001
            results.append(log_trade_event(
                db, account, exchange, tradingsymbol, transaction_type, exit_qty,
                "FAILED", str(e), broadcast,
            ))
            continue

        for slice_qty in slices:
            try:
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=tradingsymbol,
                    transaction_type=transaction_type,
                    quantity=slice_qty,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=limit_price,
                    product=pos.get("product", kite.PRODUCT_MIS),
                )
                _mark_own_order(order_id)
                results.append(log_trade_event(
                    db, account, exchange, tradingsymbol, transaction_type, slice_qty,
                    "SUCCESS", f"Exited @ {limit_price} (order {order_id}){note}.", broadcast,
                ))
            except Exception as e:  # noqa: BLE001
                results.append(log_trade_event(
                    db, account, exchange, tradingsymbol, transaction_type, slice_qty,
                    "FAILED", str(e), broadcast,
                ))

    return results


MARKET_PROTECTION_PCT = 5  # % buffer around LTP so the LIMIT order fills like a market order


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
    tick_size = _get_tick_size(kite, exchange, tradingsymbol)
    return round(round(raw_price / tick_size) * tick_size, 2)


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
    return _lookup_instrument(kite, exchange, tradingsymbol)


def _lookup_instrument(kite, exchange: str, tradingsymbol: str):
    """Cache-lookup step shared by _get_instrument_detail and _get_tick_size - refreshes the
    per-exchange instrument dump on a cache miss, then returns whatever's cached (or None)."""
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


def _get_tick_size(kite, exchange: str, tradingsymbol: str) -> float:
    """Real tick size for any exchange, not just F&O - equity scripts aren't all 0.05 (some are
    0.10 or more), so _protected_limit_price needs the actual instrument value rather than a
    guess. Unlike _get_instrument_detail this doesn't skip equity exchanges, since tick size
    (unlike lot size) isn't a safe constant to assume there."""
    instrument = _lookup_instrument(kite, exchange, tradingsymbol)
    return (instrument or {}).get("tick_size") or 0.05


def _guess_lot_size(tradingsymbol: str) -> int:
    """Last-resort fallback if the instrument dump can't be fetched - always prefer the real lot
    size from Kite's instrument dump (_get_instrument_detail) over this."""
    symbol = tradingsymbol.upper()
    if symbol.startswith("BANKNIFTY"):
        return 15
    if symbol.startswith("NIFTY"):
        return 65
    return 1
