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
"""

from typing import Any, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictInt, ValidationError

from app.database import DEFAULT_LAYOUT, get_db

router = APIRouter()

# dashboard.html calls GridStack.init() without a `column` option, so the
# grid uses Gridstack's default of 12 columns.
GRID_COLUMNS = 12
MAX_ROW = 500  # y + h ceiling: the dashboard scrolls, but nothing sane is this tall

WidgetId = Literal[tuple(widget_id for widget_id, *_ in DEFAULT_LAYOUT)]


class LayoutItem(BaseModel):
    id: WidgetId
    x: StrictInt
    y: StrictInt
    w: StrictInt
    h: StrictInt


class LayoutPayload(BaseModel):
    items: list[dict[str, Any]]


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
        await db.executemany(
            """UPDATE layout_state
               SET grid_x = ?, grid_y = ?, grid_w = ?, grid_h = ?
               WHERE widget_id = ?""",
            valid,
        )
        await db.commit()

    return JSONResponse({"status": "ok", "saved": len(valid), "skipped": skipped})
