import os
import asyncio
import datetime
from typing import Optional, List

from fastapi import FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.orm import Session
from dotenv import load_dotenv

import models
import crypto_utils
from database import engine, get_db, Base
from kite_auth import auto_generate_request_token, generate_access_token, get_kite_client, LoginError
from ticker_listener import start_master_listener, stop_master_listener, is_running

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


_run_light_migrations()

app = FastAPI(title="Zerodha Copy Trading API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_ORIGIN", "http://localhost:5173")],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- WebSocket broadcast hub (pushes live trade-log / status events to the dashboard) ----
_ws_clients: List[WebSocket] = []
_loop: Optional[asyncio.AbstractEventLoop] = None


@app.on_event("startup")
async def on_startup():
    global _loop
    _loop = asyncio.get_event_loop()


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
    _ws_clients.append(ws)
    try:
        while True:
            await ws.receive_text()  # we don't expect client messages, just keep the connection open
    except WebSocketDisconnect:
        if ws in _ws_clients:
            _ws_clients.remove(ws)


# ---------------------------- Schemas ----------------------------
class AccountCreate(BaseModel):
    label: str
    role: str  # "master" or "child"
    client_id: str
    api_key: str
    api_secret: str
    password: Optional[str] = None
    totp_secret: str
    multiplier_override: Optional[float] = None


class AccountOut(BaseModel):
    id: int
    label: str
    role: str
    client_id: str
    real_name: Optional[str] = None
    capital: float
    multiplier_override: Optional[float]
    active: bool
    token_generated_at: Optional[datetime.datetime]
    has_token_today: bool

    class Config:
        from_attributes = True


class ManualTokenIn(BaseModel):
    request_token: str


class MultiplierIn(BaseModel):
    multiplier_override: Optional[float] = None


# ---------------------------- Helpers ----------------------------
def _token_is_fresh(acc: models.Account) -> bool:
    if not acc.token_generated_at or not acc.access_token_enc:
        return False
    return acc.token_generated_at.date() == datetime.datetime.utcnow().date()


def _to_out(acc: models.Account) -> dict:
    return {
        "id": acc.id,
        "label": acc.label,
        "role": acc.role,
        "client_id": acc.client_id,
        "real_name": acc.real_name,
        "capital": acc.capital,
        "multiplier_override": acc.multiplier_override,
        "active": acc.active,
        "token_generated_at": acc.token_generated_at,
        "has_token_today": _token_is_fresh(acc),
    }


# ---------------------------- Account CRUD ----------------------------
@app.post("/accounts", response_model=AccountOut)
def create_account(payload: AccountCreate, db: Session = Depends(get_db)):
    if payload.role not in ("master", "child"):
        raise HTTPException(400, "role must be 'master' or 'child'")
    acc = models.Account(
        label=payload.label,
        role=payload.role,
        client_id=payload.client_id,
        api_key_enc=crypto_utils.encrypt(payload.api_key),
        api_secret_enc=crypto_utils.encrypt(payload.api_secret),
        password_enc=crypto_utils.encrypt(payload.password) if payload.password else None,
        totp_secret_enc=crypto_utils.encrypt(payload.totp_secret),
        multiplier_override=payload.multiplier_override,
    )
    db.add(acc)
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


@app.patch("/accounts/{account_id}/toggle")
def toggle_active(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    acc.active = not acc.active
    db.commit()
    if acc.role == "master":
        if acc.active and _token_is_fresh(acc):
            start_master_listener(acc.id, broadcast=broadcast)
        else:
            stop_master_listener(acc.id)
    return _to_out(acc)


# ---------------------------- Daily login / token ----------------------------
@app.post("/accounts/{account_id}/auto-login")
def auto_login(account_id: int, db: Session = Depends(get_db)):
    """Attempts fully automated login using stored password + TOTP secret."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
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
    request_token from the redirected URL and POST it to /accounts/{id}/manual-token."""
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    api_key = crypto_utils.decrypt(acc.api_key_enc)
    return {"login_url": f"https://kite.zerodha.com/connect/login?api_key={api_key}&v=3"}


@app.post("/accounts/{account_id}/manual-token")
def manual_token(account_id: int, payload: ManualTokenIn, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    try:
        access_token = generate_access_token(
            api_key=crypto_utils.decrypt(acc.api_key_enc),
            api_secret=crypto_utils.decrypt(acc.api_secret_enc),
            request_token=payload.request_token,
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


@app.post("/accounts/{account_id}/refresh-capital")
def refresh_capital(account_id: int, db: Session = Depends(get_db)):
    acc = db.query(models.Account).get(account_id)
    if not acc:
        raise HTTPException(404, "not found")
    if not _token_is_fresh(acc):
        raise HTTPException(400, "No valid token for today - log in first.")
    _refresh_capital(acc, db)
    return _to_out(acc)


def _refresh_capital(acc: models.Account, db: Session):
    kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
    margins = kite.margins()
    acc.capital = margins.get("equity", {}).get("available", {}).get("live_balance", 0.0)
    try:
        profile = kite.profile()
        acc.real_name = profile.get("user_name") or profile.get("user_shortname")
    except Exception:
        pass
    db.commit()


# ---------------------------- Trade logs ----------------------------
@app.get("/logs")
def get_logs(limit: int = 100, db: Session = Depends(get_db)):
    logs = (
        db.query(models.TradeLog)
        .order_by(models.TradeLog.timestamp.desc())
        .limit(limit)
        .all()
    )
    out = []
    for log in logs:
        child = db.query(models.Account).get(log.child_account_id) if log.child_account_id else None
        out.append(
            {
                "id": log.id,
                "timestamp": log.timestamp,
                "child_account": child.label if child else None,
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
    """Live running P&L per account, pulled from Kite's open positions."""
    accounts = db.query(models.Account).all()
    out = []
    total = 0.0
    for acc in accounts:
        pnl = None
        if _token_is_fresh(acc):
            try:
                kite = get_kite_client(crypto_utils.decrypt(acc.api_key_enc), crypto_utils.decrypt(acc.access_token_enc))
                positions = kite.positions()
                pnl = sum(p.get("pnl", 0.0) for p in positions.get("net", []))
            except Exception:
                pnl = None
        out.append({"id": acc.id, "role": acc.role, "pnl": pnl})
        if pnl is not None:
            total += pnl
    return {"accounts": out, "total": total}


@app.get("/status")
def status(db: Session = Depends(get_db)):
    masters = db.query(models.Account).filter(models.Account.role == "master").all()
    return {"masters": [{"id": m.id, "label": m.label, "listening": is_running(m.id)} for m in masters]}


@app.get("/")
def root():
    return {"status": "ok", "service": "zerodha-copy-trading-backend"}
