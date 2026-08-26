"""
Meal planning widget (Section 4.5) — simple text-based plan per day,
no recipe database, editable inline from the display.
"""

from datetime import date, timedelta

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.templating import templates

router = APIRouter()


async def get_week_meal_plan(db, start: date | None = None):
    """Return the next 7 days as a list of {date, weekday, description}."""
    start = start or date.today()
    days = [start + timedelta(days=i) for i in range(7)]
    rows = await (await db.execute(
        "SELECT * FROM meal_plans WHERE date IN ({})".format(
            ",".join("?" for _ in days)
        ),
        [d.isoformat() for d in days],
    )).fetchall()
    plan_by_date = {row["date"]: row["meal_description"] for row in rows}

    return [
        {
            "date": d.isoformat(),
            "weekday": d.strftime("%A"),
            "is_today": d == date.today(),
            "description": plan_by_date.get(d.isoformat(), ""),
        }
        for d in days
    ]


@router.get("/widgets/meals", response_class=HTMLResponse)
async def meals_widget(request: Request):
    async with get_db() as db:
        days = await get_week_meal_plan(db)
    return templates.TemplateResponse(request, "widgets/meals.html", {"days": days})


@router.post("/api/meals/{meal_date}", response_class=HTMLResponse)
async def update_meal(request: Request, meal_date: str, description: str = Form(...)):
    async with get_db() as db:
        await db.execute(
            """INSERT INTO meal_plans (date, meal_description) VALUES (?, ?)
               ON CONFLICT(date) DO UPDATE SET meal_description = excluded.meal_description""",
            (meal_date, description.strip()),
        )
        await db.commit()
        days = await get_week_meal_plan(db)

    return templates.TemplateResponse(request, "widgets/meals.html", {"days": days})
