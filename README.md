# Zerodha Copy Trader

Copies trades from one "master" Zerodha account onto 2–3 "child" accounts in real time,
sizing each child's order proportionally to its available capital relative to the master's.

## How it works

1. The backend opens a Kite Ticker WebSocket on your **master** account and listens for order updates.
2. When a master order reaches `COMPLETE`, the replication engine computes each active child's
   quantity as `master_qty × (child_capital / master_capital)`, rounded down to the nearest lot
   (or uses a manual multiplier you set per child instead).
3. It places a matching MARKET order on each child account via the official Kite Connect API and
   logs the result, which streams live to the dashboard over its own WebSocket.

## 1. Prerequisites

- A **Kite Connect developer app** for *each* account (master + every child) at
  https://developers.kite.trade — ₹2000/month per app, billed by Zerodha. You need each account's
  API key and API secret.
- Each account's **TOTP secret** — the base32 string you got when you set up TOTP-based 2FA on Kite
  (not the 6-digit code itself, the secret used to generate it). If you use SMS/app-based 2FA
  instead of TOTP, you'll need to switch to TOTP in Kite's security settings first, since that's
  what the automated login uses.
- Python 3.10+ and Node.js 18+.

## 2. Backend setup

```bash
cd backend
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# paste the printed key into .env as ENCRYPTION_KEY

uvicorn main:app --reload --port 8000
```

The first run creates `copytrader.db` (SQLite) in the `backend/` folder automatically.

## 3. Frontend setup

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173.

## 4. Add your accounts

In the dashboard, click **Add account** once for your master account and once per child (2–3 in
your case). You'll need each account's client ID, API key, API secret, and TOTP secret. The login
password field is optional:

- **With password** → "Auto-login" logs in for you every morning (see security note below).
- **Without password** → use "Manual token": it gives you a login link, you log in in the browser
  once, then paste the `request_token` from the redirected URL back into the dashboard. Kite
  tokens expire daily, so this is a once-a-day, ~30-second task per account if you skip auto-login.

After logging in, click **Refresh capital** is automatic on login — it fetches each account's
available margin so the capital-ratio bars and multipliers are accurate. Re-run login each trading
day before market open.

Toggle a master to "Resume copying" once it has a fresh token to start the live listener. Toggle
child accounts to "Include" to have them participate — you can exclude one temporarily without
deleting it.

## 5. Order type & product

Kite Connect rejects plain MARKET orders placed via the API ("market orders without market
protection are not allowed"), and there's no API parameter to enable protection directly. To work
around this, replicated orders are placed as **LIMIT** orders priced a 0.5% buffer beyond the
current LTP in the trade's direction (`_protected_limit_price` in
`backend/replication_engine.py`) — this fills immediately like a market order under normal
liquidity while capping slippage. The buffer is controlled by `MARKET_PROTECTION_PCT` in that
file. Product type mirrors the master order (defaults to MIS if unspecified).

## Security notes — please read

- API secrets, TOTP secrets, and (if provided) your login password are encrypted at rest using the
  `ENCRYPTION_KEY` in `.env`, but this app has no user-login layer of its own — anyone with access
  to the machine or the dashboard URL can trade on all connected accounts. Only run this on a
  machine you control, and don't expose port 8000/5173 to the open internet without adding your
  own auth layer.
- The automated login (`kite_auth.py`) drives Zerodha's *web login pages*, not an official Kite
  Connect endpoint — it's the same technique many open-source Kite tools use, but Zerodha can
  change their login flow without notice, which would break auto-login until updated. The manual
  token method uses only the official `generate_session()` call and will keep working regardless.
- Keep `copytrader.db` and `.env` out of version control (already covered by the included
  `.gitignore`).

## 6. Known limitations / things to check before relying on this live

- Lot sizes for index F&O (NIFTY/BANKNIFTY) are hardcoded as a fallback guess in
  `replication_engine._guess_lot_size` and NSE revises these periodically — for anything beyond
  quick testing, wire in `kite.instruments()` to pull real lot sizes instead of trusting the guess.
- Only `COMPLETE` master orders are copied; partial fills, modifications, and cancellations on the
  master are not currently propagated to children — worth adding if your master strategy relies on
  partial exits or SL modifications.
- There's no retry/backoff on a failed child order (e.g. transient network blip) — it's logged as
  FAILED and the dashboard shows it, but nothing re-attempts automatically.
- Test everything in small size / paper first. This places real MARKET orders with real money the
  moment the master's order completes.

## Project structure

```
backend/
  main.py                # FastAPI app: REST routes + live WebSocket broadcast
  models.py               # Account + TradeLog tables (SQLAlchemy)
  database.py              # SQLite engine/session
  crypto_utils.py           # Fernet encryption for stored secrets
  kite_auth.py               # Daily access-token generation (auto + manual)
  replication_engine.py       # Proportional sizing + order placement
  ticker_listener.py           # KiteTicker order-update listener (master)
frontend/
  src/App.jsx              # Dashboard shell, live feed via WebSocket
  src/components/           # MasterCard, ChildCard, TradeFeed, modals
```
