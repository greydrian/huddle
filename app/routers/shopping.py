"""
Single shared shopping list (Section 4.4). Bidirectional Google Tasks sync
isn't wired up yet — see the note in tasks.py; the same sync_queue pattern
applies here so it's a drop-in once OAuth is configured.
"""

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.templating import templates

router = APIRouter()


async def get_shopping_items(db):
    rows = await (await db.execute(
        "SELECT * FROM shopping_items ORDER BY is_checked, created_at"
    )).fetchall()
    return [dict(row) for row in rows]


def _queue_sync(db, service: str, payload: dict):
    return db.execute(
        "INSERT INTO sync_queue (service, payload_json) VALUES (?, ?)",
        (service, json.dumps(payload)),
    )


@router.get("/widgets/shopping", response_class=HTMLResponse)
async def shopping_widget(request: Request):
    async with get_db() as db:
        items = await get_shopping_items(db)
    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping", response_class=HTMLResponse)
async def add_shopping_item(request: Request, title: str = Form(...)):
    title = title.strip()
    async with get_db() as db:
        if title:
            cursor = await db.execute(
                "INSERT INTO shopping_items (title) VALUES (?)", (title,)
            )
            await _queue_sync(db, "shopping", {"action": "add", "title": title})
            await db.commit()
        items = await get_shopping_items(db)
    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping/{item_id}/toggle", response_class=HTMLResponse)
async def toggle_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        cursor = await db.execute("SELECT * FROM shopping_items WHERE id = ?", (item_id,))
        item = await cursor.fetchone()
        if item is None:
            return HTMLResponse(status_code=404, content="Item not found")

        new_state = 0 if item["is_checked"] else 1
        await db.execute(
            "UPDATE shopping_items SET is_checked = ? WHERE id = ?", (new_state, item_id)
        )
        await _queue_sync(db, "shopping", {"item_id": item_id, "is_checked": bool(new_state)})
        await db.commit()
        items = await get_shopping_items(db)

    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping/{item_id}/delete", response_class=HTMLResponse)
async def delete_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        await db.execute("DELETE FROM shopping_items WHERE id = ?", (item_id,))
        await _queue_sync(db, "shopping", {"action": "delete", "item_id": item_id})
        await db.commit()
        items = await get_shopping_items(db)

    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})
