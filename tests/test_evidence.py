from petlibro_mcp.evidence import (
    MIN_EVIDENCE_DAYS, MS_PER_DAY,
    last_change_at, may_trim, split_events, verdict, window_days,
)
from petlibro_mcp.history import Event


def test_last_change_at_prefers_the_journal():
    plans = [{"updateTime": 1000, "enable": True}]
    assert last_change_at(plans, 9999) == 9999


def test_last_change_at_falls_back_to_max_update_time():
    plans = [{"updateTime": 1000, "enable": True},
             {"updateTime": 3000, "enable": True}]
    assert last_change_at(plans, None) == 3000


def test_last_change_at_ignores_disabled_rows():
    plans = [{"updateTime": 1000, "enable": True},
             {"updateTime": 9_000_000, "enable": False}]
    assert last_change_at(plans, None) == 1000


def test_last_change_at_none_when_nothing_is_known():
    assert last_change_at([], None) is None


def test_split_events_partitions_on_the_change():
    before = Event("block", 600, 1000, 0)
    after = Event("block", 600, 5000, 0)
    at_change = Event("block", 600, 3000, 0)
    b, s = split_events([before, after, at_change], 3000)
    assert b == [before]
    assert s == [at_change, after]


def test_window_days_converts_ms():
    assert window_days(0, 3 * MS_PER_DAY) == 3.0


def test_window_days_never_negative():
    assert window_days(5 * MS_PER_DAY, 0) == 0.0


def test_verdict_too_early_dominates():
    assert verdict(1.0, 0.1, MIN_EVIDENCE_DAYS - 1) == "too_early"


def test_verdict_unavailable_without_a_before_window():
    assert verdict(None, 0.5, 30) == "unavailable"


def test_verdict_improving_on_a_clear_drop():
    assert verdict(1.0, 0.5, 30) == "improving"


def test_verdict_worse_on_a_clear_rise():
    assert verdict(1.0, 1.5, 30) == "worse"


def test_verdict_no_change_within_the_band():
    assert verdict(1.0, 1.0, 30) == "no_change"


def test_may_trim_requires_a_full_evidence_window():
    assert may_trim(MIN_EVIDENCE_DAYS) is True
    assert may_trim(MIN_EVIDENCE_DAYS - 0.5) is False
    assert may_trim(None) is False


def test_may_trim_does_not_depend_on_a_before_window():
    """bridget has 26 days under the current schedule but no usable 'before'.
    That is enough to trim on; the before/after is only for reporting."""
    assert may_trim(26) is True
