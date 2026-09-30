"""
/avatars/{profile_id}-{hash}: a family member's avatar photo (spec 10.9).

PIN-free, because the kiosk shows it. The URL carries the photo's hash, so a
new photo gets a new URL and each one can be cached for good; a stale or
made-up hash is a 404, never another photo. Only digits and hex are accepted.
"""

import re

from fastapi import APIRouter, HTTPException
from fastapi import Path as PathParam
from fastapi.responses import Response

from app import avatars
from app.database import get_db

router = APIRouter()

_KEY = re.compile(rf"([1-9][0-9]{{0,9}})-([0-9a-f]{{{avatars.HASH_CHARS}}})")


def _media_type(photo: bytes) -> str:
    return "image/png" if photo.startswith(b"\x89PNG") else "image/webp"


@router.get("/avatars/{key}")
async def avatar_photo(key: str = PathParam(max_length=32)):
    match = _KEY.fullmatch(key)
    if not match:
        raise HTTPException(status_code=404)
    profile_id, wanted = int(match.group(1)), match.group(2)
    async with get_db() as db:
        row = await (
            await db.execute("SELECT avatar_photo, avatar_hash FROM profiles WHERE id = ?", (profile_id,))
        ).fetchone()
    if row is None or not row["avatar_photo"] or row["avatar_hash"] != wanted:
        raise HTTPException(status_code=404)
    photo = bytes(row["avatar_photo"])
    return Response(
        photo,
        media_type=_media_type(photo),
        headers={"Cache-Control": "public, max-age=31536000, immutable", "X-Content-Type-Options": "nosniff"},
    )
