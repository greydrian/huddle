"""Property checks for the layout rules in app/services/layout.py (spec
10.3), over many random layouts with fixed seeds: hide then show restores a
layout exactly and never drifts, and a drag on a day off never leaves the
saved layout (or the next school day's) overlapping."""

import random

from app.services import layout
from app.services.layout import GRID_COLUMNS


def _random_layout(rng: random.Random, n: int | None = None) -> list[dict]:
    """A non-overlapping layout: each widget dropped at a random spot, or
    the nearest free one."""
    rows: list[dict] = []
    for i in range(n or rng.randint(2, 9)):
        w = rng.choice([1, 2, 2, 3, 4, 4, 6, 8, 12])
        row = {
            "widget_id": f"w{i}",
            "grid_x": rng.randint(0, GRID_COLUMNS - w),
            "grid_y": rng.randint(0, 14),
            "grid_w": w,
            "grid_h": rng.randint(1, 5),
        }
        row["grid_x"], row["grid_y"] = layout.free_spot(rows, row)
        rows.append(row)
    return rows


def _boxes(rows):
    return {r["widget_id"]: (r["grid_x"], r["grid_y"], r["grid_w"], r["grid_h"]) for r in rows}


def _overlapping(rows) -> list[tuple[str, str]]:
    boxes = _boxes(rows)
    ids = sorted(boxes)
    return [(a, b) for i, a in enumerate(ids) for b in ids[i + 1 :] if layout._overlaps(boxes[a], boxes[b])]


def _hide(rows, widget_id):
    gone = next(r for r in rows if r["widget_id"] == widget_id)
    after, moves = layout.hide([r for r in rows if r is not gone], gone)
    return after, gone, moves


def test_hide_then_show_restores_exactly():
    rng = random.Random(103)
    moved_something = 0
    for _ in range(20_000):
        rows = _random_layout(rng)
        widget_id = rng.choice(rows)["widget_id"]
        after, gone, moves = _hide(rows, widget_id)
        assert not _overlapping(after)
        moved_something += bool(moves)
        assert _boxes(layout.show(after, gone, moves)) == _boxes(rows)
    assert moved_something > 2_000  # the interesting case is well covered


def test_repeated_cycles_never_drift():
    rng = random.Random(7)
    for _ in range(3_000):
        original = _random_layout(rng)
        rows = original
        for _cycle in range(5):
            after, gone, moves = _hide(rows, rng.choice(rows)["widget_id"])
            rows = layout.show(after, gone, moves)
        assert _boxes(rows) == _boxes(original)


def test_nested_hides_restore_in_reverse_order_and_never_overlap_otherwise():
    rng = random.Random(11)
    for _ in range(3_000):
        original = _random_layout(rng, rng.randint(3, 9))
        a, b = rng.sample([r["widget_id"] for r in original], 2)
        after_a, gone_a, moves_a = _hide(original, a)
        after_b, gone_b, moves_b = _hide(after_a, b)
        # Shown again last-hidden first: exactly as before.
        lifo = layout.show(layout.show(after_b, gone_b, moves_b), gone_a, moves_a)
        assert _boxes(lifo) == _boxes(original)
        # The other order may not restore exactly, but never overlaps.
        fifo = layout.show(layout.show(after_b, gone_a, moves_a), gone_b, moves_b)
        assert not _overlapping(fifo)


def test_a_drag_on_a_day_off_never_leaves_overlaps():
    rng = random.Random(5)
    dragged = 0
    for _ in range(3_000):
        saved = _random_layout(rng)
        for row in rng.sample(saved, rng.randint(1, max(1, len(saved) - 1))):
            row["school_days_only"] = 1
        shown = layout.day_layout(saved, school_day=False)
        if not shown:
            continue
        mover = rng.choice(shown)
        others = [r for r in shown if r is not mover]
        bottom = max((r["grid_y"] + r["grid_h"] for r in shown), default=0)
        spots = [
            (x, y)
            for y in range(bottom + 3)
            for x in range(GRID_COLUMNS - mover["grid_w"] + 1)
            if layout._fits((x, y, mover["grid_w"], mover["grid_h"]), others)
        ]
        x, y = rng.choice(spots)
        posted = _boxes(shown)
        posted[mover["widget_id"]] = (x, y, mover["grid_w"], mover["grid_h"])
        dragged += posted[mover["widget_id"]] != _boxes(shown)[mover["widget_id"]]

        after = layout.apply_drag(saved, shown, posted)
        assert not _overlapping(after)
        assert not _overlapping(layout.day_layout(after, school_day=True))  # the next school day
        assert not _overlapping(layout.day_layout(after, school_day=False))
        if posted[mover["widget_id"]] != _boxes(shown)[mover["widget_id"]]:
            assert _boxes(after)[mover["widget_id"]] == posted[mover["widget_id"]]  # the drag is kept
        before = _boxes(saved)
        for widget_id, box in _boxes(after).items():
            if widget_id != mover["widget_id"] and box != before[widget_id]:
                # Only a widget whose saved place the drag landed on is re-placed.
                assert layout._overlaps(before[widget_id], posted[mover["widget_id"]])
    assert dragged > 2_000
