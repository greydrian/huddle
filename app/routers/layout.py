"""
Widget layout persistence (Section 4.6): anyone can drag/resize widgets on
the dashboard, no PIN required. Gridstack fires a 'change' event with the
new positions; the frontend posts that here as JSON and we persist it.

Because the endpoint is unauthenticated, input is checked before anything is
written (a stored x:"a" would render as a broken gs-x attribute), but
per item: one bad item must never block the rest of the save, or a single
off-spec row would make every future drag silently fail to persist.

- A body that isn't {"items": [<object>, ...]} at all -> 422, nothing written.
- An item with an unknown widget id, a missing key or a non-integer value is
  skipped (there is no sensible value to substitute).
- Integers out of range are clamped onto the grid rather than dropped, so an
  off-spec position heals itself on the next drag instead of being stuck.

Only positions the family actually changed are written (spec 10.3,
services/layout.apply_drag):
- a hidden widget's row is never written (its position is where it goes back
  to when shown again);
- a widget still where the wall shows it keeps its saved position. On a day
  that isn't a school day the wall closes "school days only" widgets' gaps for
  that day only; a drag must not save that, nor leave rows overlapping.
- The page sends the layout `generation` it was rendered with. A page that's
  out of date (a widget hidden or shown in Admin since, or a new day) gets 409
  and nothing is written: its positions would undo the change. It reloads.
  A body without one (a page from before this check) is still accepted.
"""

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, ValidationError

from app.database import DEFAULT_LAYOUT, get_db
from app.services import layout
from app.services.layout import GRID_COLUMNS

router = APIRouter()
MAX_ROW = 500  # y + h ceiling: the dashboard scrolls, but nothing sane is this tall

# Built from the registry at import time, which a static checker can't follow.
WidgetId = Literal[tuple(widget_id for widget_id, *_ in DEFAULT_LAYOUT)]  # type: ignore[valid-type]


class LayoutItem(BaseModel):
    id: WidgetId  # type: ignore[valid-type]
    x: StrictInt
    y: StrictInt
    w: StrictInt
    h: StrictInt


class LayoutPayload(BaseModel):
    items: list[dict[str, Any]]
    generation: str | None = None


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _fit_to_grid(item: LayoutItem) -> tuple[int, int, int, int]:
    w = _clamp(item.w, 1, GRID_COLUMNS)
    x = _clamp(item.x, 0, GRID_COLUMNS - w)
    h = _clamp(item.h, 1, MAX_ROW)
    y = _clamp(item.y, 0, MAX_ROW - h)
    return x, y, w, h


@router.post("/api/layout")
async def save_layout(payload: LayoutPayload):
    valid, skipped = [], 0
    for raw in payload.items:
        try:
            item = LayoutItem.model_validate(raw)
        except ValidationError:
            skipped += 1
            continue
        valid.append((*_fit_to_grid(item), item.id))

    async with get_db() as db:
        if payload.generation is not None and payload.generation != await layout.generation(db):
            return JSONResponse({"status": "stale"}, status_code=409)
        await layout.save_positions(db, {item_id: (x, y, w, h) for x, y, w, h, item_id in valid})
        await db.commit()

    return JSONResponse({"status": "ok", "saved": len(valid), "skipped": skipped})
