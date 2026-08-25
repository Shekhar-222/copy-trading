"""
Live running P&L per account (individual + total), and the "does this account have a usable
token today" check. Split out of main.py so telegram_notify.py can reuse the same computation
without importing the whole FastAPI app module.
"""
import datetime

import models
import crypto_utils
import kotak_client
import angel_client
import groww_client
from kite_auth import get_kite_client


def token_is_fresh(acc: "models.Account") -> bool:
    if not acc.token_generated_at or not acc.access_token_enc:
        return False
    if acc.broker == "groww":
        # Groww's TOTP-generated access token is documented as never expiring (see
        # groww_auth.py) - once logged in, it stays "fresh" indefinitely instead of needing a
        # same-day check the way Zerodha/Angel/Kotak all do, so this deliberately skips the
        # date comparison below for this one broker.
        return True
    return acc.token_generated_at.date() == datetime.datetime.utcnow().date()


def compute_pnl(db) -> dict:
    """Live running P&L per account, pulled from open positions. Kotak Neo's and Groww's
    figures are best-effort (see kotak_client.get_pnl / groww_client.get_pnl) since neither
    positions API returns a ready-made live-LTP PnL the way Kite's does - both sum realized
    P&L only. Angel One's (see angel_client.get_pnl) is summed from its own documented "pnl"
    field."""
    accounts = db.query(models.Account).all()
    out = []
    total = 0.0
    for acc in accounts:
        pnl = None
        if token_is_fresh(acc):
            try:
                if acc.broker == "kotak_neo":
                    pnl = kotak_client.get_pnl(acc)
                elif acc.broker == "angel_one":
                    pnl = angel_client.get_pnl(acc)
                elif acc.broker == "groww":
                    pnl = groww_client.get_pnl(acc)
                else:
                    kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
                    positions = kite.positions()
                    pnl = sum(p.get("pnl", 0.0) for p in positions.get("net", []))
            except Exception:
                pnl = None
        out.append({"id": acc.id, "role": acc.role, "pnl": pnl})
        if pnl is not None:
            total += pnl
    return {"accounts": out, "total": total}
