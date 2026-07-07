from petlibro_mcp.planner import allocate_portions, plan_rows


def test_allocate_sums_to_total():
    out = allocate_portions([0.5, 0.3, 0.2], 12)
    assert sum(out) == 12
    assert out == [6, 4, 2]


def test_allocate_largest_remainder():
    # 10 * [0.333, 0.333, 0.334] -> floors [3,3,3]=9, remainder 1 to the largest frac
    out = allocate_portions([0.333, 0.333, 0.334], 10)
    assert sum(out) == 10
    assert out == [3, 3, 4]


def test_plan_rows_formats_times_and_drops_zeros():
    split = [(240, 0.75), (1200, 0.25)]  # 04:00, 20:00
    rows = plan_rows(split, 12)
    assert rows == [("04:00", 9), ("20:00", 3)]


def test_plan_rows_drops_zero_portion_meals():
    split = [(0, 0.9), (720, 0.1)]  # 00:00 gets 2, 12:00 rounds to 0 at total=2
    rows = plan_rows(split, 2)
    assert rows == [("00:00", 2)]
