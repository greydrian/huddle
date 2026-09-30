"""
Family member task lists (Section 4.3, spec 10.4): the Tasks widget, its
tick endpoint and the PIN-free quick add. Data, groups and the daily reset
live in app/services/tasks.py.
"""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import tasks as task_service
from app.templating import templates

router = APIRouter()


async def _render(request: Request, db, **extra):
    context = await task_service.widget_context(db)
    return templates.TemplateResponse(request, "widgets/tasks.html", {**context, **extra})


@router.get("/widgets/tasks", response_class=HTMLResponse)
async def tasks_widget(request: Request):
    async with get_db() as db:
        return await _render(request, db)


@router.post("/api/tasks/{task_id}/toggle", response_class=HTMLResponse)
async def toggle_task(request: Request, task_id: int):
    async with get_db() as db:
        now_completed = await task_service.toggle_task(db, task_id)
        if now_completed is None:
            return HTMLResponse(status_code=404, content="Task not found")
        return await _render(request, db, just_completed_id=task_id if now_completed else None)


@router.post("/api/tasks", response_class=HTMLResponse)
async def quick_add_task(
    request: Request,
    profile_id: str = Form(""),
    title: str = Form(""),
    time_of_day: str = Form(""),
):
    """The widget's "+ Add". A refused add re-renders the widget with the
    form still open, what was typed, and a friendly message (200, so htmx
    swaps it in)."""
    async with get_db() as db:
        try:
            await task_service.quick_add(db, profile_id, title, time_of_day)
        except task_service.QuickAddError as exc:
            return await _render(
                request,
                db,
                add_error=exc.message,
                add_form={
                    "profile_id": profile_id,
                    "title": title[: task_service.MAX_TITLE],
                    "time_of_day": time_of_day,
                },
            )
        return await _render(request, db)
