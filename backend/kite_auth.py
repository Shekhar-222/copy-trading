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

    try:
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

        # Step 3: hit the Connect login URL with the authenticated session. This redirects
        # (still on kite.zerodha.com) to /connect/finish, which in turn redirects to the app's
        # registered redirect URL carrying request_token in its query string. Follow only the
        # first, same-host hop and read the second hop's target off its Location header
        # (allow_redirects=False) rather than actually connecting to it - that final URL is
        # whatever the app's redirect URL is (here, the local frontend dev server), which may
        # not be running/reachable and has nothing to do with completing the login.
        r3 = session.get(
            "https://kite.zerodha.com/connect/login",
            params={"api_key": api_key, "v": 3},
            allow_redirects=False,
            timeout=15,
        )
        finish_url = r3.headers.get("Location", "")
        r4 = session.get(finish_url, allow_redirects=False, timeout=15)
        location = r4.headers.get("Location", "")
        if "request_token=" not in location:
            raise LoginError(
                "Could not extract request_token - Zerodha may have changed their login "
                "flow, or 2FA failed silently. Use the manual token method instead."
            )
        return location.split("request_token=")[1].split("&")[0]
    except LoginError:
        raise
    except requests.exceptions.RequestException as e:
        raise LoginError(f"Network error while logging in: {e}")


def generate_access_token(api_key: str, api_secret: str, request_token: str, expected_client_id: str = None) -> str:
    """Official Kite Connect call - exchanges a request_token for a day's access_token.

    expected_client_id, when given, is checked against generate_session()'s own user_id field
    before the token is trusted. Necessary now that one app/api_key can be shared across family
    accounts (the static-IP setup): a mixed-up login - wrong account's button clicked, or some
    quirk of Kite's session handling on a shared app - can silently hand back a token for a
    DIFFERENT client than the one you meant to log in. Without this check, that token still
    "works" (every call succeeds), it just silently trades on the wrong person's real account -
    exactly what happened to Tatya's account on 2026-08-03, where its stored token turned out to
    authenticate as MAP014 instead of SOS452.
    """
    kite = KiteConnect(api_key=api_key)
    data = kite.generate_session(request_token, api_secret=api_secret)
    if expected_client_id and data.get("user_id") != expected_client_id:
        raise LoginError(
            f"Logged in as {data.get('user_id')}, not {expected_client_id} - wrong account "
            "(easy mix-up when accounts share one app). Token discarded, nothing was saved."
        )
    return data["access_token"]


def get_kite_client(api_key: str, access_token: str) -> KiteConnect:
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite
