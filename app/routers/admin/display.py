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
from app.services import banners, countdowns, layout, next_up, shopping, term_dates, weather
from app.widgets import WIDGETS

router = APIRouter(prefix="/admin")


@tab_context("display")
async def display_context(db, base: dict, extra: dict) -> dict:
    today = await common.family_today(db)
    return {
        "weather_location": await weather.get_location(db),
        "weather_error": extra.get("weather_error"),
        "appearance_setting": await appearance.get_appearance(db),
        "night_source": await appearance.night_source(db),
        "appearances": appearance.APPEARANCES,
        "widget_settings": await layout.admin_widgets(db),
        "school_day_today": await term_dates.family_school_day(db, today),
        "banner_settings": await banners.get_settings(db),
        "next_up_enabled": await next_up.is_enabled(db),
        "countdown_list": await countdowns.list_all(db),
        "countdown_today": today.isoformat(),
        "countdown_breaks": await countdowns.breaks_enabled(db),
        "countdown_shown": countdowns.SHOWN,
        "countdown_max_title": countdowns.MAX_TITLE,
        "countdown_break_horizon": countdowns.BREAK_HORIZON,
        "shopping_view": await shopping.get_view(db),
        "shopping_views": shopping.VIEWS,
        "shopping_order": [(key, shopping.CATEGORIES[key]) for key in await shopping.get_order(db)],
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
        # Fetch the new place's forecast now (it has its own short deadline
        # and outage handling), so Auto night follows its sunset straight
        # away even with the Weather widget hidden.
        await weather.refresh(db)
    return RedirectResponse(url=admin_url("weather"), status_code=303)


# --- Display ---


@router.post("/next-up", dependencies=[Depends(require_admin)])
async def save_next_up(enabled: bool = Form(False)):
    """The "next up" strip under the banners, on or off."""
    async with get_db() as db:
        await next_up.set_enabled(db, enabled)
    return RedirectResponse(url=admin_url("banners"), status_code=303)


@router.post("/countdowns", dependencies=[Depends(require_admin)])
async def add_countdown(title: str = Form(""), target_date: str = Form(""), profile_id: str = Form("")):
    async with get_db() as db:
        try:
            await countdowns.add(db, title, target_date, profile_id, await common.family_today(db))
        except countdowns.CountdownError as exc:
            return admin_error(str(exc))
    return RedirectResponse(url=admin_url("countdowns"), status_code=303)


@router.post("/countdowns/{countdown_id}/delete", dependencies=[Depends(require_admin)])
async def delete_countdown(countdown_id: int):
    """Deleting one that's already gone is fine: the list shows what's left."""
    async with get_db() as db:
        await countdowns.delete(db, countdown_id)
    return RedirectResponse(url=admin_url("countdowns"), status_code=303)


@router.post("/countdowns/school-breaks", dependencies=[Depends(require_admin)])
async def save_countdown_breaks(enabled: bool = Form(False)):
    async with get_db() as db:
        await countdowns.set_breaks_enabled(db, enabled)
    return RedirectResponse(url=admin_url("countdowns"), status_code=303)


@router.post("/shopping/view", dependencies=[Depends(require_admin)])
async def save_shopping_view(view: str = Form("")):
    """Spec 10.8: the Shopping widget as a simple list or grouped by aisle."""
    async with get_db() as db:
        try:
            await shopping.set_view(db, view)
        except ValueError:
            return admin_error("shopping-view")
    return RedirectResponse(url=admin_url("shopping"), status_code=303)


@router.post("/shopping/order", dependencies=[Depends(require_admin)])
async def move_shopping_category(category: str = Form(""), step: int = Form(0)):
    """One aisle up (-1) or down (+1) in the grouped view's order."""
    async with get_db() as db:
        await shopping.move_category(db, category, step)
    return RedirectResponse(url=admin_url("shopping"), status_code=303)


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
