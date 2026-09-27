"""
Meal planning widget (Section 4.5) — simple text-based plan per day,
no recipe database, editable inline from the display. Data lives in
app/services/meals.py.
"""

from datetime import date

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services.meals import get_week_meal_plan, set_meal
from app.templating import templates

router = APIRouter()


@router.get("/widgets/meals", response_class=HTMLResponse)
async def meals_widget(request: Request):
    async with get_db() as db:
        days = await get_week_meal_plan(db)
    return templates.TemplateResponse(request, "widgets/meals.html", {"days": days})


@router.post("/api/meals/{meal_date}", response_class=HTMLResponse)
async def update_meal(request: Request, meal_date: date, description: str = Form(...)):
    # Typed as `date` so FastAPI 422s anything that isn't YYYY-MM-DD before
    # it can land in meal_plans as a junk key.
    async with get_db() as db:
        await set_meal(db, meal_date, description)
        days = await get_week_meal_plan(db)

    return templates.TemplateResponse(request, "widgets/meals.html", {"days": days})
