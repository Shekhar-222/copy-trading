import datetime
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, ForeignKey, Text
from database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, index=True)
    label = Column(String, nullable=False)              # human-friendly name, e.g. "Shekhar Main"
    role = Column(String, nullable=False)                # "master" or "child"
    client_id = Column(String, nullable=False)           # Zerodha client id, e.g. AJ230321
    api_key_enc = Column(Text, nullable=False)
    api_secret_enc = Column(Text, nullable=False)
    password_enc = Column(Text, nullable=True)            # Zerodha login password - only needed for auto-login
    totp_secret_enc = Column(Text, nullable=False)       # base32 TOTP secret from Kite 2FA setup
    access_token_enc = Column(Text, nullable=True)       # refreshed daily, encrypted at rest
    token_generated_at = Column(DateTime, nullable=True)

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
