from petlibro_mcp.schedule_diff import diff_schedule


def _cur(id, t, g):
    return {"id": id, "executionTime": t, "grainNum": g,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "L",
            "enableAudio": True, "audioTimes": 2}


def test_equal_counts_all_updates():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("07:30", 4), ("21:00", 1)]
    d = diff_schedule(current, target)
    assert d["removes"] == [] and d["adds"] == []
    assert [(u["id"], u["executionTime"], u["grainNum"]) for u in d["updates"]] == [
        (1, "07:30", 4), (2, "21:00", 1)]
    # preserved fields carried through
    assert d["updates"][0]["repeatDay"] == "[1,2,3,4,5,6,7]"
    assert d["updates"][0]["enableAudio"] is True and d["updates"][0]["enable"] is True


def test_target_longer_updates_plus_adds():
    current = [_cur(1, "08:00", 3)]
    target = [("08:00", 3), ("20:00", 2)]
    d = diff_schedule(current, target)
    assert [u["id"] for u in d["updates"]] == [1]
    assert d["removes"] == []
    assert len(d["adds"]) == 1
    add = d["adds"][0]
    assert add["executionTime"] == "20:00" and add["grainNum"] == 2
    assert add["repeatDay"] == "[1,2,3,4,5,6,7]"  # inherited from template row


def test_target_shorter_updates_plus_removes():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("08:00", 5)]
    d = diff_schedule(current, target)
    assert [u["id"] for u in d["updates"]] == [1]
    assert d["adds"] == []
    assert d["removes"] == [2]


def test_empty_target_removes_all():
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    d = diff_schedule(current, [])
    assert d["updates"] == [] and d["adds"] == []
    assert sorted(d["removes"]) == [1, 2]


def test_add_uses_defaults_when_no_current_template():
    d = diff_schedule([], [("09:00", 2)])
    assert d["updates"] == [] and d["removes"] == []
    assert d["adds"][0]["repeatDay"] == "[1,2,3,4,5,6,7]"
