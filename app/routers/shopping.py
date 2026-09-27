"""
Shopping list widget (Section 4.4). Data and Google Tasks sync queueing live
in app/services/shopping.py.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import shopping as shopping_service
from app.templating import templates

router = APIRouter()


@router.get("/widgets/shopping", response_class=HTMLResponse)
async def shopping_widget(request: Request):
    async with get_db() as db:
        items = await shopping_service.get_shopping_items(db)
    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping", response_class=HTMLResponse)
async def add_shopping_item(request: Request, title: str = Form(...)):
    async with get_db() as db:
        await shopping_service.add_item(db, title)
        items = await shopping_service.get_shopping_items(db)
    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping/{item_id}/toggle", response_class=HTMLResponse)
async def toggle_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        if not await shopping_service.toggle_item(db, item_id):
            return HTMLResponse(status_code=404, content="Item not found")
        items = await shopping_service.get_shopping_items(db)

    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})


@router.post("/api/shopping/{item_id}/delete", response_class=HTMLResponse)
async def delete_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        await shopping_service.delete_item(db, item_id)
        items = await shopping_service.get_shopping_items(db)

    return templates.TemplateResponse(request, "widgets/shopping.html", {"items": items})
