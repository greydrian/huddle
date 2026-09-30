"""Assistant tab (docs/assistant-spec.md): today only which Claude model
the school inbox reads with. No routes yet."""

from fastapi import APIRouter

from app.routers.admin.common import tab_context
from app.services import extraction

router = APIRouter(prefix="/admin")


@tab_context("assistant")
async def assistant_context(db, base: dict, extra: dict) -> dict:
    return {"assistant_model": extraction.model_name()}
