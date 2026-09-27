"""
Widget layout persistence (Section 4.6): anyone can drag/resize widgets on
the dashboard, no PIN required. Gridstack fires a 'change' event with the
new positions; the frontend posts that here as JSON and we persist it.

Because the endpoint is unauthenticated, every item is validated before
anything is written: a malformed body gets a 422 and the stored layout is
left untouched (a stored x:"a" would render as a broken gs-x attribute).
"""

from typing import Annotated, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt, model_validator

from app.database import DEFAULT_LAYOUT, get_db

router = APIRouter()

# dashboard.html calls GridStack.init() without a `column` option, so the
# grid uses Gridstack's default of 12 columns.
GRID_COLUMNS = 12
MAX_ROW = 500  # generous: the dashboard scrolls, but nothing sane is this tall

WidgetId = Literal[tuple(widget_id for widget_id, *_ in DEFAULT_LAYOUT)]


class LayoutItem(BaseModel):
    id: WidgetId
    x: Annotated[StrictInt, Field(ge=0, lt=GRID_COLUMNS)]
    y: Annotated[StrictInt, Field(ge=0, le=MAX_ROW)]
    w: Annotated[StrictInt, Field(ge=1, le=GRID_COLUMNS)]
    h: Annotated[StrictInt, Field(ge=1, le=MAX_ROW)]

    @model_validator(mode="after")
    def _fits_grid(self):
        if self.x + self.w > GRID_COLUMNS:
            raise ValueError(f"x + w must be <= {GRID_COLUMNS}")
        return self


class LayoutPayload(BaseModel):
    items: list[LayoutItem]


@router.post("/api/layout")
async def save_layout(payload: LayoutPayload):
    async with get_db() as db:
        for item in payload.items:
            await db.execute(
                """UPDATE layout_state
                   SET grid_x = ?, grid_y = ?, grid_w = ?, grid_h = ?
                   WHERE widget_id = ?""",
                (item.x, item.y, item.w, item.h, item.id),
            )
        await db.commit()

    return JSONResponse({"status": "ok"})
