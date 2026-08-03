"""Pure: turn (current enabled rows, target rows) into update/add/remove actions."""
from __future__ import annotations

_DEFAULT_REPEAT = "[1,2,3,4,5,6,7]"


def diff_schedule(current_enabled: list[dict], target_rows: list[tuple[str, int]]) -> dict:
    """Match current rows to target rows BY TIME, never by position.

    The device rejects any write that would leave two enabled plans sharing an
    executionTime (1212 CURRENT_TIME_PLAN_EXIST), and writes land one call at a time,
    so intermediate states are visible to it. Pairing by index meant rewriting a row's
    time onto a slot a not-yet-rewritten row still held — a mid-apply rejection.

    Matching by time makes the collision structurally impossible, given the
    removes -> updates -> adds order in tools._apply_actions:
      - updates keep executionTime fixed and only change grainNum, so they can never
        land on another row's slot;
      - removes are exactly the rows whose time is NOT in the target, so they run
        first and free every slot the target no longer wants;
      - adds are exactly the times NO current row holds, and they run last, so the
        slot is guaranteed free by then.
    Add times and remove times are disjoint by construction, so no add can race a
    remove either.
    """
    cur_by_time: dict[str, dict] = {}
    for row in sorted(current_enabled, key=lambda p: p.get("executionTime", "")):
        cur_by_time.setdefault(row.get("executionTime"), row)

    target: dict[str, int] = {}
    for time, grain in target_rows:
        # A zero-portion row means "no meal at this time", matching planner.plan_rows
        # which drops them. Keeping it would write a useless grainNum=0 row instead of
        # removing the meal, and the two paths would disagree about what zero means.
        if grain <= 0:
            continue
        target[time] = grain  # last wins; the planner does not emit duplicate times

    template = min(current_enabled, key=lambda p: p.get("executionTime", ""),
                   default={}) if current_enabled else {}

    updates: list[dict] = []
    adds: list[dict] = []
    removes: list[int] = []

    for time in sorted(target):
        grain = target[time]
        row = cur_by_time.get(time)
        if row is None:
            adds.append({
                "executionTime": time,
                "grainNum": grain,
                "repeatDay": template.get("repeatDay", _DEFAULT_REPEAT),
                "label": template.get("label", ""),
                "enableAudio": template.get("enableAudio", False),
                "audioTimes": template.get("audioTimes", 2),
            })
        else:
            updates.append({
                "id": row["id"],
                "executionTime": time,          # unchanged — that is the whole point
                "grainNum": grain,
                "repeatDay": row.get("repeatDay", _DEFAULT_REPEAT),
                "label": row.get("label", ""),
                "enableAudio": row.get("enableAudio", False),
                "audioTimes": row.get("audioTimes", 2),
                "enable": True,
            })

    for row in current_enabled:
        if row.get("executionTime") not in target:
            removes.append(row["id"])

    return {"updates": updates, "adds": adds, "removes": removes}
