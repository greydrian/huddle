"""
Meal planning widget (Section 4.5, spec 10.7): a text plan per day,
editable inline from the display, with a favourites picker and recipe
cards. Data lives in app/services/meals.py; the fetch in app/recipes.py.
"""

from datetime import date

import segno
from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import meals
from app.templating import templates

router = APIRouter()


@router.get("/widgets/meals", response_class=HTMLResponse)
async def meals_widget(request: Request):
    async with get_db() as db:
        context = await meals.widget_context(db)
    return templates.TemplateResponse(request, "widgets/meals.html", context)


@router.post("/api/meals/{meal_date}", response_class=HTMLResponse)
async def update_meal(request: Request, meal_date: date, description: str = Form(...)):
    # Typed as `date` so FastAPI 422s anything that isn't YYYY-MM-DD before
    # it can land in meal_plans as a junk key.
    async with get_db() as db:
        await meals.set_meal(db, meal_date, description)
        context = await meals.widget_context(db)
    return templates.TemplateResponse(request, "widgets/meals.html", context)


def _qr(url: str) -> str:
    """The link as an inline SVG QR code, to open the recipe on a phone."""
    return segno.make(url, error="m").svg_inline(scale=4, border=2, dark="#000", light="#fff")


@router.get("/meals/recipe", response_class=HTMLResponse)
async def recipe_card(request: Request, name: str = Query("", max_length=meals.MAX_MEAL)):
    """The recipe card overlay for a meal (PIN-free, like the widget): the
    card from its saved link, its notes, and a QR code for the link."""
    async with get_db() as db:
        recipe = await meals.recipe_card(db, name)
    qr = _qr(recipe["url"]) if recipe["url"] else None
    return templates.TemplateResponse(request, "_recipe_card.html", {"recipe": recipe, "qr_svg": qr})
