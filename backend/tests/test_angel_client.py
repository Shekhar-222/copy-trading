import datetime

import pytest

import angel_client


class FakeAngel:
    def __init__(self):
        self.placed = []

    def ltpData(self, exchange, tradingsymbol, symboltoken):
        return {"status": True, "data": {"ltp": 6000.0}}

    def placeOrderFullResponse(self, params):
        self.placed.append(params)
        return {"status": True, "data": {"orderid": "A1"}}

    def orderBook(self):
        return {"data": [{"orderid": "A1", "status": "open"}]}


_SCRIP_MASTER = {
    ("MCX", "CRUDEOIL"): [{
        "symbol": "CRUDEOIL19NOV26FUT", "token": "111", "expiry": "19NOV2026", "strike": "0.000000",
        "instrumenttype": "FUTCOM", "lotsize": "100", "tick_size": "100.000000",
    }],
    ("NCO", "GOLD"): [{
        "symbol": "GOLD04DEC26FUT", "token": "222", "expiry": "04DEC2026", "strike": "0.000000",
        "instrumenttype": "FUTBLN", "lotsize": "1", "tick_size": "100.000000",
    }],
    ("NFO", "NIFTY"): [{
        "symbol": "NIFTY25NOV26FUT", "token": "333", "expiry": "25NOV2026", "strike": "0.000000",
        "instrumenttype": "FUTIDX", "lotsize": "75", "tick_size": "5.000000",
    }],
}


@pytest.fixture
def fake_angel(monkeypatch):
    client = FakeAngel()
    monkeypatch.setattr(angel_client, "_client_for", lambda account: client)
    monkeypatch.setattr(angel_client, "_load_instrument_master", lambda: _SCRIP_MASTER)
    monkeypatch.setattr(angel_client.time, "sleep", lambda s: None)
    return client


def _fut(name, expiry, lot_size):
    return {"name": name, "expiry": expiry, "strike": 0, "instrument_type": "FUT", "lot_size": lot_size}


def test_mcx_kite_lots_are_converted_to_angel_units(fake_angel):
    # Kite: 2 lots of CRUDEOIL (lot_size=1) -> Angel: 2 x 100 barrels.
    angel_client.place_child_order(None, "MCX", "CRUDEOIL26NOVFUT", "BUY", 2, "NRML",
                                   _fut("CRUDEOIL", datetime.date(2026, 11, 19), 1))
    assert fake_angel.placed[0]["quantity"] == "200"
    assert fake_angel.placed[0]["exchange"] == "MCX"


def test_mcx_conversion_goes_through_the_lot_count_if_kite_reports_units(fake_angel):
    angel_client.place_child_order(None, "MCX", "CRUDEOIL26NOVFUT", "SELL", 300, "NRML",
                                   _fut("CRUDEOIL", datetime.date(2026, 11, 19), 100))
    assert fake_angel.placed[0]["quantity"] == "300"


def test_nco_quantity_is_unchanged_when_both_brokers_use_lot_size_1(fake_angel):
    angel_client.place_child_order(None, "NCO", "GOLD26DECFUT", "BUY", 3, "NRML",
                                   _fut("GOLD", datetime.date(2026, 12, 4), 1))
    assert fake_angel.placed[0]["quantity"] == "3"


def test_nfo_quantity_is_not_converted(fake_angel):
    angel_client.place_child_order(None, "NFO", "NIFTY26NOVFUT", "BUY", 150, "NRML",
                                   _fut("NIFTY", datetime.date(2026, 11, 25), 75))
    assert fake_angel.placed[0]["quantity"] == "150"


def test_nfo_lot_size_mismatch_still_fails_loudly(fake_angel):
    with pytest.raises(ValueError, match="not a whole multiple"):
        angel_client.place_child_order(None, "NFO", "NIFTY26NOVFUT", "BUY", 65, "NRML",
                                       _fut("NIFTY", datetime.date(2026, 11, 25), 65))
    assert fake_angel.placed == []


def test_angel_error_message_is_surfaced(fake_angel, monkeypatch):
    monkeypatch.setattr(fake_angel, "placeOrderFullResponse", lambda params: {
        "success": False, "errorCode": "AG7002", "data": "",
        "message": "1.2.3.4 is not a registered IP, please check your registered IP.",
    })
    with pytest.raises(ValueError, match="not a registered IP.*AG7002"):
        angel_client.place_child_order(None, "MCX", "CRUDEOIL26NOVFUT", "BUY", 1, "NRML",
                                       _fut("CRUDEOIL", datetime.date(2026, 11, 19), 1))
