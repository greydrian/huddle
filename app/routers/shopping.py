"""
Shopping list widget (Section 4.4, spec 10.8). Data, quantities, categories
and Google Tasks sync queueing live in app/services/shopping.py.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import shopping as shopping_service
from app.templating import templates

router = APIRouter()


async def _render(request: Request, db) -> HTMLResponse:
    return templates.TemplateResponse(request, "widgets/shopping.html", await shopping_service.widget_context(db))


@router.get("/widgets/shopping", response_class=HTMLResponse)
async def shopping_widget(request: Request):
    async with get_db() as db:
        return await _render(request, db)


@router.post("/api/shopping", response_class=HTMLResponse)
async def add_shopping_item(request: Request, title: str = Form(...)):
    async with get_db() as db:
        await shopping_service.add_item(db, title)
        return await _render(request, db)


@router.post("/api/shopping/{item_id}/toggle", response_class=HTMLResponse)
async def toggle_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        if not await shopping_service.toggle_item(db, item_id):
            return HTMLResponse(status_code=404, content="Item not found")
        return await _render(request, db)


@router.post("/api/shopping/{item_id}/delete", response_class=HTMLResponse)
async def delete_shopping_item(request: Request, item_id: int):
    async with get_db() as db:
        await shopping_service.delete_item(db, item_id)
        return await _render(request, db)


@router.post("/api/shopping/{item_id}/category", response_class=HTMLResponse)
async def set_shopping_category(request: Request, item_id: int, category: str = Form("")):
    """Corrects an item's aisle; remembered for its name next time (PIN-free,
    like the rest of the list)."""
    async with get_db() as db:
        if not await shopping_service.set_category(db, item_id, category):
            return HTMLResponse(status_code=404, content="Item or category not found")
        return await _render(request, db)
