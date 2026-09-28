"""
Pen test page (spec §3, return-window checks 1 and 2; §10.10 pen input).

A diagnostic canvas for checking a new tablet in Fully Kiosk: does the pen
report pointerType "pen", pressure and hover, and can touch be ignored while
it's down? Everything happens in static/js/pen-test.js; nothing is sent back
or stored. Admin-only so it isn't a kiosk toy.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import appearance
from app.auth import require_admin
from app.database import get_db
from app.templating import templates

router = APIRouter()


@router.get("/pen-test", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def pen_test(request: Request):
    async with get_db() as db:
        mode = await appearance.current_mode(db)
    return templates.TemplateResponse(request, "pen_test.html", {"appearance": mode})
