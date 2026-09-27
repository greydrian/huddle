"""
Family member task lists (Section 4.3): the Tasks widget and its tick
endpoint. Data and the daily reset live in app/services/tasks.py.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import tasks as task_service
from app.templating import templates

router = APIRouter()


@router.get("/widgets/tasks", response_class=HTMLResponse)
async def tasks_widget(request: Request):
    async with get_db() as db:
        profiles = await task_service.get_profiles_with_tasks(db)
    return templates.TemplateResponse(
        request, "widgets/tasks.html", {"profiles": profiles}
    )


@router.post("/api/tasks/{task_id}/toggle", response_class=HTMLResponse)
async def toggle_task(request: Request, task_id: int):
    async with get_db() as db:
        now_completed = await task_service.toggle_task(db, task_id)
        if now_completed is None:
            return HTMLResponse(status_code=404, content="Task not found")
        profiles = await task_service.get_profiles_with_tasks(db)

    return templates.TemplateResponse(
        request,
        "widgets/tasks.html",
        {"profiles": profiles, "just_completed_id": task_id if now_completed else None},
    )
