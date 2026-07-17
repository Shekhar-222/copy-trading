import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)              # human-friendly name, e.g. "Shekhar Main"
    role = Column(String, nullable=False)                # "master" or "child"
    broker = Column(String, nullable=False, default="zerodha")  # "zerodha", "kotak_neo", or "angel_one"

    # Fields below are reused across brokers where the concept lines up, to avoid a column
    # per broker. Kotak Neo and Angel One are both child-only (see kotak_client.py /
    # angel_client.py) and have no master/order-update feed and no password-based login -
    # only TOTP + a static PIN. Kotak Neo has no daily-refreshable token (its SDK has no
    # documented way to reattach a session from a stored token alone, so every action
    # re-authenticates fresh); Angel One DOES support this (generateSession() returns a real,
    # reusable JWT access/refresh token pair), so it follows Zerodha's daily-token model
    # instead - see angel_auth.py's module docstring for why that distinction matters (a
    # fresh-login-per-action design for Angel One hit its login endpoint's rate limit).
    #   client_id       - Zerodha client id, e.g. AJ230321  /  Kotak Neo UCC          /  Angel One client code
    #   api_key_enc      - Kite Connect api_key              /  Kotak Neo consumer_key /  Angel One SmartAPI api_key
    #   api_secret_enc    - Kite Connect api_secret            /  unused for Kotak Neo  /  Angel One refresh token
    #                                                             (stored as "")             (from generateSession,
    #                                                                                          reused to rebuild a
    #                                                                                          client without a
    #                                                                                          fresh login - see
    #                                                                                          angel_auth.get_angel_client)
    #   totp_secret_enc  - TOTP secret, same concept across all three brokers (only used at daily-login time
    #                                                                          for Angel One, every action for Kotak Neo)
    #   password_enc     - Zerodha login password (optional)  /  unused for Kotak Neo and Angel One
    #   access_token_enc  - Kite's daily access token          /  sentinel marking "logged in       /  Angel One's real
    #                                                             today" for Kotak Neo (re-               daily JWT access
    #                                                             authenticates fresh every action,       token, reused
    #                                                             see kotak_auth.py)                       across actions
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
