from petlibro_mcp.trim import (
    MIN_DAILY_PORTIONS, TRIM_PER_JAM_PER_DAY,
    attribute_blocks, jam_rate, trim_amount, trim_schedule,
)


def test_jam_rate_is_blocks_per_day():
    assert jam_rate(29, 27) == 29 / 27


def test_jam_rate_of_empty_window_is_zero():
    assert jam_rate(5, 0) == 0.0


def test_trim_amount_scales_with_rate():
    # bridget: 1.07 jams/day -> round(2 * 1.07) == 2
    assert trim_amount(1.07, 12) == 2
    # zeus: 0.72/day -> round(1.44) == 1
    assert trim_amount(0.72, 12) == 1


def test_trim_amount_dead_band_protects_a_clean_feeder():
    """ricco jams 0.15/day; round(0.30) == 0, so he is never trimmed."""
    assert trim_amount(0.15, 12) == 0


def test_trim_amount_respects_the_floor():
    assert trim_amount(5.0, 9) == 9 - MIN_DAILY_PORTIONS  # only 1 portion of room
    assert trim_amount(5.0, MIN_DAILY_PORTIONS) == 0      # already at the floor


def test_trim_amount_never_negative_below_the_floor():
    assert trim_amount(2.0, 4) == 0


def test_trim_per_jam_constant_is_two():
    assert TRIM_PER_JAM_PER_DAY == 2


def test_attribute_blocks_counts_to_nearest_meal():
    blocks = [14 * 60 + 10, 14 * 60 - 5, 22 * 60 + 2]
    assert attribute_blocks(blocks, ["14:00", "22:00"]) == {"14:00": 2, "22:00": 1}


def test_attribute_blocks_wraps_around_midnight():
    """23:50 is 20 minutes from 00:10, not 23 hours from it."""
    assert attribute_blocks([23 * 60 + 50], ["00:10", "12:00"]) == {"00:10": 1}


def test_attribute_blocks_with_no_meals_is_empty():
    assert attribute_blocks([600], []) == {}


def test_trim_schedule_takes_from_the_jammiest_meal():
    rows = [("10:30", 3), ("14:00", 2), ("22:00", 4)]
    out = trim_schedule(rows, {"14:00": 10, "22:00": 9, "10:30": 1}, 2)
    assert out == [("10:30", 3), ("14:00", 1), ("22:00", 3)]


def test_trim_schedule_never_takes_a_meal_below_one():
    """14:00 is jammiest but already at 1, so the cut moves to 22:00."""
    rows = [("14:00", 1), ("22:00", 4)]
    out = trim_schedule(rows, {"14:00": 10, "22:00": 2}, 2)
    assert out == [("14:00", 1), ("22:00", 2)]


def test_trim_schedule_stops_when_every_meal_is_at_one():
    rows = [("08:00", 1), ("20:00", 1)]
    assert trim_schedule(rows, {"08:00": 5, "20:00": 5}, 3) == rows


def test_trim_schedule_of_zero_is_a_noop():
    rows = [("08:00", 2), ("20:00", 3)]
    assert trim_schedule(rows, {"08:00": 9}, 0) == rows


def test_trim_schedule_returns_time_order():
    rows = [("22:00", 4), ("06:00", 3)]
    out = trim_schedule(rows, {"22:00": 8}, 1)
    assert [t for t, _ in out] == ["06:00", "22:00"]


def test_trim_schedule_untouched_meals_keep_their_portions():
    rows = [("06:00", 3), ("14:00", 3)]
    out = trim_schedule(rows, {"14:00": 7}, 1)
    assert dict(out)["06:00"] == 3
