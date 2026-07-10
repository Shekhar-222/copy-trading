import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)              # human-friendly name, e.g. "Shekhar Main"
    role = Column(String, nullable=False)                # "master" or "child"
    broker = Column(String, nullable=False, default="zerodha")  # "zerodha" or "kotak_neo"

    # Fields below are reused across brokers where the concept lines up, to avoid a column
    # per broker. Kotak Neo is child-only (see kotak_client.py) and has no master/order-update
    # feed, no daily-refreshable token, and no password-based login - only TOTP + a static MPIN.
    #   client_id       - Zerodha client id, e.g. AJ230321  /  Kotak Neo UCC
    #   api_key_enc      - Kite Connect api_key              /  Kotak Neo consumer_key
    #   api_secret_enc    - Kite Connect api_secret            /  unused for Kotak Neo (stored as "")
    #   totp_secret_enc  - TOTP secret, same concept for both brokers
    #   password_enc     - Zerodha login password (optional)  /  unused for Kotak Neo
    #   access_token_enc  - Kite's daily access token          /  sentinel marking "logged in today"
    #                                                             for Kotak Neo (every Kotak Neo action
    #                                                             re-authenticates fresh with TOTP+MPIN,
    #                                                             see kotak_auth.py, since there's no
    #                                                             documented way to reattach a session
    #                                                             from a stored token alone)
    client_id = Column(String, nullable=False)
    api_key_enc = Column(Text, nullable=False)
    api_secret_enc = Column(Text, nullable=False)
    password_enc = Column(Text, nullable=True)
    totp_secret_enc = Column(Text, nullable=False)
    access_token_enc = Column(Text, nullable=True)
    token_generated_at = Column(DateTime, nullable=True)

    mpin_enc = Column(Text, nullable=True)               # Kotak Neo MPIN - 2FA, not used by Zerodha
    mobile_number = Column(String, nullable=True)        # Kotak Neo registered mobile number - not used by Zerodha

    real_name = Column(String, nullable=True)             # account holder's name, fetched from Kite profile
    capital = Column(Float, default=0.0)                 # last-fetched available margin, used for ratio calc
    multiplier_override = Column(Float, nullable=True)   # optional manual override instead of ratio calc
    active = Column(Boolean, default=True)                # whether this account participates in copying
    created_at = Column(DateTime, default=datetime.datetime.utcnow)


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
