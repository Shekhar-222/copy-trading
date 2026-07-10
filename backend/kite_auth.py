"""
Handles daily Kite Connect access-token generation.

Kite Connect access tokens expire every day around market close / early morning.
This module supports two ways of generating today's token for an account:

1. AUTOMATED (login + password + TOTP secret) - convenient but relies on Zerodha's
   web login endpoints, which are NOT part of the official Kite Connect API and can
   change without notice. If it breaks, use the manual method below.

2. MANUAL - you log into https://kite.zerodha.com/connect/login?api_key=XXXX&v=3
   yourself, copy the `request_token` from the redirect URL, and paste it into the
   dashboard. The backend then exchanges it for an access_token via the official
   generate_session() call.

Both paths end up calling the official KiteConnect.generate_session(), so the only
"unofficial" part is step 1's login automation.
"""
import time
import pyotp
import requests
from kiteconnect import KiteConnect


class LoginError(Exception):
    pass


def auto_generate_request_token(client_id: str, password: str, totp_secret: str, api_key: str) -> str:
    """Automates the Zerodha web login flow to obtain a request_token for a given api_key."""
    session = requests.Session()

    # Step 1: submit user_id + password
    r = session.post(
        "https://kite.zerodha.com/api/login",
        data={"user_id": client_id, "password": password},
        timeout=15,
    )
    if r.status_code != 200:
        raise LoginError(f"Login step failed: {r.text}")
    request_id = r.json()["data"]["request_id"]

    # Step 2: submit TOTP
    totp = pyotp.TOTP(totp_secret).now()
    r2 = session.post(
        "https://kite.zerodha.com/api/twofa",
        data={"user_id": client_id, "request_id": request_id, "twofa_value": totp, "twofa_type": "totp"},
        timeout=15,
    )
    if r2.status_code != 200:
        raise LoginError(f"2FA step failed: {r2.text}")

    # Step 3: hit the Connect login URL with the authenticated session to get redirected
    # with a request_token in the query string.
    r3 = session.get(
        "https://kite.zerodha.com/connect/login",
        params={"api_key": api_key, "v": 3},
        allow_redirects=True,
        timeout=15,
    )
    # The request_token shows up as a query param on the final redirect URL.
    final_url = r3.url
    if "request_token=" not in final_url:
        raise LoginError(
            "Could not extract request_token - Zerodha may have changed their login "
            "flow, or 2FA failed silently. Use the manual token method instead."
        )
    request_token = final_url.split("request_token=")[1].split("&")[0]
    return request_token


def generate_access_token(api_key: str, api_secret: str, request_token: str) -> str:
    """Official Kite Connect call - exchanges a request_token for a day's access_token."""
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    return data["access_token"]


def get_kite_client(api_key: str, access_token: str) -> KiteConnect:
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite
