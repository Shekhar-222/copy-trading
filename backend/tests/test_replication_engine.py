from replication_engine import compute_child_quantity, _freeze_quantity, _slice_quantity


def test_scales_by_capital_ratio_and_rounds_down_to_whole_lots():
    # child has half the master's capital -> half the quantity, floored to lot size
    qty = compute_child_quantity(master_qty=100, master_capital=200000, child_capital=100000, lot_size=25)
    assert qty == 50


def test_rounds_down_to_nearest_lot_not_up():
    # raw = 10 * 0.9 = 9, which is less than one 25-lot - must floor to 0, never round up
    qty = compute_child_quantity(master_qty=10, master_capital=100000, child_capital=90000, lot_size=25)
    assert qty == 0


def test_manual_multiplier_override_takes_precedence_over_capital_ratio():
    qty = compute_child_quantity(
        master_qty=10, master_capital=100000, child_capital=1, lot_size=1, multiplier_override=2.0
    )
    assert qty == 20


def test_zero_or_negative_master_capital_yields_zero_without_override():
    assert compute_child_quantity(master_qty=10, master_capital=0, child_capital=5000, lot_size=1) == 0
    assert compute_child_quantity(master_qty=10, master_capital=-100, child_capital=5000, lot_size=1) == 0


def test_never_returns_a_negative_quantity():
    qty = compute_child_quantity(master_qty=10, master_capital=100000, child_capital=1, lot_size=25)
    assert qty >= 0


# ---------------------------- Freeze-quantity slicing ----------------------------

def test_freeze_quantity_is_none_for_equity_cash():
    # NSE/BSE cash market has no per-order freeze limit - only F&O/commodity/currency do.
    assert _freeze_quantity("NSE", "INFY") is None
    assert _freeze_quantity("BSE", "RELIANCE") is None


def test_freeze_quantity_looks_up_known_index_contracts():
    assert _freeze_quantity("NFO", "BANKNIFTY24JUL50000CE") == 900
    assert _freeze_quantity("NFO", "NIFTY24JUL24000CE") == 1800
    assert _freeze_quantity("BFO", "SENSEX24JUL80000CE") == 1000


def test_freeze_quantity_falls_back_to_a_conservative_default_for_unlisted_contracts():
    # e.g. an individual stock's F&O contract, not in the hardcoded index table
    assert _freeze_quantity("NFO", "RELIANCE24JULFUT") == 900


def test_slice_quantity_is_a_no_op_under_the_freeze_limit():
    assert _slice_quantity(500, lot_size=25, freeze_qty=1800) == [500]


def test_slice_quantity_is_a_no_op_when_there_is_no_freeze_limit():
    assert _slice_quantity(10_000, lot_size=1, freeze_qty=None) == [10_000]


def test_slice_quantity_splits_evenly_when_qty_is_a_multiple_of_the_slice_size():
    # freeze 1800, lot 25 -> 72 lots/slice -> 1800/slice; 3600 splits into exactly two
    assert _slice_quantity(3600, lot_size=25, freeze_qty=1800) == [1800, 1800]


def test_slice_quantity_puts_the_remainder_in_the_final_slice():
    assert _slice_quantity(4000, lot_size=25, freeze_qty=1800) == [1800, 1800, 400]


def test_slice_quantity_slices_stay_within_freeze_limit_and_are_whole_lots():
    slices = _slice_quantity(4321, lot_size=25, freeze_qty=1800)
    assert sum(slices) == 4321
    assert all(s <= 1800 for s in slices)
    assert all(s % 25 == 0 for s in slices[:-1])  # every slice but a possibly-partial last lot chunk
