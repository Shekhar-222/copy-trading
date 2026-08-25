import os
import hmac
import asyncio
import datetime
from typing import Optional, List

import requests
from fastapi import FastAPI, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import models
import crypto_utils
from database import engine, get_db, Base, SessionLocal, backup_sqlite_db
from kite_auth import auto_generate_request_token, generate_access_token, get_kite_client, LoginError
from kotak_auth import get_kotak_client, KotakLoginError
import kotak_client
import angel_auth
from angel_auth import AngelLoginError
import angel_client
import groww_auth
from groww_auth import GrowwLoginError
import groww_client
from ticker_listener import start_master_listener, stop_master_listener, is_running
from replication_engine import exit_account
from pnl import token_is_fresh, compute_pnl
import trade_log

load_dotenv()
Base.metadata.create_all(bind=engine)


def _run_light_migrations():
    """create_all() only creates missing tables, not missing columns on existing
    ones - patch older DBs in place for newly added columns."""
    inspector = inspect(engine)
    if "accounts" not in inspector.get_table_names():
        return
    existing_cols = {c["name"] for c in inspector.get_columns("accounts")}
    if "real_name" not in existing_cols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN real_name VARCHAR")
    if "broker" not in existing_cols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN broker VARCHAR DEFAULT 'zerodha'")
            conn.exec_driver_sql("UPDATE accounts SET broker = 'zerodha' WHERE broker IS NULL")
    if "mpin_enc" not in existing_cols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN mpin_enc TEXT")
    if "mobile_number" not in existing_cols:
        with engine.begin() as conn:
            conn.exec_driver_sql("ALTER TABLE accounts ADD COLUMN mobile_number VARCHAR")

    if "mirrored_orders" in inspector.get_table_names():
        existing_mirror_cols = {c["name"] for c in inspector.get_columns("mirrored_orders")}
        if "variety" not in existing_mirror_cols:
            with engine.begin() as conn:
                conn.exec_driver_sql("ALTER TABLE mirrored_orders ADD COLUMN variety VARCHAR DEFAULT 'regular'")
                conn.exec_driver_sql("UPDATE mirrored_orders SET variety = 'regular' WHERE variety IS NULL")


_run_light_migrations()

app = FastAPI(title="Zerodha Copy Trading API")

_allowed_origins = [
    o.strip() for o in os.getenv("FRONTEND_ORIGIN", "http://localhost:5173").split(",") if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- Access gate ----
# This app moves real money (order placement, position square-off) and has no per-user
# accounts - it's built for one operator. APP_ACCESS_TOKEN is a single shared secret that
# gates every request once this is deployed somewhere reachable off localhost. Left unset,
# the app behaves exactly as before (open, for local-only use) - it's on the operator to set
# this before exposing the API publicly.
APP_ACCESS_TOKEN = os.getenv("APP_ACCESS_TOKEN")
_PUBLIC_PATHS = {"/", "/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def _token_matches(candidate: Optional[str]) -> bool:
    return bool(candidate) and hmac.compare_digest(candidate, APP_ACCESS_TOKEN)


@app.middleware("http")
async def require_access_token(request: Request, call_next):
    if not APP_ACCESS_TOKEN or request.method == "OPTIONS" or request.url.path in _PUBLIC_PATHS:
        return await call_next(request)
    header = request.headers.get("x-access-token")
    if not _token_matches(header):
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

# ---- WebSocket broadcast hub (pushes live trade-log / status events to the dashboard) ----
_ws_clients: List[WebSocket] = []
_loop: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def on_startup():
    global _loop
    _loop = asyncio.get_event_loop()

    try:
        backup_sqlite_db()
    except Exception:  # noqa: BLE001 - a failed backup must never block startup
        pass

    # Resume order-update listeners for masters that were already active before a restart -
    # otherwise replication silently stops until someone manually toggles the account off/on.
    db = SessionLocal()
    try:
        masters = (
            db.query(models.Account)
            .filter(models.Account.role == "master", models.Account.active == True)  # noqa: E712
            .all()
        )
        for m in masters:
            if token_is_fresh(m):
                start_master_listener(m.id, broadcast=broadcast)

        # The Telegram digest loop's "has an order been punched" state lives in memory (see
        # telegram_notify.py) and resets on every restart. Without this, a restart mid-session
        # would silently go quiet until the next brand-new order, even though today's
        # already-placed orders are sitting right there - re-arm immediately if there's any,
        # so the very first digest after a restart still reports on them.
        first_today = (
            db.query(models.TradeLog)
            .filter(models.TradeLog.timestamp >= trade_log.today_ist_start_utc())
            .order_by(models.TradeLog.id.asc())
            .first()
        )
        if first_today:
            import telegram_notify
            telegram_notify.ensure_running(first_today.id)
    finally:
        db.close()


def broadcast(payload: dict):
    """Thread-safe broadcast, callable from the KiteTicker background thread."""
    if _loop is None:
        return
    asyncio.run_coroutine_threadsafe(_broadcast_async(payload), _loop)


async def _broadcast_async(payload: dict):
    dead = []
    for ws in _ws_clients:
        try:
            await ws.send_json(payload)
        except Exception:
            dead.append(ws)
    for d in dead:
        _ws_clients.remove(d)


@app.websocket("/ws/live")
async def live_updates(ws: WebSocket):
    await ws.accept()
    if APP_ACCESS_TOKEN:
        # Browsers can't set custom headers on a WebSocket handshake, so the token travels as
        # the first message instead of the X-Access-Token header the HTTP middleware checks -
        # deliberately NOT a query param, since uvicorn's default access log writes the full
        # request path (including query string) to stdout on every connection, which would
        # leak the token in plaintext to whatever's reading the process logs.
        try:
            first_message = await asyncio.wait_for(ws.receive_text(), timeout=10)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            await ws.close(code=4401)
            return
        if not _token_matches(first_message):
            await ws.close(code=4401)
            return
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # we don't expect further client messages, just keep the connection open
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------- Schemas ----------------------------
class AccountCreate(BaseModel):
    label: str
    role: str  # "master" or "child"
    broker: str = "zerodha"  # "zerodha", "kotak_neo", "angel_one", or "groww"
    client_id: str  # Kite client id, Kotak Neo UCC, Angel One client code, or Groww UCC (optional/informational for Groww)
    api_key: str  # Kite api_key, Kotak Neo consumer_key, Angel One SmartAPI api_key, or Groww API key
    api_secret: Optional[str] = None  # Kite api_secret (required for zerodha, unused otherwise)
    password: Optional[str] = None  # Zerodha login password - only used for zerodha auto-login
    totp_secret: str
    mpin: Optional[str] = None  # Kotak Neo MPIN / Angel One trading PIN - required for those brokers
    mobile_number: Optional[str] = None  # Kotak Neo registered mobile number - required for kotak_neo only
    multiplier_override: Optional[float] = None


class AccountOut(BaseModel):
    id: int
    label: str
    role: str
    broker: str
    client_id: str
    real_name: Optional[str] = None
    capital: float
    multiplier_override: Optional[float]
    active: bool
    token_generated_at: Optional[datetime.datetime]
    has_token_today: bool

    class Config:
        from_attributes = True


class AccountUpdate(BaseModel):
    """All fields optional - only what's sent gets changed, everything else is left as-is.
    Broker and role aren't editable here (role has its own endpoint; broker isn't switchable at
    all - see switch_role)."""
    label: Optional[str] = None
    client_id: Optional[str] = None
    api_key: Optional[str] = None
    api_secret: Optional[str] = None
    password: Optional[str] = None
    totp_secret: Optional[str] = None
    mpin: Optional[str] = None
    mobile_number: Optional[str] = None


class ManualTokenIn(BaseModel):
    request_token: str


class MultiplierIn(BaseModel):
    multiplier_override: Optional[float] = None


class RoleIn(BaseModel):
    role: str  # "master" or "child"


# ---------------------------- Helpers ----------------------------
def _to_out(acc: models.Account) -> dict:
    return {
        "id": acc.id,
        "label": acc.label,
        "role": acc.role,
        "broker": acc.broker,
        "client_id": acc.client_id,
        "real_name": acc.real_name,
        "capital": acc.capital,
        "multiplier_override": acc.multiplier_override,
        "active": acc.active,
        "token_generated_at": acc.token_generated_at,
        "has_token_today": token_is_fresh(acc),
    }


# ---------------------------- Account CRUD ----------------------------
@app.post("/accounts", response_model=AccountOut)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    if payload.role not in ("master", "child"):
        raise HTTPException(400, "role must be 'master' or 'child'")
    if payload.broker not in ("zerodha", "kotak_neo", "angel_one", "groww"):
        raise HTTPException(400, "broker must be 'zerodha', 'kotak_neo', 'angel_one', or 'groww'")
    if payload.broker == "kotak_neo":
        if payload.role != "child":
            raise HTTPException(400, "Kotak Neo accounts can only be added as child accounts for now.")
        if not payload.mpin or not payload.mobile_number:
            raise HTTPException(400, "mpin and mobile_number are required for Kotak Neo accounts.")
    elif payload.broker == "angel_one":
        if payload.role != "child":
            raise HTTPException(400, "Angel One accounts can only be added as child accounts for now.")
        if not payload.mpin:
            raise HTTPException(400, "mpin (trading PIN) is required for Angel One accounts.")
    elif payload.broker == "groww":
        if payload.role != "child":
            raise HTTPException(400, "Groww accounts can only be added as child accounts for now.")
        if bool(payload.totp_secret) == bool(payload.api_secret):
            raise HTTPException(400, "Provide exactly one of totp_secret or api_secret for Groww, matching the API key's type (TOTP or Approval).")
    else:
        if not payload.api_secret:
            raise HTTPException(400, "api_secret is required for Zerodha accounts.")

    # Stripped: these are typically copy-pasted from a broker's app/website, and a stray
    # leading/trailing space (e.g. from a triple-click copy) is invisible in the input box but
    # breaks things downstream - a totp_secret with a trailing space fails pyotp's base32
    # decode outright ("Non-base32 digit found"), confirmed against a real Angel One account.
    # password isn't stripped since it's typically typed, not pasted, and unlike an API
    # key/secret a password is occasionally intentionally whitespace-containing.
    acc = models.Account(
        label=payload.label,
        role=payload.role,
        broker=payload.broker,
        client_id=payload.client_id.strip(),
        api_key_enc=crypto_utils.encrypt(payload.api_key.strip()),
        api_secret_enc=crypto_utils.encrypt((payload.api_secret or "").strip()),
        password_enc=crypto_utils.encrypt(payload.password) if payload.password else None,
        totp_secret_enc=crypto_utils.encrypt(payload.totp_secret.strip()),
        mpin_enc=crypto_utils.encrypt(payload.mpin.strip()) if payload.mpin else None,
        mobile_number=payload.mobile_number.strip() if payload.mobile_number else None,
        multiplier_override=payload.multiplier_override,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


def _invalidate_token(acc: models.Account) -> None:
    """Clears the stored access token, forcing a fresh Auto-login/manual token before the
    account trades again. Also stops a live master listener - it would otherwise keep running
    against a now-discarded token until it happens to reconnect."""
    acc.access_token_enc = None
    acc.token_generated_at = None
    if acc.role == "master":
        stop_master_listener(acc.id)


@app.patch("/accounts/{account_id}")
def update_account(account_id: int, payload: AccountUpdate, db: Session = Depends(get_db)):
    """Edits an existing account's label/credentials in place, so a typo'd or outdated
    credential doesn't require deleting and re-adding the whole account. Any credential field
    left blank in the request is left untouched. Changing any credential invalidates the stored
    access token (forces a fresh login) - continuing to use a token generated under
    now-discarded credentials would be silently wrong, not just stale."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if acc.broker == "groww" and payload.totp_secret and payload.api_secret:
        raise HTTPException(400, "Provide only one of totp_secret or api_secret for Groww, matching the API key's type - not both.")

    credential_touched = False
    if payload.label:
        acc.label = payload.label
    if payload.client_id:
        acc.client_id = payload.client_id.strip()
    if payload.api_key:
        acc.api_key_enc = crypto_utils.encrypt(payload.api_key.strip())
        credential_touched = True
    if payload.api_secret:
        acc.api_secret_enc = crypto_utils.encrypt(payload.api_secret.strip())
        credential_touched = True
        if acc.broker == "groww":
            # Groww's two key types are mutually exclusive (see groww_auth.py) - setting one
            # secret must clear the other, or a leftover from account creation makes
            # get_access_token() reject both being present.
            acc.totp_secret_enc = crypto_utils.encrypt("")
    if payload.password:
        acc.password_enc = crypto_utils.encrypt(payload.password)
        credential_touched = True
    if payload.totp_secret:
        acc.totp_secret_enc = crypto_utils.encrypt(payload.totp_secret.strip())
        credential_touched = True
        if acc.broker == "groww":
            acc.api_secret_enc = crypto_utils.encrypt("")
    if payload.mpin:
        acc.mpin_enc = crypto_utils.encrypt(payload.mpin.strip())
        credential_touched = True
    if payload.mobile_number:
        acc.mobile_number = payload.mobile_number.strip()

    if credential_touched:
        _invalidate_token(acc)

    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@app.get("/accounts", response_model=List[AccountOut])
def list_accounts(db: Session = Depends(get_db)):
    return [_to_out(a) for a in db.query(models.Account).all()]


@app.delete("/accounts/{account_id}")
def delete_account(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if acc.role == "master":
        stop_master_listener(acc.id)
    db.delete(acc)
    db.commit()
    return {"ok": True}


@app.patch("/accounts/{account_id}/multiplier")
def set_multiplier(account_id: int, payload: MultiplierIn, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    acc.multiplier_override = payload.multiplier_override
    db.commit()
    return _to_out(acc)


@app.patch("/accounts/{account_id}/role")
def switch_role(account_id: int, payload: RoleIn, db: Session = Depends(get_db)):
    """Switches an existing account between master and child. Kotak Neo, Angel One, and Groww
    accounts can't become a master (the order-update feed that drives replication only exists
    for Zerodha) and an account must be toggled off/excluded first, so a live listener or
    in-flight copying never gets yanked out from under it mid-switch."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if payload.role not in ("master", "child"):
        raise HTTPException(400, "role must be 'master' or 'child'")
    if payload.role == acc.role:
        return _to_out(acc)
    if acc.active:
        raise HTTPException(400, "Stop trading / exclude this account before switching its role.")
    if payload.role == "master" and acc.broker in ("kotak_neo", "angel_one", "groww"):
        raise HTTPException(400, "Kotak Neo, Angel One, and Groww accounts can only be children - the master must be Zerodha.")

    if acc.role == "master":
        stop_master_listener(acc.id)
    acc.role = payload.role
    db.commit()
    return _to_out(acc)


@app.patch("/accounts/{account_id}/toggle")
def toggle_active(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    acc.active = not acc.active
    db.commit()
    if acc.role == "master":
        if acc.active and token_is_fresh(acc):
            start_master_listener(acc.id, broadcast=broadcast)
        else:
            stop_master_listener(acc.id)
    return _to_out(acc)


# ---------------------------- Daily login / token ----------------------------
@app.post("/accounts/{account_id}/auto-login")
def auto_login(account_id: int, db: Session = Depends(get_db)):
    """Zerodha: attempts fully automated login using stored password + TOTP secret, storing a
    daily access token. Angel One: logs in fresh with TOTP + PIN once and stores the resulting
    access/refresh token pair the same way (see angel_auth.py) - every other Angel One action
    reuses those stored tokens rather than logging in again. Groww: logs in fresh with TOTP once
    too, but unlike Angel the resulting token is documented as never expiring (see
    groww_auth.py) - clicking this again later just regenerates a (still non-expiring) token,
    it's never required daily the way Zerodha/Angel are. Kotak Neo: logs in fresh with TOTP +
    MPIN on every action instead (see kotak_auth.py), since its SDK has no equivalent
    stored-token reattachment. All paths finish by refreshing capital and, for a Zerodha
    master, starting the order-update listener."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")

    if acc.broker == "kotak_neo":
        try:
            get_kotak_client(
                consumer_key=crypto_utils.decrypt(acc.api_key_enc),
                mobile_number=acc.mobile_number,
                ucc=acc.client_id,
                totp_secret=crypto_utils.decrypt(acc.totp_secret_enc),
                mpin=crypto_utils.decrypt(acc.mpin_enc),
            )
        except KotakLoginError as e:
            raise HTTPException(400, f"Kotak Neo login failed: {e}")

        acc.access_token_enc = crypto_utils.encrypt("kotak-neo-verified")
        acc.token_generated_at = datetime.datetime.utcnow()
        db.commit()
        _refresh_capital(acc, db)
        return _to_out(acc)

    if acc.broker == "angel_one":
        # Logs in fresh exactly once here (the only place angel_auth.login() is called) and
        # persists the resulting access/refresh token pair - every other Angel One action
        # (including the _refresh_capital call right below) reconstructs a client from these
        # stored tokens instead of logging in again, since Angel's login endpoint is rate-
        # limited tightly enough that repeated fresh logins get rejected (see angel_auth.py).
        try:
            token_data = angel_auth.login(
                api_key=crypto_utils.decrypt(acc.api_key_enc),
                client_id=acc.client_id,
                totp_secret=crypto_utils.decrypt(acc.totp_secret_enc),
                pin=crypto_utils.decrypt(acc.mpin_enc),
            )
        except AngelLoginError as e:
            raise HTTPException(400, f"Angel One login failed: {e}")

        # generateSession's jwtToken comes back already prefixed "Bearer " (confirmed live) -
        # stored stripped so it's clean at rest; angel_auth.get_angel_client also strips
        # defensively on the read side, so this isn't the only thing standing between a bad
        # token and every subsequent call failing with "Invalid Token".
        jwt_token = token_data.get("jwtToken", "")
        if jwt_token.strip().lower().startswith("bearer "):
            jwt_token = jwt_token.strip()[len("bearer "):].strip()
        acc.access_token_enc = crypto_utils.encrypt(jwt_token)
        acc.api_secret_enc = crypto_utils.encrypt(token_data.get("refreshToken", ""))
        acc.token_generated_at = datetime.datetime.utcnow()
        db.commit()
        _refresh_capital(acc, db)
        return _to_out(acc)

    if acc.broker == "groww":
        try:
            access_token = groww_auth.get_access_token(
                api_key=crypto_utils.decrypt(acc.api_key_enc),
                totp_secret=crypto_utils.decrypt(acc.totp_secret_enc),
                api_secret=crypto_utils.decrypt(acc.api_secret_enc) if acc.api_secret_enc else "",
            )
        except GrowwLoginError as e:
            raise HTTPException(400, f"Groww login failed: {e}")

        acc.access_token_enc = crypto_utils.encrypt(access_token)
        acc.token_generated_at = datetime.datetime.utcnow()
        db.commit()
        _refresh_capital(acc, db)
        return _to_out(acc)

    if not acc.password_enc:
        raise HTTPException(400, "No password stored for this account - use manual token login instead.")

    try:
        request_token = auto_generate_request_token(
            client_id=acc.client_id,
            password=crypto_utils.decrypt(acc.password_enc),
            totp_secret=crypto_utils.decrypt(acc.totp_secret_enc),
            api_key=crypto_utils.decrypt(acc.api_key_enc),
        )
        access_token = generate_access_token(
            api_key=crypto_utils.decrypt(acc.api_key_enc),
            api_secret=crypto_utils.decrypt(acc.api_secret_enc),
            request_token=request_token,
            expected_client_id=acc.client_id,
        )
    except LoginError as e:
        raise HTTPException(400, f"Auto-login failed: {e}. Try the manual token method.")

    acc.access_token_enc = crypto_utils.encrypt(access_token)
    acc.token_generated_at = datetime.datetime.utcnow()
    db.commit()

    _refresh_capital(acc, db)

    if acc.role == "master" and acc.active:
        start_master_listener(acc.id, broadcast=broadcast)

    return _to_out(acc)


@app.get("/accounts/{account_id}/login-url")
def get_login_url(account_id: int, db: Session = Depends(get_db)):
    """Returns the URL you should open in a browser, log in manually, then copy the
    request_token from the redirected URL and POST it to /accounts/{id}/manual-token.
    Zerodha only - Kotak Neo's, Angel One's, and Groww's TOTP-based logins have no
    manual/browser step, use auto-login."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if acc.broker in ("kotak_neo", "angel_one", "groww"):
        raise HTTPException(400, "This broker doesn't use a manual-token login flow - use auto-login instead.")
    api_key = crypto_utils.decrypt(acc.api_key_enc)
    return {"login_url": f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"}


@app.post("/accounts/{account_id}/manual-token")
def manual_token(account_id: int, payload: ManualTokenIn, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if acc.broker in ("kotak_neo", "angel_one", "groww"):
        raise HTTPException(400, "This broker doesn't use a manual-token login flow - use auto-login instead.")
    try:
        access_token = generate_access_token(
            api_key=crypto_utils.decrypt(acc.api_key_enc),
            api_secret=crypto_utils.decrypt(acc.api_secret_enc),
            request_token=payload.request_token,
            expected_client_id=acc.client_id,
        )
    except Exception as e:
        raise HTTPException(400, f"Token exchange failed: {e}")

    acc.access_token_enc = crypto_utils.encrypt(access_token)
    acc.token_generated_at = datetime.datetime.utcnow()
    db.commit()

    _refresh_capital(acc, db)

    if acc.role == "master" and acc.active:
        start_master_listener(acc.id, broadcast=broadcast)

    return _to_out(acc)


@app.post("/accounts/{account_id}/logout")
def logout(account_id: int, db: Session = Depends(get_db)):
    """Clears the stored access token so the account goes back to NO TOKEN and needs a fresh
    Auto-login/manual token before trading again. Mainly useful for Groww/Angel, whose tokens
    don't expire on their own the way Zerodha/Kotak's daily tokens do - see pnl.token_is_fresh."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    _invalidate_token(acc)
    db.commit()
    db.refresh(acc)
    return _to_out(acc)


@app.post("/accounts/{account_id}/refresh-capital")
def refresh_capital(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if not token_is_fresh(acc):
        raise HTTPException(400, "No valid token for today - log in first.")
    _refresh_capital(acc, db)
    return _to_out(acc)


def _refresh_capital(acc: models.Account, db: Session):
    if acc.broker == "kotak_neo":
        acc.capital = kotak_client.get_margin(acc)
        try:
            acc.real_name = kotak_client.get_profile_name(acc)
        except Exception:
            pass
        db.commit()
        return

    if acc.broker == "angel_one":
        acc.capital = angel_client.get_margin(acc)
        try:
            acc.real_name = angel_client.get_profile_name(acc)
        except Exception:
            pass
        db.commit()
        return

    if acc.broker == "groww":
        acc.capital = groww_client.get_margin(acc)
        try:
            acc.real_name = groww_client.get_profile_name(acc)
        except Exception:
            pass
        db.commit()
        return

    kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
    margins = kite.margins()
    # "net" (cash + collateral - utilised) reflects true usable capital, including pledged-stock
    # collateral. "live_balance" only counts cash and ignores collateral entirely - for a
    # collateral-funded account it can show a small or negative number even when the account has
    # lakhs of real usable margin, which makes it a poor basis for proportional position sizing.
    acc.capital = margins.get("equity", {}).get("net", 0.0)
    try:
        profile = kite.profile()
        acc.real_name = profile.get("user_name") or profile.get("user_shortname")
    except Exception:
        pass
    db.commit()


@app.post("/accounts/{account_id}/exit")
def exit_positions(account_id: int, db: Session = Depends(get_db)):
    """Cancels every pending order and squares off every open position on this one account.
    For Kotak Neo, Angel One, and Groww accounts, only pending-order cancellation is automated
    for now - see kotak_client.exit_account / angel_client.exit_account / groww_client.exit_account."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if not token_is_fresh(acc):
        raise HTTPException(400, "No valid token for today - log in first.")
    results = exit_account(db, acc, broadcast=broadcast)
    return {"results": results}


@app.get("/accounts/{account_id}/positions")
def get_positions(account_id: int, db: Session = Depends(get_db)):
    """Both open and closed (squared-off today) positions for one account, fetched fresh on
    demand - not polled automatically, since the dashboard only needs this when a card's
    positions panel is expanded. "status" is "OPEN" for a non-zero net quantity, "CLOSED" for
    a position that was fully squared off today (net quantity 0, but still carries realized
    P&L worth showing)."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if not token_is_fresh(acc):
        return []
    try:
        if acc.broker == "kotak_neo":
            return kotak_client.get_positions(acc)
        if acc.broker == "angel_one":
            return angel_client.get_positions(acc)
        if acc.broker == "groww":
            return groww_client.get_positions(acc)
        kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
        positions = kite.positions().get("net", [])
        return [
            {
                "tradingsymbol": p.get("tradingsymbol"),
                "exchange": p.get("exchange"),
                "quantity": p.get("quantity"),
                "average_price": p.get("average_price"),
                "pnl": p.get("pnl"),
                "product": p.get("product"),
                "status": "OPEN" if p.get("quantity", 0) != 0 else "CLOSED",
            }
            for p in positions
        ]
    except Exception:
        return []


# ---------------------------- Trade logs ----------------------------
@app.get("/logs")
def get_logs(limit: int = 100, db: Session = Depends(get_db)):
    """Scoped to today's (IST) trades only, so the dashboard's live feed naturally clears each
    day - older rows aren't deleted, just left out of this view."""
    logs = (
        db.query(models.TradeLog)
        .filter(models.TradeLog.timestamp >= trade_log.today_ist_start_utc())
        .order_by(models.TradeLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    # One query for all labels instead of one per log row (this endpoint is polled by the
    # dashboard, so an N+1 here multiplies quickly).
    labels = dict(db.query(models.Account.id, models.Account.label).all())
    out = []
    for log in logs:
        out.append(
            {
                "id": log.id,
                # naive UTC (see trade_log.to_dict) - append "Z" so the frontend parses it as
                # UTC instead of misreading it as already being local time.
                "timestamp": log.timestamp.isoformat() + "Z",
                "child_account": labels.get(log.child_account_id),
                "tradingsymbol": log.tradingsymbol,
                "exchange": log.exchange,
                "transaction_type": log.transaction_type,
                "master_quantity": log.master_quantity,
                "replicated_quantity": log.replicated_quantity,
                "status": log.status,
                "message": log.message,
            }
        )
    return out


@app.get("/pnl")
def get_pnl(db: Session = Depends(get_db)):
    return compute_pnl(db)


# ---------------------------- Index ticker ----------------------------
_TICKER_INSTRUMENTS = [
    ("NIFTY 50", "NSE:NIFTY 50"),
    ("BANKNIFTY", "NSE:NIFTY BANK"),
    ("SENSEX", "BSE:SENSEX"),
    ("INDIA VIX", "NSE:INDIA VIX"),
]
_TICKER_CACHE_TTL = datetime.timedelta(seconds=5)
_ticker_cache = {"at": None, "data": None}


@app.get("/ticker")
def get_ticker(db: Session = Depends(get_db)):
    """Live index prices for the dashboard's scrolling ticker tape, fetched through any
    logged-in Zerodha account's quote API (master preferred) - no separate market-data
    subscription needed. Cached briefly server-side since the frontend polls this and
    Kite rate-limits quote calls. Returns {"indices": []} when no Zerodha account has a
    fresh token yet (the frontend simply hides the tape)."""
    now = datetime.datetime.utcnow()
    if _ticker_cache["at"] and now - _ticker_cache["at"] < _TICKER_CACHE_TTL:
        return _ticker_cache["data"]

    acc = next(
        (a for a in db.query(models.Account).order_by(models.Account.role.desc()).all()
         if a.broker == "zerodha" and token_is_fresh(a)),
        None,
    )
    if not acc:
        return {"indices": []}

    try:
        kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
        quotes = kite.ohlc([key for _, key in _TICKER_INSTRUMENTS])
        indices = []
        for name, key in _TICKER_INSTRUMENTS:
            q = quotes.get(key)
            if not q:
                continue
            last = q.get("last_price") or 0.0
            prev_close = (q.get("ohlc") or {}).get("close") or 0.0
            change = last - prev_close
            indices.append({
                "symbol": name,
                "last_price": round(last, 2),
                "change": round(change, 2),
                "change_pct": round((change / prev_close) * 100, 2) if prev_close else 0.0,
            })
        data = {"indices": indices}
        _ticker_cache["at"], _ticker_cache["data"] = now, data
        return data
    except Exception:  # noqa: BLE001 - a quote hiccup must never break the dashboard
        return _ticker_cache["data"] or {"indices": []}


@app.get("/status")
def status(db: Session = Depends(get_db)):
    masters = db.query(models.Account).filter(models.Account.role == "master").all()
    return {"masters": [{"id": m.id, "label": m.label, "listening": is_running(m.id)} for m in masters]}


_ip_cache = {"ip": None, "at": None}


@app.get("/system/ip")
def get_public_ip(force: bool = False):
    """
    The server's outbound public IPv4 - the address to whitelist with brokers like Kotak Neo
    that require it (kotak_auth.py forces IPv4-egress process-wide, so this matches what
    actually leaves the box). Cached briefly to avoid hammering the external lookup service on
    every dashboard poll, but short enough that a network change (new WiFi, VPN toggle) shows up
    quickly; ?force=true bypasses the cache entirely for an explicit manual refresh.
    """
    now = datetime.datetime.utcnow()
    if not force and _ip_cache["ip"] and (now - _ip_cache["at"]).total_seconds() < 30:
        return {"ip": _ip_cache["ip"]}
    try:
        resp = requests.get("https://api.ipify.org?format=json", timeout=5)
        resp.raise_for_status()
        ip = resp.json()["ip"]
        _ip_cache["ip"] = ip
        _ip_cache["at"] = now
        return {"ip": ip}
    except Exception:
        if _ip_cache["ip"]:
            return {"ip": _ip_cache["ip"]}
        raise HTTPException(status_code=502, detail="Could not determine public IP")


@app.get("/")
def root():
    return {"status": "ok", "service": "zerodha-copy-trading-backend"}
