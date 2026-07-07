from petlibro_mcp.rhythm import BINS, circadian_curve, find_peaks


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


def test_find_peaks_picks_separated_maxima():
    curve = [0.0] * 48
    curve[8] = 10.0    # 04:00
    curve[20] = 8.0    # 10:00
    curve[40] = 6.0    # 20:00
    peaks = find_peaks(curve, max_meals=6, min_separation_bins=3)
    assert peaks == [8, 20, 40]


def test_find_peaks_respects_max_meals():
    curve = [0.0] * 48
    for i, v in [(4, 10), (12, 9), (20, 8), (28, 7), (36, 6), (44, 5)]:
        curve[i] = float(v)
    peaks = find_peaks(curve, max_meals=3, min_separation_bins=3)
    assert peaks == [4, 12, 20]  # three tallest, still sorted by time


def test_find_peaks_enforces_separation():
    curve = [0.0] * 48
    curve[10] = 10.0
    curve[11] = 9.0   # adjacent -> excluded by separation
    curve[30] = 8.0
    peaks = find_peaks(curve, max_meals=6, min_separation_bins=3)
    assert peaks == [10, 30]


def test_find_peaks_empty_curve():
    assert find_peaks([0.0] * 48) == []


from petlibro_mcp.rhythm import split_at_peaks


def test_split_assigns_mass_to_nearest_peak():
    curve = [0.0] * 48
    curve[8] = 6.0     # bin 8 -> 04:00
    curve[40] = 2.0    # bin 40 -> 20:00
    split = split_at_peaks(curve, [8, 40])
    assert split[0][0] == 8 * 30   # 240 minutes = 04:00
    assert split[1][0] == 40 * 30  # 1200 minutes = 20:00
    assert round(split[0][1], 3) == 0.75
    assert round(split[1][1], 3) == 0.25
    assert round(sum(f for _, f in split), 6) == 1.0


def test_split_empty_peaks():
    assert split_at_peaks([1.0] * 48, []) == []
