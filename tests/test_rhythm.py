from petlibro_mcp.rhythm import BINS, circadian_curve


def test_curve_length_and_bucketing():
    # one visit at 08:00 (minute 480) -> bin 16 (480//30)
    curve = circadian_curve([(480, 10)], bins=48, smooth=0)
    assert len(curve) == 48
    assert curve[16] == 10.0
    assert sum(curve) == 10.0


def test_curve_weights_by_duration():
    curve = circadian_curve([(480, 1), (485, 4)], bins=48, smooth=0)
    assert curve[16] == 5.0  # both fall in bin 16


def test_smoothing_spreads_mass_circularly():
    curve = circadian_curve([(0, 9)], bins=48, smooth=1)
    # window of 3 centered on bin 0 wraps to bin 47
    assert curve[0] == 3.0
    assert curve[1] == 3.0
    assert curve[47] == 3.0
