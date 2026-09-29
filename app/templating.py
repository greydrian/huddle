from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import database
from app.appearance import person_ink
from app.services.homework import SUBJECTS


def onscreen_keyboard_context(request: Request) -> dict:
    """Exposes `onscreen_keyboard` to every template, so widget fragments
    re-rendered by their own routes keep the keyboard marker without each
    router having to pass it."""
    return {"onscreen_keyboard": database.onscreen_keyboard_enabled}


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[onscreen_keyboard_context],
)

templates.env.filters["person_ink"] = person_ink
# The fixed homework subjects (key -> (label, icon)) for the Admin and inbox selects.
templates.env.globals["homework_subjects"] = SUBJECTS
