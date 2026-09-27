"""
Homework and practice-words widgets. Ticking homework done and marking a
word list "practised today" are PIN-free kiosk taps, like tasks. Data,
validation and the "today" rules live in app/services/homework.py.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import homework as homework_service
from app.templating import templates

router = APIRouter()


@router.get("/widgets/homework", response_class=HTMLResponse)
async def homework_widget(request: Request):
    async with get_db() as db:
        homework_groups = await homework_service.get_homework_groups(db)
    return templates.TemplateResponse(request, "widgets/homework.html", {"homework_groups": homework_groups})


@router.post("/api/homework/{homework_id}/toggle", response_class=HTMLResponse)
async def toggle_homework(request: Request, homework_id: int):
    async with get_db() as db:
        if not await homework_service.toggle_homework(db, homework_id):
            return HTMLResponse(status_code=404, content="Homework not found")
        homework_groups = await homework_service.get_homework_groups(db)
    return templates.TemplateResponse(request, "widgets/homework.html", {"homework_groups": homework_groups})


@router.get("/widgets/practice-words", response_class=HTMLResponse)
async def practice_words_widget(request: Request):
    async with get_db() as db:
        context = await homework_service.practice_context(db)
    return templates.TemplateResponse(request, "widgets/practice_words.html", context)


@router.post("/api/practice-words/{list_id}/practised", response_class=HTMLResponse)
async def toggle_practised(request: Request, list_id: int):
    async with get_db() as db:
        if not await homework_service.toggle_practised(db, list_id):
            return HTMLResponse(status_code=404, content="Word list not found")
        context = await homework_service.practice_context(db)
    return templates.TemplateResponse(request, "widgets/practice_words.html", context)
