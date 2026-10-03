"""The Admin page itself: GET /admin renders one tab (app/admin_tabs.py)
with the context every tab shares plus that tab's own loader (common.TAB_CONTEXT).
Route modules call render_admin() to re-show a form with a validation error."""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from app import admin_tabs, appearance, avatars, google_accounts, google_oauth
from app.auth import require_admin
from app.database import get_db
from app.routers.admin import common
from app.services import extraction
from app.templating import templates

router = APIRouter(prefix="/admin")


@router.get("", response_class=HTMLResponse, dependencies=[Depends(require_admin)])
async def admin_home(
    request: Request,
    tab: str | None = None,
    weather_error: str | None = None,
    error: str | None = None,
    remove: str | None = None,
):
    """`remove` (an account id) shows that Google account's Remove confirm step."""
    google_remove = int(remove) if remove and remove.isdigit() else None
    return await render_admin(request, tab=tab, weather_error=weather_error, error=error, google_remove=google_remove)


def _tab_from_query(request: Request, form_error: dict | None, weather_error: str | None) -> str:
    if form_error:
        return admin_tabs.SECTIONS[form_error["section"]]
    for param, section in (("sync", "sync"), ("school_email", "school-email")):
        if request.query_params.get(param):
            return admin_tabs.SECTIONS[section]
    if weather_error:
        return admin_tabs.SECTIONS["weather"]
    return admin_tabs.DEFAULT_TAB


async def render_admin(
    request: Request,
    tab: str | None = None,
    weather_error: str | None = None,
    error: str | None = None,
    homework_error: str | None = None,
    words_error: str | None = None,
    homework_form: dict | None = None,
    words_form: dict | None = None,
    inbox_form: dict | None = None,
    inbox_error: str | None = None,
    term_form: dict | None = None,
    error_message: str | None = None,
    google_remove: int | None = None,
    status_code: int = 200,
):
    """Renders one Admin tab (app/admin_tabs.py), loading only what that
    tab shows. After a validation error, *_form carries what was submitted
    (with "id" None for the add form, else the row being edited) so nothing
    typed is lost."""
    form_error = common.form_error(error)
    if form_error and error_message:
        form_error["message"] = error_message  # e.g. naming the period a new one clashes with
    if tab not in admin_tabs.TABS:
        # No (valid) tab: an older link such as /admin?error=pin-invalid#pin
        # still opens the tab its message or status belongs to.
        tab = _tab_from_query(request, form_error, weather_error)
    if form_error and admin_tabs.SECTIONS[form_error["section"]] != tab:
        form_error = None  # it belongs to another tab's section; never shown out of place
    extra = {
        "weather_error": weather_error,
        "homework_form": homework_form,
        "homework_error": homework_error,
        "words_form": words_form,
        "words_error": words_error,
        "inbox_form": inbox_form,
        "inbox_error": inbox_error,
        "term_form": term_form,
        "google_remove": google_remove,
    }
    context: dict = {
        "admin_tab": tab,
        "admin_tabs": admin_tabs.TABS,
        "admin_sections": admin_tabs.SECTIONS,
        "form_error": form_error,
    }
    async with get_db() as db:
        context["appearance"] = await appearance.current_mode(db)
        context["profiles"] = [
            avatars.attach(dict(r))
            for r in await (
                await db.execute(f"SELECT {avatars.PROFILE_COLUMNS} FROM profiles ORDER BY sort_order")
            ).fetchall()
        ]
        context["curated_emoji"] = avatars.CURATED_EMOJI
        context["avatar_kinds"] = avatars.KIND_LABELS
        context["google_accounts"] = await google_accounts.list_accounts(db)
        context["google_configured"] = google_oauth.is_configured()
        context["inbox_configured"] = extraction.is_configured()
        context.update(await common.TAB_CONTEXT[tab](db, context, extra))

    return templates.TemplateResponse(request, "admin/settings.html", context, status_code=status_code)
