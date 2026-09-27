"""
Weather widget (Section 4.4). Fetching, caching and geocoding live in
app/services/weather.py.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.database import get_db
from app.services import weather as weather_service
from app.templating import templates

router = APIRouter()


@router.get("/widgets/weather", response_class=HTMLResponse)
async def weather_widget(request: Request):
    async with get_db() as db:
        weather = await weather_service.get_weather(db)
    return templates.TemplateResponse(request, "widgets/weather.html", {"weather": weather})
