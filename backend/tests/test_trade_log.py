import datetime

import models
from trade_log import to_dict, today_ist_start_utc


def test_to_dict_appends_utc_z_suffix_to_naive_timestamp():
    # log.timestamp is naive UTC (datetime.utcnow()) - to_dict must mark it as such so the
    # frontend doesn't misread it as already being local time.
    log = models.TradeLog(
        timestamp=datetime.datetime(2026, 7, 11, 10, 30, 0),
        tradingsymbol="INFY",
        exchange="NSE",
        transaction_type="BUY",
        master_quantity=10,
        replicated_quantity=5,
        status="SUCCESS",
        message="ok",
    )
    payload = to_dict(log, "Test Child")
    assert payload["timestamp"] == "2026-07-11T10:30:00Z"
    assert payload["child_account"] == "Test Child"


def test_today_ist_start_utc_is_ist_midnight_expressed_in_utc():
    # 2026-07-11 03:00 UTC = 2026-07-11 08:30 IST -> that IST day started at 2026-07-10 18:30 UTC
    cutoff = today_ist_start_utc(now_utc=datetime.datetime(2026, 7, 11, 3, 0, 0))
    assert cutoff == datetime.datetime(2026, 7, 10, 18, 30, 0)


def test_today_ist_start_utc_handles_the_late_utc_evening_case():
    # 2026-07-11 20:00 UTC = 2026-07-12 01:30 IST (already past midnight IST) -> that IST day
    # started at 2026-07-11 18:30 UTC
    cutoff = today_ist_start_utc(now_utc=datetime.datetime(2026, 7, 11, 20, 0, 0))
    assert cutoff == datetime.datetime(2026, 7, 11, 18, 30, 0)
