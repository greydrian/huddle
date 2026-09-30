"""
Which widgets the dashboard shows, and where (spec 10.3).

`layout_state` holds one row per widget: its saved Gridstack position,
`is_visible` (Admin → Display → Widgets) and `school_days_only`.

The grid uses Gridstack `float: true`, so nothing moves up by itself. Hiding
a widget therefore closes its gap here, on the server, and touches only the
widgets below it in its columns (`close_gap`); every other widget keeps its
position. A hidden row is never written while it's hidden (/api/layout skips
it), so its own grid_x/y/w/h are the position it had when it was hidden, and
`hide_moves` records what moved up. Showing it again moves those back down
and puts it back exactly (`show`), or, if the family has rearranged since,
puts it in the nearest free space of its size (`free_spot`).

"School days only" widgets are left out on a day that isn't a school day
(`term_dates.is_school_day`). That's display-only: the gap is closed in
memory for that day (`day_layout`) and never saved, and a drag saves only
what the family moved (`apply_drag`), never leaving the saved layout
overlapping.

The pure functions (no database) hold the rules; the async ones below
read and write `layout_state` through them.
"""

import json
from datetime import date

from app.database import family_today, get_setting, set_setting
from app.services import term_dates

# dashboard.html calls GridStack.init() without a `column` option, so the
# grid uses Gridstack's default of 12 columns.
GRID_COLUMNS = 12


def _box(row: dict) -> tuple[int, int, int, int]:
    return row["grid_x"], row["grid_y"], row["grid_w"], row["grid_h"]


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _fits(box: tuple[int, int, int, int], others: list[dict]) -> bool:
    return (
        box[0] >= 0
        and box[1] >= 0
        and box[0] + box[2] <= GRID_COLUMNS
        and not any(_overlaps(box, _box(o)) for o in others)
    )


def close_gap(rows: list[dict], gone: dict) -> list[dict]:
    """The widgets in `rows` (those still on the grid) after `gone` leaves it.

    Only the widgets hanging below `gone` move, straight up by `gone`'s
    height, into the space it left:
    - a widget directly below `gone` (nothing between them in a column);
    - then, top first, a widget directly below one that moved, so a stack
      under the gap moves up together, its order and spacing kept.
    A widget moves only as far as what's directly above it moved, in every
    column it spans (the top of the grid never moves), so it only ever moves
    into space `gone` or another moved widget left, never into a gap the
    family made: a widget wider than the gap stays put unless everything
    above it moved too. Returns copies; the rows passed in are untouched."""
    gone_id = gone["widget_id"]
    everything = [dict(r) for r in rows if r["widget_id"] != gone_id] + [dict(gone)]
    rise: dict[str, int] = {gone_id: gone["grid_h"]}
    for row in sorted(everything, key=lambda r: (r["grid_y"], r["grid_x"])):
        if row["widget_id"] == gone_id:
            continue
        x, y, w, _h = _box(row)
        limits = []
        for column in range(x, x + w):
            above = [
                o
                for o in everything
                if o is not row and o["grid_x"] <= column < o["grid_x"] + o["grid_w"] and o["grid_y"] + o["grid_h"] <= y
            ]
            if not above:
                limits.append(0)  # the top of the grid
                break
            nearest = max(above, key=lambda o: o["grid_y"] + o["grid_h"])
            limits.append(rise.get(nearest["widget_id"], 0))
        rise[row["widget_id"]] = min(limits)
    # Whatever was directly above a widget moved up at least as far as it
    # did, so nothing moves into another widget.
    placed = [r for r in everything if r["widget_id"] != gone_id]
    for row in placed:
        row["grid_y"] -= rise[row["widget_id"]]
    return placed


def free_spot(rows: list[dict], want: dict) -> tuple[int, int]:
    """(x, y) for `want` among `rows`: its own position if that's free,
    else the nearest free space of its size (fewest cells away), preferring
    its own columns when two are as near."""
    x0, y0, w, h = _box(want)
    w = max(1, min(w, GRID_COLUMNS))
    others = [r for r in rows if r["widget_id"] != want["widget_id"]]
    x0 = max(0, min(x0, GRID_COLUMNS - w))
    y0 = max(0, y0)
    if _fits((x0, y0, w, h), others):
        return x0, y0
    bottom = max((r["grid_y"] + r["grid_h"] for r in others), default=0)
    best = (x0, max(bottom, y0))  # below everything is always free
    best_key = (abs(best[1] - y0), 1, best[1], best[0])
    for y in range(0, bottom + 1):
        for x in range(0, GRID_COLUMNS - w + 1):
            key = (abs(x - x0) + abs(y - y0), x != x0, y, x)
            if key < best_key and _fits((x, y, w, h), others):
                best, best_key = (x, y), key
    return best


def _no_overlaps(rows: list[dict]) -> bool:
    return all(_fits(_box(r), rows[i + 1 :]) for i, r in enumerate(rows))


# --- Hiding and showing (pure) -------------------------------------------------------------


def hide(rows: list[dict], gone: dict) -> tuple[list[dict], dict[str, list[int]]]:
    """`rows` (the widgets on the grid) after `gone` is hidden, and what
    moved: {widget_id: [x, y before, y after, w, h]}, kept with the hidden
    row so showing it can put them back."""
    after = close_gap(rows, gone)
    before = {r["widget_id"]: r["grid_y"] for r in rows}
    moves = {
        r["widget_id"]: [r["grid_x"], before[r["widget_id"]], r["grid_y"], r["grid_w"], r["grid_h"]]
        for r in after
        if r["grid_y"] != before[r["widget_id"]]
    }
    return after, moves


def show(rows: list[dict], widget: dict, moves: dict[str, list[int]] | None) -> list[dict]:
    """`rows` (the widgets on the grid) with `widget` shown again.

    If every widget that moved up when it was hidden is still exactly where
    that left it, they go back down and it goes back in its place: hide then
    show changes nothing. If the family has rearranged meanwhile so that
    can't be done cleanly, nothing else moves and it goes in its saved
    place if free, else the nearest free space (`free_spot`)."""
    by_id = {r["widget_id"]: dict(r) for r in rows}
    if moves:
        undone = dict(by_id)
        for widget_id, (x, y_before, y_after, w, h) in moves.items():
            row = undone.get(widget_id)
            if row is None or _box(row) != (x, y_after, w, h):
                break
            undone[widget_id] = {**row, "grid_y": y_before}
        else:
            candidate = [*undone.values(), dict(widget)]
            if _fits(_box(widget), []) and _no_overlaps(candidate):
                return candidate
    placed = dict(widget)
    placed["grid_x"], placed["grid_y"] = free_spot(list(by_id.values()), widget)
    return [*by_id.values(), placed]


# --- Saving a drag (pure) -----------------------------------------------------------------


def apply_drag(saved: list[dict], shown: list[dict], posted: dict[str, tuple[int, int, int, int]]) -> list[dict]:
    """The saved layout (`saved`: every visible widget's saved row) after the
    page posts its grid (`posted`: {widget_id: box} for the widgets it shows,
    `shown` being where the server showed them).

    Only a widget the family moved is written; one still where the wall shows
    it keeps its saved position (on a day off, "school days only" widgets'
    gaps are closed for that day only, so a shown position can differ from
    the saved one). A moved widget can then land on the saved place of one
    that's only moved up for today, or of a widget that's off today: that one
    is re-placed (`free_spot`), so the saved layout never overlaps and the
    next school day shows it cleanly. Returns copies."""
    shown_boxes = {r["widget_id"]: _box(r) for r in shown}
    moved = {wid: box for wid, box in posted.items() if wid in shown_boxes and box != shown_boxes[wid]}
    rows = []
    for row in saved:
        row = dict(row)
        if row["widget_id"] in moved:
            row["grid_x"], row["grid_y"], row["grid_w"], row["grid_h"] = moved[row["widget_id"]]
        rows.append(row)
    moved_rows = [r for r in rows if r["widget_id"] in moved]
    for row in sorted((r for r in rows if r["widget_id"] not in moved), key=lambda r: (r["grid_y"], r["grid_x"])):
        if not _fits(_box(row), moved_rows):
            row["grid_x"], row["grid_y"] = free_spot([r for r in rows if r is not row], row)
    return rows


# --- What the wall shows on a day (pure) --------------------------------------------------


def day_layout(rows: list[dict], school_day: bool) -> list[dict]:
    """The visible `rows` as the wall shows them on a school day or not.

    On a day that isn't a school day, "school days only" widgets are left
    out and their gaps closed, for that day only. On a school day they're at
    their saved position, or the nearest free space if another widget is
    there."""
    part_time = [r for r in rows if r.get("school_days_only")]
    if not part_time:
        return [dict(r) for r in rows]
    if not school_day:
        for row in sorted(part_time, key=lambda r: (r["grid_y"], r["grid_x"]), reverse=True):
            rows = close_gap(rows, row)
        return rows
    placed = [dict(r) for r in rows if not r.get("school_days_only")]
    for row in sorted(part_time, key=lambda r: (r["grid_y"], r["grid_x"])):
        row = dict(row)
        row["grid_x"], row["grid_y"] = free_spot(placed, row)
        placed.append(row)
    return placed


# --- Reading ------------------------------------------------------------------------------

LAYOUT_GENERATION_SETTING = "layout_generation"
COLUMNS = "widget_id, grid_x, grid_y, grid_w, grid_h, is_visible, school_days_only, hide_moves"


async def _rows(db) -> list[dict]:
    """Every registered widget's row (a widget no longer in the registry is ignored)."""
    from app.widgets import WIDGETS

    rows = await (await db.execute(f"SELECT {COLUMNS} FROM layout_state")).fetchall()  # noqa: S608
    return [dict(r) for r in rows if r["widget_id"] in WIDGETS]


async def admin_widgets(db) -> list[dict]:
    """Every registered widget, in registry order, with its two switches."""
    from app.widgets import WIDGETS

    rows = {r["widget_id"]: r for r in await _rows(db)}
    return [
        {
            "id": widget_id,
            "label": widget.label,
            "visible": bool(rows.get(widget_id, {}).get("is_visible", 1)),
            "school_days_only": bool(rows.get(widget_id, {}).get("school_days_only", 0)),
        }
        for widget_id, widget in WIDGETS.items()
    ]


async def shown_layout(db, today: date | None = None) -> list[dict]:
    """The rows the dashboard shows today, at the positions it shows them:
    hidden widgets left out (their gap was closed when they were hidden),
    and "school days only" ones as `day_layout` says."""
    rows = [r for r in await _rows(db) if r["is_visible"]]
    if not any(r["school_days_only"] for r in rows):
        return rows
    return day_layout(rows, await term_dates.is_school_day(db, today or await family_today(db)))


async def shown_ids(db, today: date | None = None) -> list[str]:
    return sorted(r["widget_id"] for r in await shown_layout(db, today))


async def generation(db, shown: list[str] | None = None) -> str:
    """Identifies the layout a page was rendered with: bumped by every Admin
    change (hide, show, school days only), plus which widgets are shown and
    the family's date. A page whose generation is out of date is stale:
    /api/layout refuses its save (409) and fresh.js reloads it."""
    counter = await get_setting(db, LAYOUT_GENERATION_SETTING) or "0"
    shown = await shown_ids(db) if shown is None else shown
    return f"{counter}-{(await family_today(db)).isoformat()}-{'.'.join(sorted(shown))}"


async def _bump_generation(db) -> None:
    counter = int(await get_setting(db, LAYOUT_GENERATION_SETTING) or "0")
    await set_setting(db, LAYOUT_GENERATION_SETTING, str(counter + 1))


# --- Writing ------------------------------------------------------------------------------


async def _write_positions(db, before: list[dict], after: list[dict]) -> None:
    old = {r["widget_id"]: _box(r) for r in before}
    await db.executemany(
        "UPDATE layout_state SET grid_x = ?, grid_y = ?, grid_w = ?, grid_h = ? WHERE widget_id = ?",
        [(*_box(r), r["widget_id"]) for r in after if old.get(r["widget_id"]) != _box(r)],
    )


async def hide_widget(db, widget_id: str) -> None:
    """Hide it and move the widgets below it up into its space. Its own row
    keeps its position, and what moved, for showing it again. Doesn't commit."""
    rows = {r["widget_id"]: r for r in await _rows(db)}
    row = rows.get(widget_id)
    if row is None or not row["is_visible"]:
        return
    visible = [r for r in rows.values() if r["is_visible"] and r["widget_id"] != widget_id]
    after, moves = hide(visible, row)
    await _write_positions(db, visible, after)
    await db.execute(
        "UPDATE layout_state SET is_visible = 0, hide_moves = ? WHERE widget_id = ?",
        (json.dumps(moves), widget_id),
    )
    await _bump_generation(db)


async def show_widget(db, widget_id: str) -> None:
    """Show it again: back where it was with the widgets below it moved back
    down, or (after a rearrangement) the nearest free space. Doesn't commit."""
    rows = {r["widget_id"]: r for r in await _rows(db)}
    row = rows.get(widget_id)
    if row is None or row["is_visible"]:
        return
    try:
        moves = json.loads(row["hide_moves"] or "{}")
    except ValueError:
        moves = {}
    visible = [r for r in rows.values() if r["is_visible"]]
    after = show(visible, row, moves)
    await _write_positions(db, visible, [r for r in after if r["widget_id"] != widget_id])
    placed = next(r for r in after if r["widget_id"] == widget_id)
    await db.execute(
        "UPDATE layout_state SET is_visible = 1, hide_moves = NULL, grid_x = ?, grid_y = ? WHERE widget_id = ?",
        (placed["grid_x"], placed["grid_y"], widget_id),
    )
    await _bump_generation(db)


async def set_school_days_only(db, widget_id: str, enabled: bool) -> None:
    await db.execute(
        "UPDATE layout_state SET school_days_only = ? WHERE widget_id = ?", (1 if enabled else 0, widget_id)
    )
    await _bump_generation(db)


async def save_positions(db, posted: dict[str, tuple[int, int, int, int]]) -> None:
    """A drag on the dashboard (/api/layout), through `apply_drag`. Hidden
    widgets are never written. Doesn't commit."""
    saved = [r for r in await _rows(db) if r["is_visible"]]
    after = apply_drag(saved, await shown_layout(db), posted)
    await _write_positions(db, saved, after)
