"""
Kotak Neo login.

Unlike Zerodha, Kotak Neo's TOTP+MPIN login has no SMS OTP step, so it's fully automatable -
there's no "manual token" fallback needed for this broker (see kite_auth.py for that concept).

Also unlike Zerodha, there's no documented way to reattach a session later from a stored token
alone (Kite's set_access_token() has no Kotak equivalent that's confirmed to work) - so every
Kotak Neo action logs in fresh with a newly generated TOTP via get_kotak_client() below, used
by both the dashboard's "Auto-login" button (to prove the stored credentials work and refresh
profile/capital) and every actual trading action, even though Kotak doesn't need a once-a-day
token refresh the way Kite does.
"""
import socket
import pyotp
import urllib3.util.connection as _urllib3_connection
from neo_api_client import NeoAPI

# Kotak Neo's static-IP whitelist for order placement only supports IPv4 ("IPv6 support
# coming soon" per their docs). On a dual-stack machine, Python/urllib3 (which both
# neo_api_client and kiteconnect use via `requests`) prefers IPv6 when a host resolves to
# both - so even a correctly whitelisted IPv4 address never matches, since the request
# actually goes out over IPv6, and place_order keeps failing with a generic "unauthorized".
# Forcing IPv4-only DNS resolution process-wide fixes this for Kotak (and is a no-op risk
# for Zerodha, which doesn't have this dual-stack ambiguity issue in practice).
_urllib3_connection.allowed_gai_family = lambda: socket.AF_INET


class KotakLoginError(Exception):
    pass


def _check_login_step(step_name: str, response) -> None:
    """
    Kotak's SDK doesn't always raise on an API-level failure (wrong TOTP/MPIN, bad UCC, session
    issues, etc.) - some failures just come back as an {"error": [...]} response instead. Left
    unchecked, that error response gets used as if login succeeded, and the real cause only
    surfaces several calls later as a confusing crash (e.g. calling .get() on a string) in
    whatever action happens to run next. Check explicitly right after each login step instead,
    so the actual Kotak error message is what the user sees.

    A successful totp_login/totp_validate response looks like {"data": {"status": "success",
    "token": ..., ...}} per Kotak's docs - note "status" is nested under "data", not a top-level
    field.
    """
    if not isinstance(response, dict):
        raise KotakLoginError(f"{step_name} returned an unexpected response: {response!r}")
    if "error" in response:
        raise KotakLoginError(f"{step_name} failed: {response['error']}")
    status = str((response.get("data") or {}).get("status", "")).lower()
    if status != "success":
        raise KotakLoginError(f"{step_name} failed: {response.get('data') or response}")


def get_kotak_client(consumer_key: str, mobile_number: str, ucc: str, totp_secret: str, mpin: str) -> NeoAPI:
    """Logs in fresh (TOTP + static MPIN, no SMS OTP) and returns an authenticated client.
    The totp_login response (which carries the account's greeting name) is stashed on the
    client as `copytrader_login_response` so callers that need it (see
    kotak_client.get_profile_name) don't have to make a separate call for it."""
    # neo_fin_key defaults to "neotradeapi" per Kotak's docs, but that default only applies when
    # the argument is omitted - passing neo_fin_key=None (as Kotak's own quickstart example
    # literally shows) overrides it with a real None instead. If that key is used as an auth
    # header specifically for trading endpoints (not read-only ones like limits/search_scrip),
    # omitting it would explain an "unauthorized" error isolated to place_order alone.
    client = NeoAPI(environment="prod", consumer_key=consumer_key, access_token=None, neo_fin_key="neotradeapi")
    try:
        totp = pyotp.TOTP(totp_secret).now()
        login_response = client.totp_login(mobile_number=mobile_number, ucc=ucc, totp=totp)
        _check_login_step("TOTP login", login_response)
        validate_response = client.totp_validate(mpin=mpin)
        _check_login_step("MPIN validation", validate_response)
    except KotakLoginError:
        raise
    except Exception as e:  # noqa: BLE001 - surface whatever Kotak's SDK/API raised
        raise KotakLoginError(str(e))
    client.copytrader_login_response = login_response
    return client
