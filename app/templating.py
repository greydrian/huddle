import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from app import database

ONSCREEN_KEYBOARD_SETTING = "onscreen_keyboard"


def onscreen_keyboard_context(request: Request) -> dict:
    """Exposes `onscreen_keyboard` to every template, so widget fragments
    re-rendered by their own routes keep inputmode="none" without each router
    having to pass it. Context processors can't await, hence a synchronous
    read-only lookup of one row in the local WAL database."""
    try:
        uri = f"{database.DB_PATH.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as conn:
            row = conn.execute(
                "SELECT value FROM app_settings WHERE key = ?", (ONSCREEN_KEYBOARD_SETTING,)
            ).fetchone()
    except sqlite3.Error:
        return {"onscreen_keyboard": False}
    return {"onscreen_keyboard": bool(row and row[0] == "1")}


templates = Jinja2Templates(
    directory=Path(__file__).parent / "templates",
    context_processors=[onscreen_keyboard_context],
)
