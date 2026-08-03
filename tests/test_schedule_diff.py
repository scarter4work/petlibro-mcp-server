import pytest
from petlibro_mcp.schedule_diff import diff_schedule


def _cur(id, t, g):
    return {"id": id, "executionTime": t, "grainNum": g,
            "repeatDay": "[1,2,3,4,5,6,7]", "label": "L",
            "enableAudio": True, "audioTimes": 2}


class FakeDevice:
    """Models the one PetLibro rule that bit us: two enabled plans may never share an
    executionTime. The cloud rejects the write with 1212 CURRENT_TIME_PLAN_EXIST.
    Writes land one call at a time, so intermediate states are observable."""

    def __init__(self, rows):
        self.rows = {r["id"]: dict(r) for r in rows}
        self._next_id = max([r["id"] for r in rows], default=0) + 1

    def _reject_if_taken(self, time, ignore_id=None):
        for rid, row in self.rows.items():
            if rid != ignore_id and row["executionTime"] == time:
                raise RuntimeError("Code: 1212, Message: CURRENT_TIME_PLAN_EXIST")

    def update(self, plan):
        self._reject_if_taken(plan["executionTime"], ignore_id=plan["id"])
        self.rows[plan["id"]].update(plan)

    def add(self, plan):
        self._reject_if_taken(plan["executionTime"])
        self.rows[self._next_id] = {**plan, "id": self._next_id}
        self._next_id += 1

    def remove(self, plan_id):
        self.rows.pop(plan_id, None)

    def apply(self, actions):
        """Same order as tools._apply_actions: removes, then updates, then adds."""
        for pid in actions["removes"]:
            self.remove(pid)
        for plan in actions["updates"]:
            self.update(plan)
        for plan in actions["adds"]:
            self.add(plan)

    def schedule(self):
        return sorted((r["executionTime"], r["grainNum"]) for r in self.rows.values())


def test_shifting_a_row_later_does_not_collide():
    """Regression: 1212 CURRENT_TIME_PLAN_EXIST.

    07:00 must move to 12:00, but 12:00 is still occupied by the row below it.
    Index-pairing rewrites row 1 first and the device rejects the write mid-apply."""
    current = [_cur(1, "07:00", 3), _cur(2, "12:00", 2)]
    target = [("12:00", 3), ("18:00", 2)]
    device = FakeDevice(current)
    device.apply(diff_schedule(current, target))
    assert device.schedule() == [("12:00", 3), ("18:00", 2)]


def test_swapping_two_times_does_not_collide():
    """The pathological case: a straight swap. No in-place rewrite order avoids a
    collision, so the diff must not rely on rewriting times at all."""
    current = [_cur(1, "07:00", 1), _cur(2, "19:00", 5)]
    target = [("07:00", 5), ("19:00", 1)]
    device = FakeDevice(current)
    device.apply(diff_schedule(current, target))
    assert device.schedule() == [("07:00", 5), ("19:00", 1)]


@pytest.mark.parametrize("target", [
    [("12:00", 3), ("18:00", 2)],                        # both shift later
    [("05:00", 3), ("07:00", 2)],                        # both shift earlier
    [("12:00", 2), ("19:00", 1), ("22:00", 4)],          # shift later + grow
    [("07:00", 5)],                                       # shrink onto an existing time
    [("06:00", 1), ("07:00", 1), ("12:00", 1)],          # interleave around held times
])
def test_no_intermediate_collision_for_any_reshuffle(target):
    current = [_cur(1, "07:00", 3), _cur(2, "12:00", 2)]
    device = FakeDevice(current)
    device.apply(diff_schedule(current, target))
    assert device.schedule() == sorted(target)


def test_moved_times_become_remove_plus_add():
    """A row whose time changes is retired and recreated, never rewritten in place.
    Rewriting the time is what produced 1212 CURRENT_TIME_PLAN_EXIST."""
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("07:30", 4), ("21:00", 1)]
    d = diff_schedule(current, target)
    assert d["updates"] == []
    assert sorted(d["removes"]) == [1, 2]
    assert [(a["executionTime"], a["grainNum"]) for a in d["adds"]] == [
        ("07:30", 4), ("21:00", 1)]
    # row settings still inherited from the existing schedule
    assert d["adds"][0]["repeatDay"] == "[1,2,3,4,5,6,7]"
    assert d["adds"][0]["enableAudio"] is True


def test_same_time_changed_portions_is_an_in_place_update():
    """The common case: rhythm keeps the time, only the portion count moves."""
    current = [_cur(1, "08:00", 3), _cur(2, "20:00", 2)]
    target = [("08:00", 5), ("20:00", 2)]
    d = diff_schedule(current, target)
    assert d["removes"] == [] and d["adds"] == []
    assert [(u["id"], u["executionTime"], u["grainNum"]) for u in d["updates"]] == [
        (1, "08:00", 5), (2, "20:00", 2)]
    assert d["updates"][0]["enable"] is True


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


def test_zero_portion_target_removes_an_existing_row():
    current = [{"id": 7, "executionTime": "14:00", "grainNum": 2, "enable": True},
               {"id": 8, "executionTime": "22:00", "grainNum": 4, "enable": True}]
    actions = diff_schedule(current, [("14:00", 0), ("22:00", 4)])
    assert actions["removes"] == [7]
    assert [u["id"] for u in actions["updates"]] == [8]
    assert actions["adds"] == []


def test_zero_portion_target_is_never_added():
    actions = diff_schedule([], [("09:00", 0)])
    assert actions == {"updates": [], "adds": [], "removes": []}


def test_zero_portion_target_does_not_write_grainnum_zero():
    """Regression: a zeroed meal must not survive as a 0-portion plan row."""
    current = [{"id": 1, "executionTime": "10:00", "grainNum": 3, "enable": True}]
    actions = diff_schedule(current, [("10:00", 0)])
    assert all(u["grainNum"] != 0 for u in actions["updates"])
    assert all(a["grainNum"] != 0 for a in actions["adds"])
