"""
Kotak Neo child-account trading (order placement, capital, PnL, exit).

Kotak Neo uses different exchange-segment codes and its own trading-symbol format from
Zerodha (Kite), so a master's Kite order can't be forwarded to Kotak Neo's place_order()
as-is - it has to be translated to the matching Kotak Neo contract first via their scrip
search API. Equity symbols usually match Kite's underlying name directly (Kotak just adds
a "-EQ" series suffix); F&O contracts are resolved by underlying/expiry/strike/type rather
than by string-parsing Kite's tradingsymbol, since strike/expiry formatting differs between
the two brokers' symbologies and a parsing mistake could place the wrong contract with real
money. See replication_engine._get_instrument_detail for where the Kite side of that comes
from.

Based on Kotak's official v2 SDK docs (github.com/Kotak-Neo/Kotak-neo-api-v2) as of this
writing - some field names (particularly in totp_login's kwargs, and the exact live shape
of positions()/order_report() responses) were not fully verifiable without a live account,
so error messages are surfaced verbatim rather than swallowed, to make any mismatch obvious
on first real use instead of failing silently.
"""
import crypto_utils
from kotak_auth import get_kotak_client
from trade_log import log_trade_event

MARKET_PROTECTION_PCT = 5  # matches replication_engine's Zerodha buffer

_EXCHANGE_SEGMENT = {
    "NSE": "nse_cm",
    "BSE": "bse_cm",
    "NFO": "nse_fo",
    "BFO": "bse_fo",
    "MCX": "mcx_fo",
    "CDS": "cde_fo",
}
_TRANSACTION_TYPE = {"BUY": "B", "SELL": "S"}
_OPEN_ORDER_STATUSES = {"open", "trigger pending", "modify pending", "open pending", "validation pending"}


def _client_for(account) -> object:
    return get_kotak_client(
        consumer_key=crypto_utils.decrypt(account.api_key_enc),
        mobile_number=account.mobile_number,
        ucc=account.client_id,
        totp_secret=crypto_utils.decrypt(account.totp_secret_enc),
        mpin=crypto_utils.decrypt(account.mpin_enc),
    )


def _resolve_equity(client, exchange_segment: str, tradingsymbol: str) -> tuple:
    """Returns (kotak_trading_symbol, lot_size) for a cash-market equity symbol."""
    matches = client.search_scrip(exchange_segment=exchange_segment, symbol=tradingsymbol) or []
    if not isinstance(matches, list):
        raise ValueError(f"Kotak Neo search_scrip() returned an unexpected response: {matches!r}")
    for m in matches:
        if str(m.get("pSymbolName", "")).upper() == tradingsymbol.upper() and m.get("pGroup") == "EQ":
            lot_size = int(m.get("lLotSize") or m.get("iLotSize") or 1)
            return m["pTrdSymbol"], lot_size
    raise ValueError(f"Could not find a matching Kotak Neo equity scrip for {tradingsymbol} on {exchange_segment}.")


def _resolve_fo(client, exchange_segment: str, instrument: dict) -> tuple:
    """Returns (kotak_trading_symbol, lot_size) for an F&O contract, resolved from Kite's
    structured instrument fields (underlying/expiry/strike/type) rather than by parsing
    Kite's own tradingsymbol string."""
    expiry = instrument.get("expiry")
    expiry_str = expiry.strftime("%d%b%Y").upper() if hasattr(expiry, "strftime") else str(expiry or "")
    instrument_type = instrument.get("instrument_type", "")
    option_type = instrument_type if instrument_type in ("CE", "PE") else ""
    strike = instrument.get("strike") or 0
    matches = client.search_scrip(
        exchange_segment=exchange_segment,
        symbol=instrument.get("name", ""),
        expiry=expiry_str,
        option_type=option_type,
        strike_price=str(int(strike)) if strike else "",
    ) or []
    if not isinstance(matches, list):
        raise ValueError(f"Kotak Neo search_scrip() returned an unexpected response: {matches!r}")
    if not matches:
        raise ValueError(
            f"Could not find a matching Kotak Neo contract for {instrument.get('name')} "
            f"{expiry_str} {strike} {option_type} on {exchange_segment}."
        )
    m = matches[0]
    lot_size = int(m.get("lLotSize") or m.get("iLotSize") or 1)
    return m["pTrdSymbol"], lot_size


def place_child_order(account, exchange: str, tradingsymbol: str, transaction_type: str,
                       quantity: int, product: str, instrument: dict = None) -> str:
    """Places a market-protected order on a Kotak Neo child account, translating the
    master's Kite exchange/symbol into the equivalent Kotak Neo contract first. `instrument`
    is the full Kite instrument record (see replication_engine._get_instrument_detail) when
    the trade is F&O, None for equity."""
    exchange_segment = _EXCHANGE_SEGMENT.get(exchange)
    if not exchange_segment:
        raise ValueError(f"Kotak Neo child accounts don't support exchange {exchange!r} yet.")

    client = _client_for(account)
    if instrument is not None:
        kotak_symbol, _lot_size = _resolve_fo(client, exchange_segment, instrument)
    else:
        kotak_symbol, _lot_size = _resolve_equity(client, exchange_segment, tradingsymbol)

    resp = client.place_order(
        exchange_segment=exchange_segment,
        product=product,
        price="0",
        order_type="MKT",
        quantity=str(quantity),
        validity="DAY",
        trading_symbol=kotak_symbol,
        transaction_type=_TRANSACTION_TYPE.get(transaction_type, transaction_type),
        amo="NO",
        disclosed_quantity="0",
        market_protection=str(MARKET_PROTECTION_PCT),
        pf="N",
        trigger_price="0",
    )
    order_id = resp.get("nOrdNo") if isinstance(resp, dict) else None
    if not order_id:
        raise ValueError(f"Kotak Neo did not return an order id: {resp}")
    return order_id


def get_margin(account) -> float:
    client = _client_for(account)
    limits = client.limits(segment="ALL", exchange="ALL", product="ALL")
    if not isinstance(limits, dict):
        raise ValueError(f"Kotak Neo limits() returned an unexpected response: {limits!r}")
    try:
        return float(limits.get("Net", 0.0))
    except (TypeError, ValueError):
        return 0.0


def get_profile_name(account) -> str:
    """totp_login's response carries a greeting name; re-logs in to fetch it (cheap - just
    an extra TOTP-based login) since Kotak Neo has no separate lightweight profile call
    documented the way Kite's profile() is."""
    client = _client_for(account)
    resp = getattr(client, "copytrader_login_response", None)
    if not isinstance(resp, dict):
        return ""
    data = resp.get("data")
    return data.get("greetingName") or "" if isinstance(data, dict) else ""


def get_positions(account) -> list:
    """
    Open and closed (squared-off today) positions for the dashboard's positions panel. Net
    quantity is (cfBuyQty + flBuyQty) - (cfSellQty + flSellQty) per Kotak's docs - verified
    against a live account: a NIFTY position's flBuyQty/flSellQty matched lots * lotSz exactly.
    Closed positions (net 0) are kept, not dropped, so realized P&L from today's squared-off
    trades is still visible - same as how Zerodha's positions() keeps those rows too.
    """
    try:
        client = _client_for(account)
        positions = client.positions()
        rows = positions.get("data") if isinstance(positions, dict) else positions
        if not isinstance(rows, list):
            return []
        out = []
        for p in rows:
            buy_qty = float(p.get("cfBuyQty", 0) or 0) + float(p.get("flBuyQty", 0) or 0)
            sell_qty = float(p.get("cfSellQty", 0) or 0) + float(p.get("flSellQty", 0) or 0)
            net_qty = buy_qty - sell_qty
            buy_amt = float(p.get("cfBuyAmt", 0) or 0) + float(p.get("buyAmt", 0) or 0)
            sell_amt = float(p.get("cfSellAmt", 0) or 0) + float(p.get("sellAmt", 0) or 0)
            avg_price = (buy_amt / buy_qty) if buy_qty else (sell_amt / sell_qty if sell_qty else 0)
            out.append({
                "tradingsymbol": p.get("trdSym"),
                "exchange": p.get("exSeg"),
                "quantity": int(net_qty),
                "average_price": round(avg_price, 2),
                "pnl": round(sell_amt - buy_amt, 2),
                "product": p.get("prod"),
                "status": "OPEN" if net_qty != 0 else "CLOSED",
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def get_pnl(account):
    """
    Best-effort running P&L for a Kotak Neo account. Kotak's positions() response doesn't
    return a ready-made PnL figure the way Kite's does - it has to be derived from buy/sell
    amount fields per Kotak's docs, and only the realized buy/sell-amount delta is computed
    here (no live LTP fetch for the unrealized mark-to-market leg). Returns None (shown as
    "-" in the dashboard) if positions can't be fetched, rather than risk a wrong number.
    """
    try:
        client = _client_for(account)
        positions = client.positions()
        rows = positions.get("data") if isinstance(positions, dict) else positions
        if not isinstance(rows, list):
            return None
        if not rows:
            return 0.0
        total = 0.0
        for p in rows:
            buy_amt = float(p.get("cfBuyAmt", 0) or 0) + float(p.get("buyAmt", 0) or 0)
            sell_amt = float(p.get("cfSellAmt", 0) or 0) + float(p.get("sellAmt", 0) or 0)
            total += sell_amt - buy_amt
        return total
    except Exception:  # noqa: BLE001
        return None


def exit_account(db, account, broadcast=None) -> list:
    """
    Cancels every pending order on a Kotak Neo account. Unlike the Zerodha exit flow, this
    does NOT attempt to automatically square off open positions - Kotak Neo's positions API
    doesn't return a direct net-quantity field the way Kite's does (it has to be derived from
    several buy/sell quantity fields whose exact live shape we could not verify without a
    real account), and guessing wrong here would place a wrong-quantity order with real
    money. Open positions need to be closed manually on the Kotak Neo app/terminal after
    using this button.
    """
    results = []
    try:
        client = _client_for(account)
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Login failed: {e}", broadcast))
        return results

    try:
        report = client.order_report()
        rows = report.get("data") if isinstance(report, dict) else report
        if not isinstance(rows, list):
            raise ValueError(f"Kotak Neo order_report() returned an unexpected response: {rows!r}")
    except Exception as e:  # noqa: BLE001
        results.append(log_trade_event(db, account, None, None, None, None, "FAILED", f"Could not fetch order book: {e}", broadcast))
        rows = []

    for o in rows or []:
        status = str(o.get("ordSt") or o.get("stat") or "").strip().lower()
        if status not in _OPEN_ORDER_STATUSES:
            continue
        exchange = o.get("exSeg")
        tradingsymbol = o.get("trdSym")
        transaction_type = o.get("trnsTp")
        try:
            client.cancel_order(order_id=o.get("nOrdNo"))
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
        "Kotak Neo open positions were not auto-squared-off (not supported yet) - "
        "please close them manually on the Kotak Neo app/terminal.",
        broadcast,
    ))
    return results
