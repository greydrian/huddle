"""
Widget layout persistence (Section 4.6): anyone can drag/resize widgets on
the dashboard, no PIN required. Gridstack fires a 'change' event with the
new positions; the frontend posts that here as JSON and we persist it.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.database import get_db

router = APIRouter()


@router.post("/api/layout")
async def save_layout(request: Request):
    body = await request.json()
    items = body.get("items", [])

    async with get_db() as db:
        for item in items:
            await db.execute(
                """UPDATE layout_state
                   SET grid_x = ?, grid_y = ?, grid_w = ?, grid_h = ?
                   WHERE widget_id = ?""",
                (item["x"], item["y"], item["w"], item["h"], item["id"]),
            )
        await db.commit()

    return JSONResponse({"status": "ok"})


@router.post("/api/layout/{widget_id}/visibility")
async def set_widget_visibility(widget_id: str, request: Request):
    body = await request.json()
    visible = bool(body.get("visible", True))

    async with get_db() as db:
        await db.execute(
            "UPDATE layout_state SET is_visible = ? WHERE widget_id = ?",
            (int(visible), widget_id),
        )
        await db.commit()

    return JSONResponse({"status": "ok"})
