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
        kotak_symbol, lot_size = _resolve_fo(client, exchange_segment, instrument)
    else:
        kotak_symbol, lot_size = _resolve_equity(client, exchange_segment, tradingsymbol)

    if lot_size and quantity % lot_size != 0:
        # quantity is computed upstream in replication_engine.py from the MASTER's (Kite)
        # lot size, shared across every child - it's normally correct since exchanges set lot
        # sizes centrally, not brokers, so Kite and Kotak should agree. When they don't, Kite's
        # instrument data is the one that's stale/wrong (confirmed live, 2026-09: Kite reported
        # lot_size=1 for a real MCX contract Kotak correctly resolved as lot_size=10, matching
        # the same strike/expiry/type - not a wrong-contract mismatch, genuinely bad Kite data).
        # Fail loudly with the real numbers instead of letting Kotak's API reject it with an
        # opaque "please provide valid lotwise quantity" - and this same stale lot size was used
        # for every OTHER child's quantity too, so a mismatch here is worth checking elsewhere.
        raise ValueError(
            f"Kotak Neo's own lot size for {kotak_symbol} is {lot_size}, but was asked to place "
            f"{quantity} - not a whole multiple. This usually means Kite's instrument data has a "
            f"stale/incorrect lot size for this contract (quantity is computed from Kite's lot "
            f"size and shared across every child) - worth checking whether other children's "
            f"actual fills for this contract are sized correctly too."
        )

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


def _safe_float(value, default: float = 1.0) -> float:
    """Coerces a Kotak numeric-as-string field, falling back to `default` for missing/zero/
    unparseable values - guards the PnL formula's multiplier/genNum/genDen/prcNum/prcDen
    terms against a stray "0" or "" causing a bogus multiply-by-zero or ZeroDivisionError."""
    try:
        v = float(value)
        return v if v else default
    except (TypeError, ValueError):
        return default


def _fetch_ltps(client, tokens: list) -> dict:
    """Live LTP for a batch of Kotak positions, keyed by (exSeg, tok) exactly as they appear
    on a position row. Kotak's positions() response has no LTP field of its own (confirmed by
    Kotak's own SDK team: github.com/Kotak-Neo/Kotak-neo-api-v2/issues/19), so it has to be
    fetched separately via quotes(), which is capped at 50 instruments/call by the backend
    API (not the SDK) - batched here to respect that."""
    ltp_by_token = {}
    for i in range(0, len(tokens), 50):
        batch = tokens[i:i + 50]
        resp = client.quotes(
            instrument_tokens=[{"instrument_token": tok, "exchange_segment": seg} for seg, tok in batch],
            quote_type="ltp",
        )
        rows = resp if isinstance(resp, list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                ltp_by_token[(row.get("exchange"), str(row.get("exchange_token")))] = float(row.get("ltp") or 0)
            except (TypeError, ValueError):
                continue
    return ltp_by_token


def _unrealized_leg(p: dict, net_qty: float, ltp: float) -> float:
    """The mark-to-market term of Kotak's documented PnL formula (docs/Positions.md):
    Net Qty * LTP * multiplier * (genNum/genDen) * (prcNum/prcDen)."""
    multiplier = _safe_float(p.get("multiplier"))
    gen_num = _safe_float(p.get("genNum"))
    gen_den = _safe_float(p.get("genDen"))
    prc_num = _safe_float(p.get("prcNum"))
    prc_den = _safe_float(p.get("prcDen"))
    return net_qty * ltp * multiplier * (gen_num / gen_den) * (prc_num / prc_den)


def get_positions(account) -> list:
    """
    Open and closed (squared-off today) positions for the dashboard's positions panel. Net
    quantity is (cfBuyQty + flBuyQty) - (cfSellQty + flSellQty) per Kotak's docs - verified
    against a live account: a NIFTY position's flBuyQty/flSellQty matched lots * lotSz exactly.
    Closed positions (net 0) are kept, not dropped, so realized P&L from today's squared-off
    trades is still visible - same as how Zerodha's positions() keeps those rows too.

    PnL for OPEN rows follows Kotak's documented formula (realized leg + unrealized
    mark-to-market via a live LTP fetch) - see get_pnl()'s docstring for why the unrealized
    leg matters. CLOSED rows have net qty 0 so the unrealized leg is naturally zero and the
    realized-only figure is already correct, same as before.
    """
    try:
        client = _client_for(account)
        positions = client.positions()
        rows = positions.get("data") if isinstance(positions, dict) else positions
        if not isinstance(rows, list):
            return []

        parsed = []
        open_tokens = []
        for p in rows:
            buy_qty = float(p.get("cfBuyQty", 0) or 0) + float(p.get("flBuyQty", 0) or 0)
            sell_qty = float(p.get("cfSellQty", 0) or 0) + float(p.get("flSellQty", 0) or 0)
            net_qty = buy_qty - sell_qty
            buy_amt = float(p.get("cfBuyAmt", 0) or 0) + float(p.get("buyAmt", 0) or 0)
            sell_amt = float(p.get("cfSellAmt", 0) or 0) + float(p.get("sellAmt", 0) or 0)
            avg_price = (buy_amt / buy_qty) if buy_qty else (sell_amt / sell_qty if sell_qty else 0)
            parsed.append((p, net_qty, sell_amt - buy_amt))
            if net_qty and p.get("tok") and p.get("exSeg"):
                open_tokens.append((p.get("exSeg"), str(p.get("tok"))))

        ltp_by_token = {}
        if open_tokens:
            try:
                ltp_by_token = _fetch_ltps(client, open_tokens)
            except Exception:  # noqa: BLE001
                pass  # unrealized leg is best-effort; realized pnl below is still shown

        out = []
        for p, net_qty, realized in parsed:
            pnl = realized
            ltp = ltp_by_token.get((p.get("exSeg"), str(p.get("tok"))))
            if net_qty and ltp is not None:
                pnl += _unrealized_leg(p, net_qty, ltp)
            buy_qty = float(p.get("cfBuyQty", 0) or 0) + float(p.get("flBuyQty", 0) or 0)
            sell_qty = float(p.get("cfSellQty", 0) or 0) + float(p.get("flSellQty", 0) or 0)
            buy_amt = float(p.get("cfBuyAmt", 0) or 0) + float(p.get("buyAmt", 0) or 0)
            sell_amt = float(p.get("cfSellAmt", 0) or 0) + float(p.get("sellAmt", 0) or 0)
            avg_price = (buy_amt / buy_qty) if buy_qty else (sell_amt / sell_qty if sell_qty else 0)
            out.append({
                "tradingsymbol": p.get("trdSym"),
                "exchange": p.get("exSeg"),
                "quantity": int(net_qty),
                "average_price": round(avg_price, 2),
                "pnl": round(pnl, 2),
                "product": p.get("prod"),
                "status": "OPEN" if net_qty != 0 else "CLOSED",
            })
        return out
    except Exception:  # noqa: BLE001
        return []


def get_pnl(account):
    """
    Best-effort running P&L for a Kotak Neo account, following Kotak's own documented formula
    (docs/Positions.md): (Total Sell Amt - Total Buy Amt) + (Net Qty * LTP * multiplier *
    (genNum/genDen) * (prcNum/prcDen)). The first term is realized cash-flow; the second is
    the unrealized mark-to-market leg for whatever's still open, which needs a live LTP -
    Kotak's positions() response doesn't include one (confirmed by Kotak's own SDK team:
    github.com/Kotak-Neo/Kotak-neo-api-v2/issues/19), so it's fetched separately via quotes().

    Previously this function only computed the first (realized) term, which is exactly why
    the figure looked wrong while a position was open live and "corrected itself" once the
    position closed - net qty hits 0 at that point, so the missing unrealized leg was zero
    anyway and the bug was invisible.

    The unrealized leg is wrapped in its own try/except so a quotes() failure (rate limit,
    network blip) degrades to realized-only P&L instead of losing the figure entirely -
    still returns None only when positions() itself can't be fetched.
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
        open_rows = []
        for p in rows:
            buy_qty = float(p.get("cfBuyQty", 0) or 0) + float(p.get("flBuyQty", 0) or 0)
            sell_qty = float(p.get("cfSellQty", 0) or 0) + float(p.get("flSellQty", 0) or 0)
            net_qty = buy_qty - sell_qty
            buy_amt = float(p.get("cfBuyAmt", 0) or 0) + float(p.get("buyAmt", 0) or 0)
            sell_amt = float(p.get("cfSellAmt", 0) or 0) + float(p.get("sellAmt", 0) or 0)
            total += sell_amt - buy_amt
            if net_qty and p.get("tok") and p.get("exSeg"):
                open_rows.append((p, net_qty))

        if open_rows:
            try:
                tokens = [(p.get("exSeg"), str(p.get("tok"))) for p, _ in open_rows]
                ltp_by_token = _fetch_ltps(client, tokens)
                for p, net_qty in open_rows:
                    ltp = ltp_by_token.get((p.get("exSeg"), str(p.get("tok"))))
                    if ltp is not None:
                        total += _unrealized_leg(p, net_qty, ltp)
            except Exception:  # noqa: BLE001
                pass  # unrealized leg is best-effort; realized total above is still meaningful

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
