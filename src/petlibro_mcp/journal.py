"""Append-only record of the schedules this tool wrote.

The device's own updateTime dates most edits, but an apply that only REMOVES rows
leaves the survivors' updateTime stale — so the evidence window would be misdated
exactly when a schedule shrank. Journalling our own writes closes that gap; the
device remains the fallback for edits made in the phone app.

One JSON object per line, so a partial write costs at most the last entry.
"""
from __future__ import annotations
import json
from pathlib import Path

DEFAULT_JOURNAL_NAME = ".petlibro-journal.jsonl"


def append(path, serial: str, schedule: list[tuple[str, int]], total: int,
           at_ms: int) -> None:
    """Record one applied schedule. Creates the file and its directory if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"serial": serial, "at_ms": int(at_ms), "total": int(total),
           "schedule": [[t, int(g)] for t, g in schedule]}
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def last_at_ms(path, serial: str) -> int | None:
    """Newest recorded change time for `serial`, or None if there is none.

    Unparseable lines are skipped rather than raised: a truncated tail must not
    hide the good entries around it, and this feeds a reporting window, not a
    write path.
    """
    p = Path(path)
    if not p.is_file():
        return None
    newest: int | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("serial") != serial:
            continue
        at = row.get("at_ms")
        if isinstance(at, int) and (newest is None or at > newest):
            newest = at
    return newest
