"""System tab: backups. (Change PIN is on this tab too; its routes live
with the rest of the PIN handling in login.py.)"""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse

from app import backup
from app.admin_tabs import admin_url
from app.auth import require_admin
from app.routers.admin import common
from app.routers.admin.common import admin_error, tab_context

router = APIRouter(prefix="/admin")


@tab_context("system")
async def system_context(db, base: dict, extra: dict) -> dict:
    return {"backup_status": backup.status(await common.family_timezone(db))}


# --- Backups ---


@router.post("/backups/run", dependencies=[Depends(require_admin)])
async def run_backup_now():
    if await backup.create_backup() is None:
        return admin_error("backup-failed")
    return RedirectResponse(url=admin_url("backups"), status_code=303)
