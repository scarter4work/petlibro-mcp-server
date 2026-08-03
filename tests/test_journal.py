import json
from petlibro_mcp import journal


def test_append_then_read_back(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert journal.last_at_ms(p, "SN-A") == 1000


def test_last_at_ms_returns_the_newest_entry(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    journal.append(p, "SN-A", [("08:00", 1)], 1, 5000)
    assert journal.last_at_ms(p, "SN-A") == 5000


def test_entries_are_isolated_per_serial(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    journal.append(p, "SN-B", [("09:00", 3)], 3, 7000)
    assert journal.last_at_ms(p, "SN-A") == 1000
    assert journal.last_at_ms(p, "SN-B") == 7000


def test_missing_file_reads_as_none(tmp_path):
    assert journal.last_at_ms(tmp_path / "nope.jsonl", "SN-A") is None


def test_unknown_serial_reads_as_none(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert journal.last_at_ms(p, "SN-Z") is None


def test_append_creates_missing_parent_directory(tmp_path):
    p = tmp_path / "deep" / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    assert p.is_file()


def test_corrupt_line_does_not_break_reading(tmp_path):
    """A half-written line must not hide the good entries around it."""
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2)], 2, 1000)
    with p.open("a") as fh:
        fh.write("{not json\n")
    journal.append(p, "SN-A", [("08:00", 1)], 1, 9000)
    assert journal.last_at_ms(p, "SN-A") == 9000


def test_line_shape_is_stable(tmp_path):
    p = tmp_path / "j.jsonl"
    journal.append(p, "SN-A", [("08:00", 2), ("20:00", 3)], 5, 1234)
    row = json.loads(p.read_text().splitlines()[0])
    assert row == {"serial": "SN-A", "at_ms": 1234, "total": 5,
                   "schedule": [["08:00", 2], ["20:00", 3]]}
