"""Pure: turn (current enabled rows, target rows) into update/add/remove actions."""
from __future__ import annotations

_DEFAULT_REPEAT = "[1,2,3,4,5,6,7]"


def diff_schedule(current_enabled: list[dict], target_rows: list[tuple[str, int]]) -> dict:
    """Edit-in-place diff: pair sorted current rows with sorted target rows by index."""
    cur = sorted(current_enabled, key=lambda p: p.get("executionTime", ""))
    tgt = sorted(target_rows, key=lambda r: r[0])
    template = cur[0] if cur else {}

    updates: list[dict] = []
    adds: list[dict] = []
    removes: list[int] = []

    n = min(len(cur), len(tgt))
    for i in range(n):
        row = cur[i]
        time, grain = tgt[i]
        updates.append({
            "id": row["id"],
            "executionTime": time,
            "grainNum": grain,
            "repeatDay": row.get("repeatDay", _DEFAULT_REPEAT),
            "label": row.get("label", ""),
            "enableAudio": row.get("enableAudio", False),
            "audioTimes": row.get("audioTimes", 2),
            "enable": True,
        })
    for i in range(n, len(tgt)):
        time, grain = tgt[i]
        adds.append({
            "executionTime": time,
            "grainNum": grain,
            "repeatDay": template.get("repeatDay", _DEFAULT_REPEAT),
            "label": template.get("label", ""),
            "enableAudio": template.get("enableAudio", False),
            "audioTimes": template.get("audioTimes", 2),
        })
    for i in range(n, len(cur)):
        removes.append(cur[i]["id"])

    return {"updates": updates, "adds": adds, "removes": removes}
