"""
Groww login.

Unlike every other broker here, Groww's TOTP-based access token is documented as having
"No Expiry" - generateSession-equivalent (get_access_token) is called once (at account setup /
"Auto-login"), and the resulting token is persisted and reused indefinitely, the same way
Angel One's token is reused rather than re-logged-in per action, except Groww has no daily
refresh cycle at all: main.py's _token_is_fresh() has a broker-specific carve-out for "groww"
so it doesn't force a same-day re-login the way Zerodha/Angel do.

Groww's SDK raises real Python exceptions on API errors (GrowwAPIException and subclasses,
each carrying .msg/.code) rather than the silent {"status": false} dict shape Kotak/Angel's
SDKs use - no manual response-shape validation needed here.

get_access_token() below does NOT call growwapi.GrowwAPI.get_access_token() - it re-implements
that one call directly. Confirmed live (2026-07-24): on a 400 response, the SDK reads
error.displayMessage from the body, but Groww's real API returns the message under
error.errorMessage instead, so the SDK swallows the real reason and reports a useless generic
"Bad Request" for every 400 (in our case: a key provisioned for Groww's "Approval" auth flow
instead of "TOTP" - the real body was {"error": {"errorMessage": "Invalid type provided"}}).
Re-implementing this one call keeps login errors diagnosable; get_groww_client() below still
uses the real SDK class since that path doesn't touch this bug.

Groww issues two different kinds of API key, chosen at key-creation time on their site, and a
key only works with the matching flow (confirmed live, 2026-07-24 - a key created for one flow
gets "Invalid type provided" when the other flow is used):
  - "TOTP" key -> paired with a TOTP secret (base32), same shape as every other broker here.
    Fully unattended - the 6-digit code is computed locally, nothing to approve anywhere.
  - "Approval" key -> paired with a plain API secret string, used to compute a SHA-256 checksum
    of itself + the current unix timestamp (no TOTP involved). CORRECTION (confirmed live,
    2026-07-25): this was originally assumed to be just as unattended as TOTP since the checksum
    is computed locally with no SDK call to "approve" anything - that assumption was wrong. A
    real account got "Groww API Error 403: Session approval required before generating token"
    on this flow, meaning Groww's server does require an explicit approval step (in the Groww
    app) before an Approval-key session can mint a token - the name isn't just cosmetic. There's
    no SDK method for this (checked: no approve_session()-shaped call exists), so it has to be
    done manually in the app, which defeats unattended daily Auto-login for this key type. If
    fully unattended login matters, use a TOTP-type key instead - see README's Groww section.
get_access_token() takes whichever of totp_secret/api_secret the account was set up with.
"""
import hashlib
import time
import uuid

import pyotp
import requests
from growwapi import GrowwAPI

_TOKEN_URL = "https://api.groww.in/v1/token/api/access"


class GrowwLoginError(Exception):
    pass


def get_access_token(api_key: str, totp_secret: str = "", api_secret: str = "") -> str:
    """Generates a fresh access token - only needs calling once per account (see module
    docstring), not per action. Exactly one of totp_secret/api_secret must be given, matching
    whichever key type was created on Groww's site (see module docstring)."""
    if bool(totp_secret) == bool(api_secret):
        raise GrowwLoginError("Provide exactly one of a TOTP secret or an API secret, matching the key type.")

    if totp_secret:
        try:
            totp = pyotp.TOTP(totp_secret).now()
        except Exception as e:  # noqa: BLE001 - bad totp secret (not base32, etc.)
            raise GrowwLoginError(str(e))
        payload = {"key_type": "totp", "totp": totp}
    else:
        timestamp = int(time.time())
        checksum = hashlib.sha256((api_secret + str(timestamp)).encode("utf-8")).hexdigest()
        payload = {"key_type": "approval", "checksum": checksum, "timestamp": timestamp}

    headers = {
        "x-request-id": str(uuid.uuid4()),
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json",
        "x-client-id": "growwapi",
        "x-client-platform": "growwapi-python-client",
        "x-client-platform-version": "1.5.0",
        "x-api-version": "1.0",
    }
    try:
        response = requests.post(_TOKEN_URL, headers=headers, json=payload, timeout=15)
    except Exception as e:  # noqa: BLE001 - network failure
        raise GrowwLoginError(str(e))

    if response.ok:
        return response.json()["token"]

    try:
        error = response.json().get("error", {}) or {}
        msg = error.get("errorMessage") or error.get("displayMessage") or response.text
    except Exception:  # noqa: BLE001 - non-JSON body
        msg = response.text or f"HTTP {response.status_code}"
    raise GrowwLoginError(f"Groww API Error {response.status_code}: {msg}")


def get_groww_client(access_token: str) -> GrowwAPI:
    """Reconstructs a client from an already-issued access token - no network call, no TOTP
    needed. Used for every action other than the one-time token generation above."""
    return GrowwAPI(access_token)
