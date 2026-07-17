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

**Zerodha children**: Kite Connect rejects plain MARKET orders placed via the API ("market orders
without market protection are not allowed"), and there's no API parameter to enable protection
directly. To work around this, replicated orders are placed as **LIMIT** orders priced a 0.5%
buffer beyond the current LTP in the trade's direction (`_protected_limit_price` in
`backend/replication_engine.py`) — this fills immediately like a market order under normal
liquidity while capping slippage. The buffer is controlled by `MARKET_PROTECTION_PCT` in that file.

**Kotak Neo children**: Kotak's API natively supports market-protected market orders via a
`market_protection` parameter, so these are placed as `MKT` orders with that set (same 0.5%
default, `MARKET_PROTECTION_PCT` in `backend/kotak_client.py`) rather than needing the LIMIT-price
workaround.

**Angel One children**: whether Angel's SmartAPI rejects a plain MARKET order the way Kite's does
isn't verified from here, so these use the same protected-LIMIT-price workaround as Zerodha
children (same 0.5% default, `MARKET_PROTECTION_PCT` in `backend/angel_client.py`) rather than
risk placing an unprotected order.

Product type mirrors the master order (defaults to MIS if unspecified) for all three brokers.

## Security notes — please read

- API secrets, TOTP secrets, and (if provided) your login password are encrypted at rest using the
  `ENCRYPTION_KEY` in `.env`, but this app has no per-user accounts of its own — anyone who reaches
  the dashboard URL can trade on all connected accounts. For local-only use this is fine (only you
  can reach `localhost`). If you deploy it somewhere reachable off your machine, **set
  `APP_ACCESS_TOKEN`** (see §9) first — without it, the API and WebSocket are wide open to anyone
  with the URL.
- The automated login (`kite_auth.py`) drives Zerodha's *web login pages*, not an official Kite
  Connect endpoint — it's the same technique many open-source Kite tools use, but Zerodha can
  change their login flow without notice, which would break auto-login until updated. The manual
  token method uses only the official `generate_session()` call and will keep working regardless.
- Keep `copytrader.db` and `.env` out of version control (already covered by the included
  `.gitignore`).

## 6. Kotak Neo child accounts

Child accounts can be Zerodha *or* Kotak Neo (the master must stay Zerodha — that's where the
order-update feed comes from). Add one from the dashboard by picking "Kotak Neo" as the broker;
you'll need a Trade API application (Invest tab → Trade API card on the Kotak Neo app/web, for
the consumer key) and TOTP registration completed there, plus your MPIN. Unlike Zerodha there's
no password or manual-token step — login is TOTP + a static MPIN with no SMS OTP involved, so
it's fully automatable the same way Zerodha's auto-login is.

Install the SDK into the backend venv (not on PyPI, installed from Kotak's official GitHub repo):
```bash
pip install "git+https://github.com/Kotak-Neo/Kotak-neo-api-v2.git@v2.0.2#egg=neo_api_client"
pip install websockets==12.0  # neo_api_client's setup downgrades this; FastAPI's /ws/live needs 12.0
```

This integration is based on Kotak's public v2 SDK docs and hasn't been exercised against a live
account from here — a few things to verify on first real use, and report back if they misbehave:
- Order placement, symbol resolution (`kotak_client._resolve_equity` / `_resolve_fo`), and pending
  order cancellation are implemented against documented endpoints, but Kotak's `totp_login()`
  kwarg is `mobile_number` per the SDK's README vs `mobilenumber` in one doc page — if login raises
  a `TypeError` about an unexpected keyword, that's the mismatch (`kotak_auth.py`).
- **The "Exit all" button does not auto-square-off Kotak Neo positions** — it only cancels pending
  orders. Kotak's positions API doesn't return a direct net-quantity field, and guessing wrong
  would risk placing a wrong-quantity order; close Kotak Neo positions manually until this is
  verified against a real account (`kotak_client.exit_account`).
- Kotak Neo's PnL figure is best-effort (realized buy/sell-amount delta only, no live-LTP
  unrealized leg) — treat it as approximate (`kotak_client.get_pnl`).

## 7. Angel One child accounts

Child accounts can also be Angel One (the master must stay Zerodha, same as Kotak Neo above).
Add one from the dashboard by picking "Angel One" as the broker; you'll need a SmartAPI app
(create one at smartapi.angelbroking.com for the API key) and TOTP registration completed on the
Angel One app, plus your trading PIN. Like Kotak Neo there's no password or manual-token step —
login is TOTP + a static PIN with no SMS OTP involved, so it's fully automatable.

Install the SDK into the backend venv (on PyPI, unlike Kotak Neo's):
```bash
pip install smartapi-python==1.5.5 logzero==1.7.0  # logzero isn't pulled in automatically - see requirements.txt
```

`smartapi-python`'s `logzero` dependency writes its own verbose log file to `backend/logs/<date>/app.log`
by default (not this app's own logging) — it includes full request headers, **including your
Angel One API key in plaintext**. This directory is gitignored, but it's still sitting on disk
locally; be aware of it if you ever archive or share the `backend/` folder.

Unlike Kotak Neo, Angel One's `generateSession()` returns a real, reusable JWT access/refresh
token pair, so this integration logs in only **once a day** (on "Auto-login") and reuses those
stored tokens for every other action (`angel_auth.get_angel_client`) rather than logging in
fresh each time. This isn't just a style choice — an earlier version of this code *did* log in
fresh per action (copying Kotak Neo's pattern), and a single "Auto-login" click triggered three
logins in a row, which tripped Angel's login-endpoint rate limit
(`"Access denied because of exceeding access rate"`, surfaced as a login failure). If you still
see that error after updating, Angel's rate-limit window may still be cooling down from earlier
attempts — wait a minute or two and try again rather than assume the code regressed.

This integration is based on Angel One's public SmartAPI docs, partially exercised against a live
account (login, its rate-limit behavior, and token-based reuse are confirmed) — a few things
remain to verify on first real trading use, and report back if they misbehave:
- The import name (`from SmartApi import SmartConnect`, used in `angel_auth.py`) has changed
  across SDK versions — if importing raises `ModuleNotFoundError`, check whether your installed
  version instead expects `from smartapi import SmartConnect`.
- **Confirmed live**: `generateSession()`'s `data.jwtToken` comes back already prefixed with
  `"Bearer "`, which the SDK's own request code then prefixes with `"Bearer "` *again* when
  building the Authorization header - every call fails with `"Invalid Token"` if that doubled
  prefix isn't stripped first. `angel_auth.get_angel_client` strips it defensively regardless of
  where the token came from, so this should be transparent, but it's the kind of undocumented
  quirk worth knowing about if you're debugging token issues here.
- Order placement, symbol resolution (`angel_client._resolve_equity` / `_resolve_fo`, against
  Angel's published scrip-master JSON file), and `placeOrder()`'s return shape are implemented
  against documented endpoints/fields but unverified live — errors are surfaced verbatim rather
  than swallowed if something doesn't match.
- Whether Angel's API rejects a plain MARKET order the way Kite's does is unverified, so orders
  use the same protected-LIMIT-price workaround as Zerodha children (see §5) rather than risk an
  unprotected order.
- **The "Exit all" button does not auto-square-off Angel One positions** — it only cancels pending
  orders, same caveat and same reason as Kotak Neo above; close Angel One positions manually until
  this is verified against a real account (`angel_client.exit_account`).

## 8. Known limitations / things to check before relying on this live

- Only `COMPLETE` master orders are copied; partial fills, modifications, and cancellations on the
  master are not currently propagated to children — worth adding if your master strategy relies on
  partial exits or SL modifications.
- There's no retry/backoff on a failed child order (e.g. transient network blip) — it's logged as
  FAILED and the dashboard shows it, but nothing re-attempts automatically.
- Test everything in small size / paper first. This places real orders with real money the
  moment the master's order completes.

## 9. Deploying so you can access it from any machine

The backend is a long-running process — it holds a persistent WebSocket to the market during
trading hours and keeps a SQLite file that must survive restarts — so it needs an always-on host
with a persistent disk, not a serverless/sleep-on-idle free tier. This section deploys the
**backend on Railway** and the **frontend (static) on Vercel**.

### 9.1 Push to GitHub

```bash
git add -A
git commit -m "Deploy: add access gate, Dockerfile, Railway/Vercel config"
git push
```

### 9.2 Backend → Railway

1. On [railway.app](https://railway.app), **New Project → Deploy from GitHub repo**, pick this repo.
2. In the service's **Settings → Source**, set **Root Directory** to `backend`. Railway will detect
   `backend/Dockerfile` and `backend/railway.toml` automatically.
3. **Settings → Volumes → New Volume**, mount it at `/data`. This is what makes your account data
   survive redeploys.
4. **Variables**, add:
   - `ENCRYPTION_KEY` — generate with
     `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
   - `DATABASE_URL` = `sqlite:////data/copytrader.db` (note **4 slashes** — that's the volume path)
   - `APP_ACCESS_TOKEN` — generate with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
     This is the password the dashboard will ask for once deployed — see the security note below.
   - `FRONTEND_ORIGIN` — leave as `http://localhost:5173` for now, you'll update it after step 9.3
     once you have your Vercel URL.
5. **Settings → Networking → Generate Domain** to get a public URL
   (e.g. `https://copy-trading-production.up.railway.app`). Every `git push` redeploys automatically.

### 9.3 Frontend → Vercel

1. On [vercel.com](https://vercel.com), **Add New → Project**, import the same GitHub repo.
2. Set **Root Directory** to `frontend` (Vercel auto-detects the Vite build command/output).
3. **Environment Variables**, add `VITE_API_BASE` = your Railway URL from step 9.2 (no trailing slash).
4. Deploy. You'll get a URL like `https://your-app.vercel.app`.
5. Back on Railway, update `FRONTEND_ORIGIN` to that Vercel URL (comma-separate if you still want
   local dev to work too: `http://localhost:5173,https://your-app.vercel.app`) — Railway redeploys
   automatically on a variable change.

Open the Vercel URL from any machine — you'll be asked for the access code (`APP_ACCESS_TOKEN`)
once per browser before the dashboard loads.

### Security note on deploying this publicly

This app places real orders and squares off real positions with **no per-user accounts** — it's
built for a single operator. `APP_ACCESS_TOKEN` (§9.2) is a single shared-secret gate on the whole
API and dashboard, checked on every request and on the WebSocket handshake; treat it like a
password (a long random string, not shared, rotate it if you suspect it leaked — just change the
Railway variable). It is **not** a substitute for keeping the URL itself private: don't post it
publicly, and consider Railway's own network restriction options if you want to lock the backend
down to specific IPs.

The SQLite file lives on the Railway volume, which is durable but is still a single copy — the
app's own nightly-on-startup backup (`database.py:backup_sqlite_db`) writes timestamped copies
into the same volume, which protects against a bad migration but not against losing the volume
itself. If you want off-host durability, periodically download the DB file via Railway's shell/CLI,
or migrate `DATABASE_URL` to a managed Postgres instance (Railway offers one — SQLAlchemy already
reads the URL from this env var, so the code needs no change beyond that and installing
`psycopg2-binary`).

## Project structure

```
backend/
  main.py                # FastAPI app: REST routes + live WebSocket broadcast + access gate
  models.py               # Account + TradeLog + MirroredOrder tables (SQLAlchemy)
  database.py              # SQLite engine/session + on-startup backup
  crypto_utils.py           # Fernet encryption for stored secrets
  trade_log.py               # Shared TradeLog creation/broadcast helper (all brokers)
  kite_auth.py                 # Zerodha daily access-token generation (auto + manual)
  kotak_auth.py                  # Kotak Neo TOTP+MPIN login
  kotak_client.py                  # Kotak Neo order placement, symbol mapping, capital, exit
  angel_auth.py                      # Angel One TOTP+PIN login
  angel_client.py                      # Angel One order placement, symbol mapping, capital, exit
  replication_engine.py                  # Proportional sizing, freeze-limit slicing, order placement
  ticker_listener.py                       # KiteTicker order-update listener (master, Zerodha only)
  Dockerfile, railway.toml                   # Backend deploy config (see §9)
frontend/
  src/App.jsx              # Dashboard shell, live feed via WebSocket
  src/components/           # MasterCard, ChildCard, TradeFeed, TickerTape, AccessGate, modals
```
