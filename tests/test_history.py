from petlibro_mcp.history import parse_duration, parse_work_record, time_of_day_minutes


def test_parse_duration_variants():
    assert parse_duration("01m37s") == 97
    assert parse_duration("24s") == 24
    assert parse_duration("1h02m03s") == 3723
    assert parse_duration("") == 0
    assert parse_duration("garbage") == 0


def test_parse_work_record_splits_eats_and_dispenses():
    days = [{
        "recordTime": "2026/07/07",
        "workRecords": [
            {"type": "DETECTION_EVENT",
             "eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
             "recordTime": 1783427269000,
             "params": '{"petName":"Saffron","seconds":"01m37s"}',
             "content": "Saffron came to eat and ate for 01m37s."},
            {"type": "GRAIN_OUTPUT_SUCCESS", "eventType": "FEEDING_PLAN_SUCCESS",
             "recordTime": 1783422014000, "actualGrainNum": 3, "expectGrainNum": 3},
        ],
    }]
    eats, dispenses = parse_work_record(days)
    assert eats == [(1783427269.0, 97)]
    assert dispenses == [(1783422014.0, 3)]


def test_parse_work_record_falls_back_to_content_for_duration():
    days = [{"workRecords": [
        {"eventType": "PET_IDENTIFY_LEAVE_EVENT_BIND_PET",
         "recordTime": 1000000, "params": "",
         "content": "Rico came to eat and ate for 24s."},
    ]}]
    eats, _ = parse_work_record(days)
    assert eats == [(1000.0, 24)]


def test_parse_work_record_handles_empty():
    assert parse_work_record([]) == ([], [])
    assert parse_work_record(None) == ([], [])


def test_time_of_day_minutes_uses_local_tz():
    # 1783427269 -> 2026-07-07 08:27 America/Indiana/Indianapolis (EDT, DST active)
    assert time_of_day_minutes(1783427269, "America/Indiana/Indianapolis") == 8 * 60 + 27
