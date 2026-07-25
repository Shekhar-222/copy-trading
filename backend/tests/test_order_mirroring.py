"""
Covers _handle_order_lifecycle (replication_engine.py): LIMIT and stoploss (SL/SL-M) orders -
including AMO orders queued after hours - should be mirrored onto Zerodha children as a live
resting order while still pending on the master, rather than only copied after they've already
filled. MARKET orders are the one exception and keep the old fill-then-copy behaviour (see
replicate_order's docstring for why).
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import crypto_utils
import models
import replication_engine


@pytest.fixture
def db_session(monkeypatch):
    # StaticPool so every connection (including the ones _replicate_one_child's worker threads
    # open via replication_engine.SessionLocal) shares the same in-memory database instead of
    # each thread getting its own empty :memory: db. replication_engine.SessionLocal is
    # monkeypatched to this test's sessionmaker for the same reason - otherwise those worker
    # threads would fall back to the real database.py engine (the actual copytrader.db file).
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool,
    )
    models.Base.metadata.create_all(bind=engine)
    TestSessionLocal = sessionmaker(bind=engine)
    monkeypatch.setattr(replication_engine, "SessionLocal", TestSessionLocal)
    session = TestSessionLocal()
    yield session
    session.close()


def make_account(db, label, role, broker="zerodha", capital=100000.0,
                  multiplier_override=None, mobile_number=None, mpin=None):
    acc = models.Account(
        label=label, role=role, broker=broker, client_id=label,
        api_key_enc=crypto_utils.encrypt(f"{label}-api-key"),
        api_secret_enc=crypto_utils.encrypt("secret"),
        totp_secret_enc=crypto_utils.encrypt("totp"),
        access_token_enc=crypto_utils.encrypt(f"{label}-token"),
        capital=capital, multiplier_override=multiplier_override,
        active=True, mobile_number=mobile_number,
        mpin_enc=crypto_utils.encrypt(mpin) if mpin else None,
    )
    db.add(acc)
    db.commit()
    db.refresh(acc)
    return acc


class FakeKite:
    """Records every call instead of hitting Kite - one instance per (fake) account."""
    VARIETY_REGULAR = "regular"
    PRODUCT_MIS = "MIS"
    ORDER_TYPE_LIMIT = "LIMIT"
    TRANSACTION_TYPE_BUY = "BUY"
    TRANSACTION_TYPE_SELL = "SELL"

    _counter = [0]

    def __init__(self):
        self.calls = []

    def place_order(self, **kwargs):
        self.calls.append(("place_order", kwargs))
        FakeKite._counter[0] += 1
        return f"ORD{FakeKite._counter[0]}"

    def modify_order(self, **kwargs):
        self.calls.append(("modify_order", kwargs))

    def cancel_order(self, **kwargs):
        self.calls.append(("cancel_order", kwargs))

    def ltp(self, key):
        return {key: {"last_price": 100.0}}


@pytest.fixture
def fake_kites(monkeypatch):
    """api_key (plaintext) -> FakeKite, patched in place of kite_auth.get_kite_client."""
    clients = {}

    def _get_kite_client(api_key, access_token):
        return clients.setdefault(api_key, FakeKite())

    monkeypatch.setattr(replication_engine, "get_kite_client", _get_kite_client)
    return clients


def sl_order(**overrides):
    order = {
        "order_id": "MASTER1",
        "status": "TRIGGER PENDING",
        "order_type": "SL",
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "trigger_price": 95.0,
        "price": 95.5,
        "product": "MIS",
    }
    order.update(overrides)
    return order


def limit_order(**overrides):
    order = {
        "order_id": "MASTER2",
        "status": "OPEN",
        "order_type": "LIMIT",
        "variety": "regular",
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "trigger_price": 0,
        "price": 150.0,
        "product": "MIS",
    }
    order.update(overrides)
    return order


def market_order(**overrides):
    order = {
        "order_id": "MASTER3",
        "status": "OPEN",
        "order_type": "MARKET",
        "variety": "amo",
        "exchange": "NSE",
        "tradingsymbol": "INFY",
        "transaction_type": "BUY",
        "quantity": 10,
        "trigger_price": 0,
        "price": 0,
        "product": "MIS",
    }
    order.update(overrides)
    return order


# ---------------------------- Stoploss (SL/SL-M) ----------------------------

def test_new_trigger_pending_mirrors_sl_order_to_zerodha_child(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, sl_order())

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]
    kwargs = child_kite.calls[0][1]
    assert kwargs["order_type"] == "SL"
    assert kwargs["variety"] == "regular"
    assert kwargs["trigger_price"] == 95.0
    assert kwargs["price"] == 95.5
    assert kwargs["quantity"] == 10

    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.status == "OPEN"
    assert mirror.child_order_id == "ORD1"


def test_duplicate_trigger_pending_is_a_no_op(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, sl_order())
    replication_engine.replicate_order(db_session, master, sl_order())  # identical repeat

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]


def test_modified_trigger_price_updates_the_mirrored_order(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, sl_order())
    replication_engine.replicate_order(db_session, master, sl_order(trigger_price=90.0, price=90.5))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order", "modify_order"]
    assert child_kite.calls[1][1]["trigger_price"] == 90.0

    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.trigger_price == 90.0


def test_cancelled_master_order_cancels_the_mirrored_child_order(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, sl_order())
    replication_engine.replicate_order(db_session, master, sl_order(status="CANCELLED"))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order", "cancel_order"]

    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.status == "CANCELLED"


def test_master_trigger_firing_does_not_place_a_second_child_order(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, sl_order())
    replication_engine.replicate_order(db_session, master, sl_order(status="COMPLETE"))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]  # no second (fill-then-copy) order

    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.status == "COMPLETE"


def test_complete_without_a_prior_mirror_falls_back_to_fill_then_copy(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    # COMPLETE arrives with no earlier TRIGGER PENDING ever having been seen for this order.
    replication_engine.replicate_order(db_session, master, sl_order(status="COMPLETE"))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]
    assert child_kite.calls[0][1]["order_type"] == "LIMIT"  # market-protected fallback, not a resting SL
    assert db_session.query(models.MirroredOrder).count() == 0


def test_kotak_neo_child_is_skipped_with_an_explicit_message(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "kotakchild", "child", broker="kotak_neo",
                 mobile_number="+919999999999", mpin="1234")

    replication_engine.replicate_order(db_session, master, sl_order())

    logs = db_session.query(models.TradeLog).all()
    assert len(logs) == 1
    assert logs[0].status == "SKIPPED"
    assert "Kotak Neo" in logs[0].message
    assert db_session.query(models.MirroredOrder).count() == 0


def test_angel_one_child_is_skipped_with_an_explicit_message(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "angelchild", "child", broker="angel_one", mpin="1234")

    replication_engine.replicate_order(db_session, master, sl_order())

    logs = db_session.query(models.TradeLog).all()
    assert len(logs) == 1
    assert logs[0].status == "SKIPPED"
    assert "Angel One" in logs[0].message
    assert db_session.query(models.MirroredOrder).count() == 0


def test_groww_child_is_skipped_with_an_explicit_message(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "growwchild", "child", broker="groww")

    replication_engine.replicate_order(db_session, master, sl_order())

    logs = db_session.query(models.TradeLog).all()
    assert len(logs) == 1
    assert logs[0].status == "SKIPPED"
    assert "Groww" in logs[0].message
    assert db_session.query(models.MirroredOrder).count() == 0


# ---------------------------- Plain LIMIT (incl. AMO) ----------------------------

def test_new_open_limit_order_is_mirrored_at_the_same_price(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, limit_order())

    child_kite = fake_kites["child1-api-key"]
    kwargs = child_kite.calls[0][1]
    assert kwargs["order_type"] == "LIMIT"
    assert kwargs["price"] == 150.0
    assert "trigger_price" not in kwargs  # plain LIMIT doesn't use a trigger price

    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.price == 150.0
    assert mirror.variety == "regular"


def test_amo_limit_order_is_mirrored_with_amo_variety(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, limit_order(variety="amo"))

    child_kite = fake_kites["child1-api-key"]
    assert child_kite.calls[0][1]["variety"] == "amo"
    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.variety == "amo"


def test_amo_order_queued_while_market_closed_is_mirrored_immediately(db_session, fake_kites):
    """Zerodha reports a queued AMO order's status as "AMO REQ RECEIVED" (not "OPEN") until
    the next session converts it - this must be treated as resting too, not ignored."""
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(
        db_session, master, limit_order(variety="amo", status="AMO REQ RECEIVED")
    )

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]
    assert child_kite.calls[0][1]["variety"] == "amo"
    mirror = db_session.query(models.MirroredOrder).one()
    assert mirror.status == "OPEN"
    assert mirror.variety == "amo"


def test_limit_order_modify_and_cancel_use_the_stored_variety(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, limit_order(variety="amo"))
    replication_engine.replicate_order(db_session, master, limit_order(variety="amo", price=145.0))
    replication_engine.replicate_order(db_session, master, limit_order(variety="amo", status="CANCELLED"))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order", "modify_order", "cancel_order"]
    assert child_kite.calls[1][1]["variety"] == "amo"
    assert child_kite.calls[2][1]["variety"] == "amo"


# ---------------------------- MARKET orders (incl. AMO) stay fill-then-copy ----------------------------

def test_market_order_resting_open_status_is_ignored(db_session, fake_kites):
    """A MARKET order (even AMO, queued overnight) can't be mirrored as a resting order - Kite
    rejects raw MARKET orders outright - so an OPEN status update for one must be a no-op."""
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, market_order())  # status OPEN

    child_kite = fake_kites.get("child1-api-key")
    assert child_kite is None or child_kite.calls == []
    assert db_session.query(models.MirroredOrder).count() == 0


def test_market_order_only_replicates_once_complete(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, market_order(status="OPEN"))
    replication_engine.replicate_order(db_session, master, market_order(status="COMPLETE"))

    child_kite = fake_kites["child1-api-key"]
    assert [c[0] for c in child_kite.calls] == ["place_order"]
    assert child_kite.calls[0][1]["order_type"] == "LIMIT"  # market-protected fallback, priced off live LTP


# ---------------------------- Freeze-quantity slicing ----------------------------
# NIFTY's exchange freeze limit is 1800; with lot_size=25 that's 72 lots/slice. A 4000-quantity
# order should never be sent to the exchange in one shot - it must come out as slices of
# [1800, 1800, 400].

def nifty_limit_order(**overrides):
    base = dict(order_id="MASTERNIFTY", exchange="NFO", tradingsymbol="NIFTY24JUL24000CE",
                quantity=4000, lot_size=25)
    base.update(overrides)
    return limit_order(**base)


def nifty_market_order(**overrides):
    base = dict(order_id="MASTERNIFTYM", exchange="NFO", tradingsymbol="NIFTY24JUL24000CE",
                quantity=4000, lot_size=25)
    base.update(overrides)
    return market_order(**base)


def test_fill_then_copy_slices_a_quantity_over_the_freeze_limit(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, nifty_market_order(status="COMPLETE"))

    child_kite = fake_kites["child1-api-key"]
    quantities = [c[1]["quantity"] for c in child_kite.calls if c[0] == "place_order"]
    assert quantities == [1800, 1800, 400]


def test_resting_limit_order_over_freeze_limit_is_mirrored_as_multiple_slices(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, nifty_limit_order())

    child_kite = fake_kites["child1-api-key"]
    quantities = [c[1]["quantity"] for c in child_kite.calls if c[0] == "place_order"]
    assert quantities == [1800, 1800, 400]

    mirrors = db_session.query(models.MirroredOrder).order_by(models.MirroredOrder.id).all()
    assert [m.quantity for m in mirrors] == [1800, 1800, 400]
    assert all(m.status == "OPEN" for m in mirrors)
    assert all(m.master_order_id == "MASTERNIFTY" for m in mirrors)


def test_cancelling_a_sliced_master_order_cancels_every_slice(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, nifty_limit_order())
    replication_engine.replicate_order(db_session, master, nifty_limit_order(status="CANCELLED"))

    child_kite = fake_kites["child1-api-key"]
    cancel_calls = [c for c in child_kite.calls if c[0] == "cancel_order"]
    assert len(cancel_calls) == 3

    mirrors = db_session.query(models.MirroredOrder).all()
    assert all(m.status == "CANCELLED" for m in mirrors)


def test_modifying_price_only_keeps_the_same_slice_count_and_modifies_each_leg(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, nifty_limit_order())
    replication_engine.replicate_order(db_session, master, nifty_limit_order(price=145.0))

    child_kite = fake_kites["child1-api-key"]
    modify_calls = [c for c in child_kite.calls if c[0] == "modify_order"]
    assert len(modify_calls) == 3
    assert all(c[1]["price"] == 145.0 for c in modify_calls)

    mirrors = db_session.query(models.MirroredOrder).filter(models.MirroredOrder.status == "OPEN").all()
    assert [m.quantity for m in mirrors] == [1800, 1800, 400]


def test_modifying_qty_across_a_slice_boundary_cancels_and_re_places(db_session, fake_kites):
    master = make_account(db_session, "master", "master")
    make_account(db_session, "child1", "child")

    replication_engine.replicate_order(db_session, master, nifty_limit_order())  # 3 slices
    replication_engine.replicate_order(db_session, master, nifty_limit_order(quantity=1000))  # now fits in 1

    child_kite = fake_kites["child1-api-key"]
    call_types = [c[0] for c in child_kite.calls]
    assert call_types.count("place_order") == 4  # 3 initial + 1 re-placed
    assert call_types.count("cancel_order") == 3  # the 3 original slices

    open_mirrors = db_session.query(models.MirroredOrder).filter(models.MirroredOrder.status == "OPEN").all()
    assert [m.quantity for m in open_mirrors] == [1000]
    cancelled = db_session.query(models.MirroredOrder).filter(models.MirroredOrder.status == "CANCELLED").all()
    assert len(cancelled) == 3
