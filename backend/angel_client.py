"""
Angel One child-account trading (order placement, capital, positions, PnL, exit).

Angel One (SmartAPI) uses different exchange codes and its own trading-symbol format from
Zerodha (Kite), so a master's Kite order can't be forwarded to Angel's placeOrder() as-is - it
has to be translated to the matching Angel contract (tradingsymbol + symboltoken) first.
Unlike Kotak Neo, Angel has no live symbol-search API - contracts are resolved against a
static JSON instrument/scrip master file Angel publishes, downloaded once and cached here (see
_load_instrument_master). Equity symbols usually match Kite's underlying name directly (Angel
adds a "-EQ" series suffix, same convention as Kotak); F&O contracts are resolved by
underlying/expiry/strike/type rather than by string-parsing Kite's tradingsymbol, for the same
reason kotak_client.py does - a parsing mistake could resolve the wrong contract with real
money. See replication_engine._get_instrument_detail for where the Kite side of that comes from.

Based on Angel One's public SmartAPI docs as of this writing - field names in generateSession,
position(), rmsLimit() and placeOrder()'s return shape were not verified against a live
account, so error messages are surfaced verbatim rather than swallowed, to make any mismatch
obvious on first real use instead of failing silently. Whether Angel's API rejects a plain
MARKET order the way Kite's does is also unverified, so orders are placed as a market-protected
LIMIT order (same technique as Zerodha children) rather than risk a rejected/unprotected order.

Every function here reconstructs its client from the account's stored access/refresh token
(angel_auth.get_angel_client) rather than logging in fresh - see angel_auth.py's module
docstring for why: a fresh TOTP login per action tripped Angel's rate limit on first real use.
"""
import datetime
import json
import os
import threading
import time

import requests

import crypto_utils
from angel_auth import get_angel_client
from trade_log import log_trade_event

MARKET_PROTECTION_PCT = 0.5  # matches replication_engine's and kotak_client's Zerodha/Kotak buffer

_EXCHANGE = {"NSE": "NSE", "BSE": "BSE", "NFO": "NFO", "BFO": "BFO", "MCX": "MCX", "CDS": "CDS"}
_TRANSACTION_TYPE = {"BUY": "BUY", "SELL": "SELL"}
_PRODUCT_TYPE = {"MIS": "INTRADAY", "CNC": "DELIVERY", "NRML": "CARRYFORWARD"}
_OPEN_ORDER_STATUSES = {"open", "pending", "trigger pending", "modified", "open pending", "validation pending"}

# Angel's public scrip master - a single JSON dump covering every exchange/segment, not a
# live search API. This file is genuinely large (~33MB, confirmed live), so it's cached both
# in memory AND on disk (_INSTRUMENT_MASTER_CACHE_FILE) - an in-memory-only cache means every
# backend restart forces a fresh ~33MB download on the very next Angel One order, which
# measured ~15s on a good connection here and stacked with other order-placement latency to
# put a live copied order roughly a minute behind the master's - confirmed against a real
# order (see git history / README §7). Persisting to disk means a restart only re-downloads
# once the file is actually older than the TTL, not on every process start.
_INSTRUMENT_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_INSTRUMENT_MASTER_TTL = datetime.timedelta(hours=12)
_INSTRUMENT_MASTER_CACHE_FILE = os.path.join(os.path.dirname(__file__), ".angel_scrip_master_cache.json")
_instrument_master_cache = {"loaded_at": None, "by_exch_name": {}}
# Children are now placed concurrently, one thread per child (see replication_engine's
# _replicate_one_child) - this guards the cache load/refresh so two Angel One children
# resolving contracts at the same moment don't race into two simultaneous 33MB downloads.
_instrument_master_lock = threading.Lock()


def _build_index(rows: list) -> dict:
    by_exch_name = {}
    for row in rows:
        key = (row.get("exch_seg"), str(row.get("name", "")).upper())
        by_exch_name.setdefault(key, []).append(row)
    return by_exch_name


def _load_instrument_master() -> dict:
    """Returns {(exch_seg, name): [row, ...]} from Angel's scrip master, refreshing from the
    network at most once per _INSTRUMENT_MASTER_TTL. Checks the on-disk cache before hitting
    the network (see module-level comment above for why), and returns the existing (possibly
    stale, possibly empty) in-memory cache on a total fetch failure rather than raising, so a
    transient network blip doesn't take down order placement - callers just won't find new
    listings until the next successful refresh."""
    now = datetime.datetime.utcnow()
    if _instrument_master_cache["loaded_at"] and now - _instrument_master_cache["loaded_at"] < _INSTRUMENT_MASTER_TTL:
        return _instrument_master_cache["by_exch_name"]

    with _instrument_master_lock:
        # Re-check inside the lock - another thread may have already refreshed it while this
        # one was waiting.
        now = datetime.datetime.utcnow()
        if _instrument_master_cache["loaded_at"] and now - _instrument_master_cache["loaded_at"] < _INSTRUMENT_MASTER_TTL:
            return _instrument_master_cache["by_exch_name"]

        try:
            mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(_INSTRUMENT_MASTER_CACHE_FILE))
            if now - mtime < _INSTRUMENT_MASTER_TTL:
                with open(_INSTRUMENT_MASTER_CACHE_FILE, "r", encoding="utf-8") as f:
                    rows = json.load(f)
                _instrument_master_cache["by_exch_name"] = _build_index(rows)
                _instrument_master_cache["loaded_at"] = now
                return _instrument_master_cache["by_exch_name"]
        except Exception:  # noqa: BLE001 - no usable disk cache, fall through to a network fetch
            pass

        try:
            resp = requests.get(_INSTRUMENT_MASTER_URL, timeout=60)
            resp.raise_for_status()
            rows = resp.json()
            _instrument_master_cache["by_exch_name"] = _build_index(rows)
            _instrument_master_cache["loaded_at"] = now
            try:
                with open(_INSTRUMENT_MASTER_CACHE_FILE, "w", encoding="utf-8") as f:
                    json.dump(rows, f)
            except Exception:  # noqa: BLE001 - disk write failing shouldn't lose the in-memory cache
                pass
        except Exception:  # noqa: BLE001 - leave cache as-is (possibly empty on first-ever failure)
            pass
        return _instrument_master_cache["by_exch_name"]


def _client_for(account) -> object:
    """Reconstructs a client from the access/refresh token pair stored at the last daily
    login (main.py's auto_login) - does NOT log in fresh. account.api_secret_enc holds the
    refresh token here (repurposed - Angel One has no API secret concept - see models.py)."""
    return get_angel_client(
        api_key=crypto_utils.decrypt(account.api_key_enc),
        access_token=crypto_utils.decrypt(account.access_token_enc),
        refresh_token=crypto_utils.decrypt(account.api_secret_enc) if account.api_secret_enc else None,
    )


def _resolve_equity(exchange_segment: str, tradingsymbol: str) -> tuple:
    """Returns (angel_trading_symbol, symboltoken, lot_size) for a cash-market equity symbol."""
    by_exch_name = _load_instrument_master()
    rows = by_exch_name.get((exchange_segment, tradingsymbol.upper()), [])
    for row in rows:
        if str(row.get("symbol", "")).upper() == f"{tradingsymbol.upper()}-EQ":
            return row["symbol"], row["token"], int(float(row.get("lotsize") or 1))
    raise ValueError(f"Could not find a matching Angel One equity scrip for {tradingsymbol} on {exchange_segment}.")


def _resolve_fo(exchange_segment: str, instrument: dict) -> tuple:
    """Returns (angel_trading_symbol, symboltoken, lot_size) for an F&O contract, resolved from
    Kite's structured instrument fields (underlying/expiry/strike/type) rather than by parsing
    Kite's own tradingsymbol string. Angel's scrip master stores expiry as "DDMMMYYYY" (e.g.
    "28MAR2024", same format Kotak uses) and strike as rupees*100 as a string, per public docs."""
    name = str(instrument.get("name", "")).upper()
    expiry = instrument.get("expiry")
    expiry_str = expiry.strftime("%d%b%Y").upper() if hasattr(expiry, "strftime") else str(expiry or "")
    instrument_type = instrument.get("instrument_type", "")
    option_type = instrument_type if instrument_type in ("CE", "PE") else ""
    strike = instrument.get("strike") or 0
    strike_key = int(round(strike * 100))

    by_exch_name = _load_instrument_master()
    rows = by_exch_name.get((exchange_segment, name), [])
    for row in rows:
        if str(row.get("expiry", "")).upper() != expiry_str:
            continue
        row_type = str(row.get("instrumenttype", "")).upper()
        if option_type:
            if row.get("symbol", "").upper()[-2:] != option_type:
                continue
            # Angel's scrip master stores strike as a decimal STRING ("3000000.000000") -
            # confirmed live - so a raw string comparison against an int-built key never
            # matches even for a contract that genuinely exists, which is exactly what
            # happened here: every F&O order failed with "Could not find a matching contract"
            # before ever reaching Angel's API, so nothing showed up in the broker's own order
            # history - there was never an order to show. Compare numerically instead.
            try:
                row_strike = int(round(float(row.get("strike", 0))))
            except (TypeError, ValueError):
                continue
            if row_strike != strike_key:
                continue
        elif row_type not in ("FUTIDX", "FUTSTK", "FUTCUR", "FUTCOM"):
            continue
        return row["symbol"], row["token"], int(float(row.get("lotsize") or 1))
    raise ValueError(
        f"Could not find a matching Angel One contract for {name} {expiry_str} {strike} {option_type} "
        f"on {exchange_segment}."
    )


def _ltp(client, exchange_segment: str, tradingsymbol: str, symboltoken: str) -> float:
    resp = client.ltpData(exchange_segment, tradingsymbol, symboltoken)
    if not isinstance(resp, dict) or not resp.get("status"):
        raise ValueError(f"Angel One ltpData() failed: {resp}")
    return float(resp["data"]["ltp"])


def _protected_limit_price(client, exchange_segment: str, tradingsymbol: str, symboltoken: str,
                            transaction_type: str) -> float:
    """Same market-protection technique as replication_engine._protected_limit_price - see
    module docstring for why plain MARKET isn't used here."""
    ltp = _ltp(client, exchange_segment, tradingsymbol, symboltoken)
    buffer = ltp * (MARKET_PROTECTION_PCT / 100)
    raw_price = ltp + buffer if transaction_type == "BUY" else ltp - buffer
    return round(raw_price * 20) / 20  # snap to the 0.05 tick size


def place_child_order(account, exchange: str, tradingsymbol: str, transaction_type: str,
                       quantity: int, product: str, instrument: dict = None) -> str:
    """Places a market-protected LIMIT order on an Angel One child account, translating the
    master's Kite exchange/symbol into the equivalent Angel contract first. `instrument` is the
    full Kite instrument record (see replication_engine._get_instrument_detail) when the trade
    is F&O, None for equity."""
    exchange_segment = _EXCHANGE.get(exchange)
    if not exchange_segment:
        raise ValueError(f"Angel One child accounts don't support exchange {exchange!r} yet.")

    client = _client_for(account)
    if instrument is not None:
        angel_symbol, symboltoken, _lot_size = _resolve_fo(exchange_segment, instrument)
    else:
        angel_symbol, symboltoken, _lot_size = _resolve_equity(exchange_segment, tradingsymbol)

    angel_txn_type = _TRANSACTION_TYPE.get(transaction_type, transaction_type)
    limit_price = _protected_limit_price(client, exchange_segment, angel_symbol, symboltoken, angel_txn_type)

    resp = client.placeOrder({
        "variety": "NORMAL",
        "tradingsymbol": angel_symbol,
        "symboltoken": symboltoken,
        "transactiontype": angel_txn_type,
        "exchange": exchange_segment,
        "ordertype": "LIMIT",
        "producttype": _PRODUCT_TYPE.get(product, "INTRADAY"),
        "duration": "DAY",
        "price": str(limit_price),
        "squareoff": "0",
        "stoploss": "0",
        "quantity": str(quantity),
    })
    # placeOrder's return shape isn't fully confirmed from here - the SDK is documented to
    # return either the order id directly or a {"status": True, "data": {"orderid": ...}}
    # dict depending on version, so handle both rather than guess one.
    order_id = None
    if isinstance(resp, str):
        order_id = resp
    elif isinstance(resp, dict):
        order_id = resp.get("data", {}).get("orderid") if isinstance(resp.get("data"), dict) else resp.get("orderid")
    if not order_id:
        raise ValueError(f"Angel One did not return an order id: {resp}")

    _raise_if_rejected(client, order_id)
    return order_id


def _raise_if_rejected(client, order_id: str) -> None:
    """
    placeOrder() returning an order id only means Angel accepted the request into its order
    pipeline - it does NOT mean the order will actually execute. An RMS-level rejection (e.g.
    insufficient funds) happens moments later and was, before this check existed, silently
    logged as a SUCCESS trade with real money never having moved - confirmed live: a rejected
    order came back from placeOrder() with a normal order id exactly like an accepted one.

    Gives Angel a moment to run its RMS check, then looks the order up in orderBook() (whose
    "status"/"text" fields were confirmed live against that same rejected order) and raises
    with the broker's own rejection message if it was rejected. If the order can't be found or
    the order book can't be fetched, this fails open (does nothing) rather than risk marking a
    perfectly good order as FAILED over a lookup hiccup - this is a best-effort check, not a
    guarantee, since a rejection could in principle still arrive after this point.
    """
    time.sleep(1.5)
    try:
        resp = client.orderBook()
        rows = (resp or {}).get("data") if isinstance(resp, dict) else resp
    except Exception:  # noqa: BLE001 - can't confirm either way, don't block on it
        return
    if not isinstance(rows, list):
        return
    for row in rows:
        if str(row.get("orderid")) != str(order_id):
            continue
        status = str(row.get("status") or row.get("orderstatus") or "").strip().lower()
        if status == "rejected":
            raise ValueError(row.get("text") or "Angel One rejected the order.")
        return


def get_margin(account) -> float:
    client = _client_for(account)
    resp = client.rmsLimit()
    if not isinstance(resp, dict) or not resp.get("status"):
        raise ValueError(f"Angel One rmsLimit() failed: {resp}")
    try:
        return float((resp.get("data") or {}).get("net", 0.0))
    except (TypeError, ValueError):
        return 0.0


def get_profile_name(account) -> str:
    """generateSession's response doesn't carry the account's display name - a separate
    getProfile(refresh_token) call is needed per Angel's docs, using the same stored refresh
    token _client_for() uses."""
    if not account.api_secret_enc:
        return ""
    refresh_token = crypto_utils.decrypt(account.api_secret_enc)
    client = _client_for(account)
    try:
        resp = client.getProfile(refresh_token)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data")
    return data.get("name") or "" if isinstance(data, dict) else ""


def get_positions(account) -> list:
    """Open and closed (squared-off today) positions for the dashboard's positions panel,
    mapped to the same shape main.py's Zerodha/Kotak paths return. Unlike Kotak, Angel's
    position() response does document a ready-made "pnl" field directly - see module
    docstring for the caveat that this hasn't been checked against a live account."""
    try:
        client = _client_for(account)
        resp = client.position()
        rows = (resp or {}).get("data") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return []
        out = []
        for p in rows:
            net_qty = int(float(p.get("netqty", 0) or 0))
            out.append({
                "tradingsymbol": p.get("tradingsymbol"),
                "exchange": p.get("exchange"),
                "quantity": net_qty,
                "average_price": float(p.get("avgnetprice", 0) or 0),
                "pnl": float(p.get("pnl", 0) or 0),
                "product": p.get("producttype"),
                "status": "OPEN" if net_qty != 0 else "CLOSED",
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def get_pnl(account):
    """Running P&L for an Angel One account, summed straight from position()'s own "pnl"
    field (realised + unrealised, per Angel's docs) - no LTP-derivation needed here, unlike
    Kotak. Returns None (shown as "-" in the dashboard) if positions can't be fetched, rather
    than risk a wrong number."""
    try:
        client = _client_for(account)
        resp = client.position()
        rows = (resp or {}).get("data") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return None
        return sum(float(p.get("pnl", 0) or 0) for p in rows)
    except Exception:  # noqa: BLE001
        return None


def exit_account(db, account, broadcast=None) -> list:
    """
    Cancels every pending order on an Angel One account. Unlike the Zerodha exit flow, this
    does NOT attempt to automatically square off open positions - the exact net-quantity
    semantics of position() haven't been verified against a live account, and guessing wrong
    would risk placing a wrong-quantity order with real money (same caveat as
    kotak_client.exit_account). Open positions need to be closed manually on the Angel One
    app/terminal after using this button.
    """
    results = []
    try:
        client = _client_for(account)
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Login failed: {e}", broadcast))
        return results

    try:
        resp = client.orderBook()
        rows = (resp or {}).get("data") if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            rows = []
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Could not fetch order book: {e}", broadcast))
        rows = []

    for o in rows or []:
        status = str(o.get("status") or o.get("orderstatus") or "").strip().lower()
        if status not in _OPEN_ORDER_STATUSES:
            continue
        exchange = o.get("exchange")
        tradingsymbol = o.get("tradingsymbol")
        transaction_type = o.get("transactiontype")
        try:
            client.cancelOrder(order_id=o.get("orderid"), variety=o.get("variety", "NORMAL"))
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
        "Angel One open positions were not auto-squared-off (not supported yet) - "
        "please close them manually on the Angel One app/terminal.",
        broadcast,
    ))
    return results
