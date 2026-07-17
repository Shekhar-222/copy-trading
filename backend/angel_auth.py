"""
Angel One (SmartAPI) login.

Unlike Kotak Neo, whose SDK has no documented way to reattach a session from a stored token
alone (see kotak_auth.py), Angel One's generateSession() returns a real, reusable JWT access
token + refresh token pair - the same shape Zerodha's daily access token has. This module
therefore follows kite_auth.py's model, not kotak_auth.py's: log in once a day via TOTP + PIN
(login()) and persist the resulting tokens, then reconstruct a client from those stored tokens
for every other action (get_angel_client()) with no further login call.

This matters in practice, not just in theory: an earlier version of this module logged in fresh
via generateSession() on every single action (matching Kotak's pattern), and a single "Auto-login"
click alone triggered three separate logins in a row (the login itself, then a margin fetch, then
a profile fetch). Angel's login endpoint is tightly rate-limited and started rejecting requests
with "Access denied because of exceeding access rate" - generateSession() is not cheap enough to
call repeatedly the way Kotak's totp_login()/totp_validate() apparently is.
"""
import pyotp
from SmartApi import SmartConnect


class AngelLoginError(Exception):
    pass


def _check_login_step(step_name: str, response) -> None:
    """
    SmartAPI's SDK, like Kotak's, doesn't always raise on an API-level failure (wrong TOTP/PIN,
    bad client code, etc.) - some failures just come back as a {"status": False, "message": ...}
    response instead. Left unchecked, that gets used as if login succeeded and the real cause
    only surfaces several calls later as a confusing crash in whatever runs next. Check
    explicitly right after login instead, so the actual SmartAPI error message is what the user
    sees. Confirmed live: a bad TOTP/client-code combination does come back in exactly this
    {"status": False, "message": "..."} shape rather than raising.
    """
    if not isinstance(response, dict):
        raise AngelLoginError(f"{step_name} returned an unexpected response: {response!r}")
    if not response.get("status"):
        raise AngelLoginError(f"{step_name} failed: {response.get('message') or response}")


def login(api_key: str, client_id: str, totp_secret: str, pin: str) -> dict:
    """Logs in fresh via TOTP + static trading PIN (no SMS OTP) and returns generateSession's
    "data" dict - {"jwtToken", "refreshToken", "feedToken"}. Call this once (at daily
    auto-login), persist jwtToken/refreshToken, and use get_angel_client() to reconstruct a
    client from them for every other action - see module docstring for why re-logging in per
    action isn't an option here."""
    client = SmartConnect(api_key=api_key)
    try:
        totp = pyotp.TOTP(totp_secret).now()
        login_response = client.generateSession(client_id, pin, totp)
        _check_login_step("SmartAPI login", login_response)
    except AngelLoginError:
        raise
    except Exception as e:  # noqa: BLE001 - surface whatever the SDK/API raised
        raise AngelLoginError(str(e))
    return login_response.get("data") or {}


def get_angel_client(api_key: str, access_token: str, refresh_token: str = None) -> SmartConnect:
    """Reconstructs a client from an already-issued access/refresh token pair - no network call,
    no TOTP needed. This is the one that should be used for every action other than the daily
    login itself (see login() above).

    Confirmed live: generateSession()'s "data.jwtToken" field comes back already prefixed with
    "Bearer " (undocumented, unexpected - most JWT APIs return the bare token). The SDK's own
    _request() unconditionally does `"Bearer {}".format(access_token)` when building the
    Authorization header, so passing the jwtToken through as-is doubles the prefix
    ("Bearer Bearer eyJ...") and every request gets rejected with "Invalid Token" - stripping
    it here, rather than trusting login() callers to have done it, means every access token
    this function is ever given ends up normalized regardless of where it came from."""
    if access_token and access_token.strip().lower().startswith("bearer "):
        access_token = access_token.strip()[len("bearer "):].strip()
    return SmartConnect(api_key=api_key, access_token=access_token, refresh_token=refresh_token)
