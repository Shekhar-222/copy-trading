"""
Groww child-account trading (order placement, capital, positions, PnL, exit).

Groww uses different trading-symbol conventions from Zerodha (Kite), so a master's Kite order
can't be forwarded to Groww's place_order() as-is - it has to be translated to the matching
Groww contract first. Unlike Kotak Neo/Angel One, there's no live search API - contracts are
resolved against a static CSV instrument file Groww publishes, downloaded once and cached here
(see _load_instrument_master).

Every field name/format used below (strike price is a plain unscaled number, expiry is ISO
"YYYY-MM-DD", instrument_type is CE/PE/FUT/EQ matching Kite's own values, F&O rows key on
underlying_symbol not name, equity trading_symbol has no "-EQ"-style suffix) was confirmed by
downloading and inspecting a real copy of https://growwapi-assets.groww.in/instruments/
instrument.csv directly - not assumed from docs alone. This matters: Angel One's equivalent
resolver shipped with a strike-price string-format mismatch that silently broke every F&O order
until debugged live (see angel_client.py's module docstring) - the numeric comparison here is
deliberate, informed by that incident, not a coincidence.

Groww's SDK raises real exceptions (growwapi.groww.exceptions.BaseGrowwException and
subclasses, each with a .msg attribute) rather than returning a silent {"status": false} dict
the way Kotak/Angel's SDKs do, so error handling here is a plain try/except surfacing e.msg
verbatim instead of manual response-shape validation.

Order placement uses a real ORDER_TYPE_MARKET order (Groww documents native MARKET order
support, unlike Kite which rejects one outright) rather than the protected-LIMIT workaround
Zerodha/Angel need - still unverified against a live account. Whether Groww's place_order()
response can be trusted as final is also unverified (its order_status enum has several
transitional values like NEW/ACKED), so a short-delay follow-up status check is applied here
too, following the exact lesson from angel_client._raise_if_rejected: a real insufficient-funds
rejection got silently logged as SUCCESS there until that check was added.
"""
import csv
import datetime
import io
import os
import threading
import time

import requests

import crypto_utils
from groww_auth import get_groww_client
from growwapi.groww.exceptions import BaseGrowwException
from trade_log import log_trade_event


# Kite's exchange field is segment-specific (NSE/BSE = cash, NFO/BFO = equity F&O, CDS =
# currency derivatives, MCX = commodity), but Groww treats exchange and segment as two
# independent dimensions - confirmed from GrowwAPI's own class constants (EXCHANGE_NSE/BSE/MCX
# only, no per-segment exchange code; SEGMENT_CASH/FNO/CURRENCY/COMMODITY separately). This
# mapping originally only covered a 1:1 NSE/BSE/MCX passthrough, so any real F&O order (Kite
# exchange "NFO") hit an unmapped lookup and was rejected locally before ever reaching Groww's
# API - confirmed live, 2026-07-24 (the same order placed manually on Groww's own app went
# through fine, proving it wasn't a real restriction on Groww's side, just a mapping gap here).
_EXCHANGE = {"NSE": "NSE", "BSE": "BSE", "NFO": "NSE", "BFO": "BSE", "CDS": "NSE", "MCX": "MCX"}
_SEGMENT = {"NSE": "CASH", "BSE": "CASH", "NFO": "FNO", "BFO": "FNO", "CDS": "CURRENCY", "MCX": "COMMODITY"}
_PRODUCT_TYPE = {"MIS": "MIS", "CNC": "CNC", "NRML": "NRML"}
_TRANSACTION_TYPE = {"BUY": "BUY", "SELL": "SELL"}
_ORDER_REJECTED_STATUSES = {"rejected", "failed"}
_OPEN_ORDER_STATUSES = {"new", "acked", "trigger_pending", "approved", "modification_requested"}

# Groww's public instrument list - a single CSV covering every exchange/segment, not a live
# search API. ~21MB (confirmed live), so cached both in memory AND on disk from the start -
# angel_client.py's equivalent cache was in-memory only for its first live use, and paid for
# that with a ~71s delay on the very first order after every backend restart. Not repeating
# that mistake here.
_INSTRUMENT_MASTER_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
_INSTRUMENT_MASTER_TTL = datetime.timedelta(hours=12)
_INSTRUMENT_MASTER_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".groww_instrument_cache.csv")
_instrument_master_cache = {"loaded_at": None, "by_exch_symbol": {}, "by_exch_underlying": {}}
# Children are now placed concurrently, one thread per child (see replication_engine's
# _replicate_one_child) - this guards the cache load/refresh so two Groww children resolving
# contracts at the same moment don't race into two simultaneous 21MB downloads.
_instrument_master_lock = threading.Lock()


def _build_index(rows: list) -> tuple:
    by_exch_symbol = {}
    by_exch_underlying = {}
    for row in rows:
        exch = row.get("exchange")
        segment = row.get("segment")
        if segment == "CASH":
            by_exch_symbol.setdefault((exch, str(row.get("trading_symbol", "")).upper()), row)
        elif segment == "FNO":
            key = (exch, str(row.get("underlying_symbol", "")).upper())
            by_exch_underlying.setdefault(key, []).append(row)
    return by_exch_symbol, by_exch_underlying


def _parse_csv_text(text: str) -> list:
    return list(csv.DictReader(io.StringIO(text)))


def _load_instrument_master() -> tuple:
    """Returns (by_exch_symbol, by_exch_underlying) built from Groww's instrument CSV,
    refreshing from the network at most once per _INSTRUMENT_MASTER_TTL. Checks the on-disk
    cache before hitting the network. Returns the existing (possibly stale, possibly empty)
    in-memory cache on a total fetch failure rather than raising, so a transient network blip
    doesn't take down order placement."""
    now = datetime.datetime.utcnow()
    if _instrument_master_cache["loaded_at"] and now - _instrument_master_cache["loaded_at"] < _INSTRUMENT_MASTER_TTL:
        return _instrument_master_cache["by_exch_symbol"], _instrument_master_cache["by_exch_underlying"]

    with _instrument_master_lock:
        # Re-check inside the lock - another thread may have already refreshed it while this
        # one was waiting.
        now = datetime.datetime.utcnow()
        if _instrument_master_cache["loaded_at"] and now - _instrument_master_cache["loaded_at"] < _INSTRUMENT_MASTER_TTL:
            return _instrument_master_cache["by_exch_symbol"], _instrument_master_cache["by_exch_underlying"]

        try:
            mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(_INSTRUMENT_MASTER_CACHE_FILE))
            if now - mtime < _INSTRUMENT_MASTER_TTL:
                with open(_INSTRUMENT_MASTER_CACHE_FILE, "r", encoding="utf-8") as f:
                    rows = list(csv.DictReader(f))
                by_symbol, by_underlying = _build_index(rows)
                _instrument_master_cache["by_exch_symbol"] = by_symbol
                _instrument_master_cache["by_exch_underlying"] = by_underlying
                _instrument_master_cache["loaded_at"] = now
                return by_symbol, by_underlying
        except Exception:  # noqa: BLE001 - no usable disk cache, fall through to a network fetch
            pass

        try:
            resp = requests.get(_INSTRUMENT_MASTER_URL, timeout=60)
            resp.raise_for_status()
            rows = _parse_csv_text(resp.text)
            by_symbol, by_underlying = _build_index(rows)
            _instrument_master_cache["by_exch_symbol"] = by_symbol
            _instrument_master_cache["by_exch_underlying"] = by_underlying
            _instrument_master_cache["loaded_at"] = now
            try:
                with open(_INSTRUMENT_MASTER_CACHE_FILE, "w", encoding="utf-8", newline="") as f:
                    f.write(resp.text)
            except Exception:  # noqa: BLE001 - disk write failing shouldn't lose the in-memory cache
                pass
        except Exception:  # noqa: BLE001 - leave cache as-is (possibly empty on first-ever failure)
            pass
        return _instrument_master_cache["by_exch_symbol"], _instrument_master_cache["by_exch_underlying"]


def _client_for(account) -> object:
    return get_groww_client(access_token=crypto_utils.decrypt(account.access_token_enc))


def _resolve_equity(exchange: str, tradingsymbol: str) -> tuple:
    """Returns (trading_symbol, exchange_token, lot_size). Groww's equity trading_symbol has
    no suffix (confirmed live: "RELIANCE", not "RELIANCE-EQ" - that's a separate
    internal_trading_symbol column not used for order placement)."""
    by_symbol, _ = _load_instrument_master()
    row = by_symbol.get((exchange, tradingsymbol.upper()))
    if not row:
        raise ValueError(f"Could not find a matching Groww equity scrip for {tradingsymbol} on {exchange}.")
    return row["trading_symbol"], row.get("exchange_token"), int(float(row.get("lot_size") or 1))


def _resolve_fo(exchange: str, instrument: dict) -> tuple:
    """Returns (trading_symbol, exchange_token, lot_size) for an F&O contract, resolved from
    Kite's structured instrument fields. Confirmed live against Groww's real CSV: F&O rows key
    on "underlying_symbol" (not "name", which is blank for F&O rows), expiry is ISO
    "YYYY-MM-DD", strike_price is a plain unscaled number, and instrument_type is CE/PE/FUT -
    the same values Kite itself uses, no translation table needed."""
    name = str(instrument.get("name", "")).upper()
    expiry = instrument.get("expiry")
    expiry_str = expiry.strftime("%Y-%m-%d") if hasattr(expiry, "strftime") else str(expiry or "")
    instrument_type = instrument.get("instrument_type", "")
    option_type = instrument_type if instrument_type in ("CE", "PE") else ""
    strike = instrument.get("strike") or 0

    _, by_underlying = _load_instrument_master()
    rows = by_underlying.get((exchange, name), [])
    for row in rows:
        if str(row.get("expiry_date", "")) != expiry_str:
            continue
        row_type = str(row.get("instrument_type", "")).upper()
        if option_type:
            if row_type != option_type:
                continue
            try:
                row_strike = float(row.get("strike_price", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(row_strike - float(strike)) > 0.01:
                continue
        elif row_type != "FUT":
            continue
        return row["trading_symbol"], row.get("exchange_token"), int(float(row.get("lot_size") or 1))
    raise ValueError(
        f"Could not find a matching Groww contract for {name} {expiry_str} {strike} {option_type} on {exchange}."
    )


def _raise_if_rejected(client, segment: str, groww_order_id: str) -> None:
    """place_order()'s own response already carries an order_status, but that status can be a
    transitional one (NEW/ACKED/...) rather than final - the same class of risk that made a
    real Angel One insufficient-funds rejection look like a SUCCESS until a follow-up check was
    added (see angel_client._raise_if_rejected, which this mirrors). Fails open (does nothing)
    if the status can't be confirmed, rather than risk marking a good order as FAILED."""
    time.sleep(1.5)
    try:
        resp = client.get_order_status(segment=segment, groww_order_id=groww_order_id)
    except Exception:  # noqa: BLE001 - can't confirm either way, don't block on it
        return
    if not isinstance(resp, dict):
        return
    status = str(resp.get("order_status") or "").strip().lower()
    if status in _ORDER_REJECTED_STATUSES:
        raise ValueError(resp.get("remark") or f"Groww rejected the order (status: {status}).")


def place_child_order(account, exchange: str, tradingsymbol: str, transaction_type: str,
                       quantity: int, product: str, instrument: dict = None) -> str:
    """Places a MARKET order on a Groww child account, translating the master's Kite
    exchange/symbol into the equivalent Groww contract first. `instrument` is the full Kite
    instrument record when the trade is F&O, None for equity."""
    groww_exchange = _EXCHANGE.get(exchange)
    segment = _SEGMENT.get(exchange)
    if not groww_exchange or not segment:
        raise ValueError(f"Groww child accounts don't support exchange {exchange!r} yet.")

    client = _client_for(account)
    if instrument is not None:
        symbol, _token, _lot_size = _resolve_fo(groww_exchange, instrument)
    else:
        symbol, _token, _lot_size = _resolve_equity(groww_exchange, tradingsymbol)

    try:
        resp = client.place_order(
            trading_symbol=symbol,
            quantity=quantity,
            validity=client.VALIDITY_DAY,
            exchange=groww_exchange,
            segment=segment,
            product=_PRODUCT_TYPE.get(product, "MIS"),
            order_type=client.ORDER_TYPE_MARKET,
            transaction_type=_TRANSACTION_TYPE.get(transaction_type, transaction_type),
        )
    except BaseGrowwException as e:
        raise ValueError(e.msg)

    order_id = resp.get("groww_order_id") if isinstance(resp, dict) else None
    if not order_id:
        raise ValueError(f"Groww did not return an order id: {resp}")

    immediate_status = str((resp or {}).get("order_status") or "").strip().lower()
    if immediate_status in _ORDER_REJECTED_STATUSES:
        raise ValueError((resp or {}).get("remark") or f"Groww rejected the order (status: {immediate_status}).")

    _raise_if_rejected(client, segment, order_id)
    return order_id


def get_margin(account) -> float:
    client = _client_for(account)
    try:
        resp = client.get_available_margin_details()
    except BaseGrowwException as e:
        raise ValueError(e.msg)
    try:
        return float((resp or {}).get("clear_cash", 0.0))
    except (TypeError, ValueError):
        return 0.0


def get_profile_name(account) -> str:
    """Groww's user-profile response doesn't document a display-name field (only vendor_user_id/
    ucc/segment flags per public docs) - falls back to the UCC (client code) if present, blank
    otherwise, same best-effort posture as Kotak/Angel's profile fetch."""
    client = _client_for(account)
    try:
        resp = client.get_user_profile()
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(resp, dict):
        return ""
    return resp.get("ucc") or ""


def get_positions(account) -> list:
    """Open and closed (squared-off today) positions for the dashboard's positions panel,
    mapped to the same shape main.py's other broker paths return. Groww's positions response
    only documents "realised_pnl" (no unrealized/live-LTP leg) - see get_pnl."""
    try:
        client = _client_for(account)
        resp = client.get_positions_for_user()
        rows = resp.get("positions") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return []
        out = []
        for p in rows:
            qty = int(float(p.get("quantity", 0) or 0))
            out.append({
                "tradingsymbol": p.get("trading_symbol"),
                "exchange": p.get("exchange"),
                "quantity": qty,
                "average_price": float(p.get("net_price", 0) or 0),
                "pnl": float(p.get("realised_pnl", 0) or 0),
                "product": p.get("product"),
                "status": "OPEN" if qty != 0 else "CLOSED",
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def get_pnl(account):
    """Best-effort running P&L for a Groww account - realised_pnl only, no live-LTP unrealized
    leg (same caveat as Kotak Neo's get_pnl). Returns None (shown as "-" in the dashboard) if
    positions can't be fetched, rather than risk a wrong number."""
    try:
        client = _client_for(account)
        resp = client.get_positions_for_user()
        rows = resp.get("positions") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return None
        return sum(float(p.get("realised_pnl", 0) or 0) for p in rows)
    except Exception:  # noqa: BLE001
        return None


def exit_account(db, account, broadcast=None) -> list:
    """
    Cancels every pending order on a Groww account. Unlike the Zerodha exit flow, this does
    NOT attempt to automatically square off open positions - even though Groww's "quantity"
    field is documented more clearly than Kotak's derived-from-multiple-fields situation, it
    hasn't been verified against a live account yet, and guessing wrong would risk placing a
    wrong-quantity order with real money (same caveat as kotak_client.exit_account /
    angel_client.exit_account). Open positions need to be closed manually on the Groww app
    after using this button.
    """
    results = []
    try:
        client = _client_for(account)
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Login failed: {e}", broadcast))
        return results

    try:
        # segment=None (the default) covers every segment in one call - confirmed from the
        # SDK's own signature, not assumed - see groww_client.py's development notes.
        resp = client.get_order_list(page=0, page_size=100)
        rows = resp.get("order_list") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            rows = []
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Could not fetch order book: {e}", broadcast))
        rows = []

    for o in rows or []:
        status = str(o.get("order_status") or "").strip().lower()
        if status not in _OPEN_ORDER_STATUSES:
            continue
        exchange = o.get("exchange")
        tradingsymbol = o.get("trading_symbol")
        transaction_type = o.get("transaction_type")
        try:
            client.cancel_order(groww_order_id=o.get("groww_order_id"), segment=o.get("segment"))
            results.append(log_trade_event(
                db, account, exchange, tradingsymbol, transaction_type, 0,
                "SUCCESS", "Pending order cancelled.", broadcast,
            ))
        except Exception as e:  # noqa: BLE001
            results.append(log_trade_event(
                db, account, exchange, tradingsymbol, transaction_type, 0,
                "FAILED", f"Cancel failed: {e}", broadcast,
            ))

    results.append(log_trade_event(
        db, account, None, None, None, None, "SKIPPED",
        "Groww open positions were not auto-squared-off (not supported yet) - "
        "please close them manually on the Groww app.",
        broadcast,
    ))
    return results
