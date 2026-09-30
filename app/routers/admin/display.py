"""Display tab: which widgets show, the weather location, appearance, the
notification banner, the on-screen keyboard. The idle screen and Photos
panels are routers/photos.py (their context comes from there too)."""

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse

from app import appearance, idle
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.database import get_db, set_onscreen_keyboard
from app.routers import photos as photos_admin
from app.routers.admin import common
from app.routers.admin.common import admin_error, tab_context
from app.services import banners, layout, term_dates, weather
from app.widgets import WIDGETS

router = APIRouter(prefix="/admin")


@tab_context("display")
async def display_context(db, base: dict, extra: dict) -> dict:
    return {
        "weather_location": await weather.get_location(db),
        "weather_error": extra.get("weather_error"),
        "appearance_setting": await appearance.get_appearance(db),
        "appearances": appearance.APPEARANCES,
        "widget_settings": await layout.admin_widgets(db),
        "school_day_today": await term_dates.is_school_day(db, await common.family_today(db)),
        "banner_settings": await banners.get_settings(db),
        "banner_triggers": banners.TRIGGERS,
        "banner_lead_range": (banners.MIN_LEAD, banners.MAX_LEAD),
        "idle_settings": await idle.get_settings(db),
        "idle_modes": idle.MODES,
        "idle_ranges": idle.RANGES,
        **await photos_admin.panel_context(db),
    }


# --- Weather ---


@router.post("/weather-location", dependencies=[Depends(require_admin)])
async def save_weather_location(place: str = Form(...)):
    try:
        location = await weather.geocode(place)
    # A malformed geocoder response is treated like an outage, never a 500.
    except httpx.HTTPError, KeyError, IndexError, ValueError, TypeError, AttributeError:
        return RedirectResponse(url=admin_url("weather", weather_error="offline"), status_code=303)
    if not location:
        return RedirectResponse(url=admin_url("weather", weather_error="notfound"), status_code=303)
    async with get_db() as db:
        await weather.set_location(db, location)
    return RedirectResponse(url=admin_url("weather"), status_code=303)


# --- Display ---


@router.post("/onscreen-keyboard", dependencies=[Depends(require_admin)])
async def save_onscreen_keyboard(enabled: bool = Form(False)):
    async with get_db() as db:
        await set_onscreen_keyboard(db, enabled)
    return RedirectResponse(url=admin_url("keyboard"), status_code=303)


@router.post("/widgets/{widget_id}/visibility", dependencies=[Depends(require_admin)])
async def save_widget_visibility(widget_id: str, visible: bool = Form(False)):
    """Show or hide a widget (spec 10.3). Hiding closes its gap; showing puts
    it back where it was, or the nearest free space."""
    if widget_id not in WIDGETS:
        return admin_error("widget-missing")
    async with get_db() as db:
        await (layout.show_widget if visible else layout.hide_widget)(db, widget_id)
        await db.commit()
    return RedirectResponse(url=admin_url("widgets"), status_code=303)


@router.post("/widgets/{widget_id}/school-days", dependencies=[Depends(require_admin)])
async def save_widget_school_days(widget_id: str, enabled: bool = Form(False)):
    if widget_id not in WIDGETS:
        return admin_error("widget-missing")
    async with get_db() as db:
        await layout.set_school_days_only(db, widget_id, enabled)
        await db.commit()
    return RedirectResponse(url=admin_url("widgets"), status_code=303)


@router.post("/appearance", dependencies=[Depends(require_admin)])
async def save_appearance(value: str = Form("")):
    if value not in appearance.APPEARANCES:
        return admin_error("appearance")
    async with get_db() as db:
        await appearance.set_appearance(db, value)
    return RedirectResponse(url=admin_url("display"), status_code=303)


@router.post("/banners", dependencies=[Depends(require_admin)])
async def save_banners(request: Request):
    """The notification banner's triggers, sounds, default lead time and
    quiet hours (spec 10.1)."""
    form = await request.form()
    try:
        settings = banners.clean_settings({key: form.get(key) for key in form.keys()})
    except banners.SettingsError as exc:
        return admin_error(exc.code)
    async with get_db() as db:
        await banners.save_settings(db, settings)
    return RedirectResponse(url=admin_url("banners"), status_code=303)
