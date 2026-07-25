import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)              # human-friendly name, e.g. "Shekhar Main"
    role = Column(String, nullable=False)                # "master" or "child"
    broker = Column(String, nullable=False, default="zerodha")  # "zerodha", "kotak_neo", "angel_one", or "groww"

    # Fields below are reused across brokers where the concept lines up, to avoid a column per
    # broker. Kotak Neo, Angel One, and Groww are all child-only (see kotak_client.py /
    # angel_client.py / groww_client.py) and have no master/order-update feed and no
    # password-based login - only TOTP (+ a static PIN for Kotak/Angel). Each has a different
    # token-lifecycle model though, which changes what access_token_enc/api_secret_enc actually
    # hold:
    #   - Kotak Neo: no stored-token reattachment support at all - every action re-authenticates
    #     fresh with TOTP+MPIN (kotak_auth.py). access_token_enc just holds a "logged in today"
    #     sentinel string.
    #   - Angel One: generateSession() returns a real, reusable JWT access/refresh token pair,
    #     refreshed once daily like Zerodha (angel_auth.py) - a fresh-login-per-action design
    #     (copying Kotak's pattern initially) hit Angel's login rate limit on first live use.
    #     access_token_enc holds the real access token, api_secret_enc (otherwise unused for
    #     this broker) holds the refresh token.
    #   - Groww: get_access_token() is documented as producing a token with "No Expiry" -
    #     generated ONCE (not daily) and reused indefinitely (groww_auth.py). access_token_enc
    #     holds that token; _token_is_fresh() has a broker-specific carve-out for "groww" so it
    #     doesn't force a same-day re-login the way Zerodha/Angel do. Groww also issues two
    #     mutually-exclusive API key types chosen at key-creation time on their site (confirmed
    #     live, 2026-07-24: using the wrong flow for a given key returns "Invalid type
    #     provided") - a "TOTP" key pairs with totp_secret_enc same as the other brokers, an
    #     "Approval" key pairs with a plain secret string in api_secret_enc instead (used to
    #     compute a checksum, no TOTP/app-approval step involved despite the name). Exactly one
    #     of totp_secret_enc/api_secret_enc is populated for a Groww account; see groww_auth.py.
    #
    #   client_id       - Zerodha client id (e.g. AJ230321) / Kotak Neo UCC / Angel One client code / Groww UCC (optional, informational only - not used for Groww auth)
    #   api_key_enc      - Kite Connect api_key / Kotak Neo consumer_key / Angel One SmartAPI api_key / Groww API key
    #   api_secret_enc    - Kite Connect api_secret / unused for Kotak Neo (stored as "") / Angel One's refresh token / Groww's Approval-key secret (empty if the account uses a TOTP-key instead)
    #   totp_secret_enc  - TOTP secret, same concept across Zerodha/Kotak Neo/Angel One (Zerodha's is only used for auto-login, not required); for Groww, populated only if the account uses a TOTP-key (empty if it uses an Approval-key instead - see api_secret_enc)
    #   password_enc     - Zerodha login password (optional) / unused for Kotak Neo, Angel One, and Groww
    #   access_token_enc  - Kite's daily access token / Kotak Neo's "logged in today" sentinel / Angel One's daily JWT / Groww's non-expiring token
    client_id = Column(String, nullable=False)
    api_key_enc = Column(Text, nullable=False)
    api_secret_enc = Column(Text, nullable=False)
    password_enc = Column(Text, nullable=True)
    totp_secret_enc = Column(Text, nullable=False)
    access_token_enc = Column(Text, nullable=True)
    token_generated_at = Column(DateTime, nullable=True)

    mpin_enc = Column(Text, nullable=True)               # Kotak Neo MPIN / Angel One trading PIN - not used by Zerodha
    mobile_number = Column(String, nullable=True)        # Kotak Neo registered mobile number - not used by Zerodha or Angel One

    real_name = Column(String, nullable=True)             # account holder's name, fetched from Kite profile
    capital = Column(Float, default=0.0)                 # last-fetched available margin, used for ratio calc
    multiplier_override = Column(Float, nullable=True)   # optional manual override instead of ratio calc
    active = Column(Boolean, default=True)                # whether this account participates in copying
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


class MirroredOrder(Base):
    """Tracks a LIMIT/SL/SL-M order mirrored onto a child's own book while it's still resting
    on the master's (see replication_engine._handle_order_lifecycle) - so a later order-update
    event for the same master order (modify/cancel/trigger) can find and act on the matching
    child order instead of re-mirroring it or losing track of it. One master order can map to
    SEVERAL rows per child: quantities above the exchange's freeze limit are sliced into
    multiple child orders, each tracked as its own row."""
    __tablename__ = "mirrored_orders"

    id = Column(Integer, primary_key=True, index=True)
    master_order_id = Column(String, nullable=False, index=True)
    child_account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    child_order_id = Column(String, nullable=False)
    exchange = Column(String)
    tradingsymbol = Column(String)
    transaction_type = Column(String)
    order_type = Column(String)                          # "LIMIT", "SL", or "SL-M"
    variety = Column(String, default="regular")          # "regular" or "amo" - must match on modify/cancel
    trigger_price = Column(Float)
    price = Column(Float, nullable=True)
    quantity = Column(Integer)
    status = Column(String, default="OPEN")              # OPEN / CANCELLED / COMPLETE
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)


class TradeLog(Base):
    __tablename__ = "trade_logs"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    master_order_id = Column(String, nullable=True)
    child_account_id = Column(Integer, ForeignKey("accounts.id"), nullable=True)
    child_order_id = Column(String, nullable=True)
    tradingsymbol = Column(String)
    exchange = Column(String)
    transaction_type = Column(String)     # BUY / SELL
    master_quantity = Column(Integer)
    replicated_quantity = Column(Integer)
    status = Column(String)               # SUCCESS / FAILED / SKIPPED
    message = Column(Text, nullable=True)
