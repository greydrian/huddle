"""Family members' avatars (spec 10.9): a coloured initial (the default), an
emoji, or a photo.

The photo is stored in the database (profiles.avatar_photo, migration 8), so
the nightly backup includes it: backups copy only the database and the key.
It's cropped square, resized to 256x256 and re-encoded as WebP from the
pixels alone, so no EXIF (camera, time, GPS) or other metadata survives.
Rendering is one macro, `person_face` in _icons.html; the kiosk loads the
photo from /avatars/{id}-{hash} (routers/avatars.py).
"""

import hashlib
import io

KINDS = ("initial", "emoji", "photo")
KIND_LABELS = {"initial": "Initial", "emoji": "Emoji", "photo": "Photo"}

PHOTO_SIZE = 256
# JPEGs are decoded at a reduced scale no smaller than this (Image.draft).
DECODE_SIZE = 1024
# The largest file Admin accepts (a phone photo is 2-6 MB; HEIC less). The
# upload guard caps the whole request a little above this.
MAX_PHOTO_BYTES = 8 * 1024 * 1024
# Pixel-bomb guard, checked from the header before anything is decoded (a
# phone photo is 12-50 MP). The same limit as the School inbox's.
MAX_PHOTO_PIXELS = 60_000_000
HASH_CHARS = 16

# A small grid of kid-friendly emoji for Admin (spec 10.9): animals, food,
# nature, sport and space. All single code points with no variation
# selector, so they look the same on every system font.
CURATED_EMOJI = (
    "🐶", "🐱", "🐭", "🐰", "🦊", "🐻", "🐼", "🐨",
    "🐯", "🦁", "🐮", "🐷", "🐸", "🐵", "🐧", "🐢",
    "🦄", "🐝", "🦋", "🐙", "🐬", "🦖", "🐳", "🦉",
    "🍓", "🍉", "🍕", "🍩", "🌈", "🌻", "🌙", "🌟",
    "🚀", "🚂", "⚽", "🏀", "🎨", "🎸", "🎈", "👑",
)


class AvatarError(ValueError):
    """An avatar that can't be saved; `code` is an ADMIN_ERRORS key."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# --- Emoji ---------------------------------------------------------------------------------

_ZWJ = 0x200D
_VS16 = 0xFE0F
_KEYCAP = 0x20E3
_SKIN_TONES = range(0x1F3FB, 0x1F400)
_REGIONAL = range(0x1F1E6, 0x1F200)
_TAGS = range(0xE0020, 0xE007F)
_TAG_END = 0xE007F
# Pictographic code points (approximately Unicode's Extended_Pictographic).
_PICTOGRAPHIC = (
    range(0x1F000, 0x1F1E6), range(0x1F200, 0x1F3FB), range(0x1F400, 0x1FB00),
    range(0x2600, 0x27C0), range(0x2300, 0x2400), range(0x2194, 0x21AB), range(0x25AA, 0x2600),
)
_PICTOGRAPHIC_SINGLES = {
    0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x24C2, 0x2934, 0x2935,
    0x2B05, 0x2B06, 0x2B07, 0x2B1B, 0x2B1C, 0x2B50, 0x2B55, 0x3030, 0x303D, 0x3297, 0x3299,
}
_KEYCAP_BASES = set("0123456789#*")
MAX_EMOJI_CODEPOINTS = 16  # the longest real sequences (families, flags of UK nations) are ~10


def _is_pictographic(cp: int) -> bool:
    return cp in _PICTOGRAPHIC_SINGLES or any(cp in r for r in _PICTOGRAPHIC)


def _element(cps: list[int], i: int) -> int | None:
    """One emoji element from index i: a pictograph with an optional VS16,
    skin tone, or tag sequence (e.g. the flag of Scotland). Returns the index
    after it, or None."""
    if i >= len(cps) or not _is_pictographic(cps[i]):
        return None
    i += 1
    if i < len(cps) and cps[i] == _VS16:
        i += 1
    if i < len(cps) and cps[i] in _SKIN_TONES:
        i += 1
    if i < len(cps) and cps[i] in _TAGS:
        while i < len(cps) and cps[i] in _TAGS:
            i += 1
        if i >= len(cps) or cps[i] != _TAG_END:
            return None
        i += 1
    return i


def is_single_emoji(text: str) -> bool:
    """Whether `text` is exactly one emoji: a pictograph (with its optional
    variation selector, skin tone or tags), a ZWJ sequence of them (👩‍🚀), a
    flag (two regional indicators) or a keycap (#️⃣). Plain letters, digits,
    spaces or two emoji side by side are refused."""
    if not text or len(text) > MAX_EMOJI_CODEPOINTS:
        return False
    cps = [ord(c) for c in text]
    if len(cps) == 2 and all(cp in _REGIONAL for cp in cps):
        return True
    if text[0] in _KEYCAP_BASES:
        rest = cps[1:]
        return rest in ([_KEYCAP], [_VS16, _KEYCAP])
    i = _element(cps, 0)
    while i is not None and i < len(cps) and cps[i] == _ZWJ:
        i = _element(cps, i + 1)
    return i == len(cps)


def clean_emoji(text: str) -> str:
    emoji = (text or "").strip()
    if not is_single_emoji(emoji):
        raise AvatarError("avatar-emoji")
    return emoji


# --- Photos --------------------------------------------------------------------------------

def process_photo(data: bytes) -> bytes:
    """An uploaded photo -> a 256x256 WebP, centre-cropped, turned upright
    (EXIF orientation), and re-encoded from the pixels only: no EXIF, ICC
    profile or other metadata is kept. HEIC (iPhone) is supported. The pixel
    count is checked from the header before anything is decoded."""
    from PIL import Image, ImageOps

    from app.services import imports  # not at the top: imports -> homework -> avatars

    if len(data) > MAX_PHOTO_BYTES:
        raise AvatarError("avatar-too-big")
    mime_type = imports._sniff(data)
    if mime_type is None or not mime_type.startswith("image/"):
        raise AvatarError("avatar-bad-type")
    # Pillow's own limit (it errors at twice this) backs up the check below.
    Image.MAX_IMAGE_PIXELS = MAX_PHOTO_PIXELS
    if mime_type == "image/heic":
        import pillow_heif

        pillow_heif.register_heif_opener()
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size  # from the header: nothing decoded yet
            if width * height > MAX_PHOTO_PIXELS:
                raise AvatarError("avatar-too-big")
            image.seek(0)  # an animated GIF/WebP: its first frame
            # A JPEG decodes straight to a smaller scale (1/2 to 1/8, never
            # under 1024 px): a 60 MP photo then needs tens of MB, not ~1 GB.
            if image.format == "JPEG":
                image.draft("RGB", (DECODE_SIZE, DECODE_SIZE))
            upright = ImageOps.exif_transpose(image)
            if upright.mode == "I" or upright.mode.startswith("I;16"):
                # 16-bit greyscale: scale 0-65535 down to 0-255 first, or
                # convert() clips everything above 255 to white.
                upright = upright.convert("I").point(lambda v: v * (1 / 256)).convert("L")
            mode = "RGBA" if upright.mode in ("RGBA", "LA", "PA") or "transparency" in upright.info else "RGB"
            square = ImageOps.fit(upright.convert(mode), (PHOTO_SIZE, PHOTO_SIZE), Image.Resampling.LANCZOS)
    except AvatarError:
        raise
    except Image.DecompressionBombError:
        raise AvatarError("avatar-too-big") from None
    except (OSError, ValueError, SyntaxError, EOFError):
        raise AvatarError("avatar-bad-type") from None
    # A brand-new image from the raw pixels: nothing from the original's
    # .info (EXIF, XMP, ICC, comments) can be carried into the output.
    clean = Image.frombytes(mode, square.size, square.tobytes())
    out = io.BytesIO()
    clean.save(out, format="WEBP", quality=85, method=4)
    return out.getvalue()


def photo_hash(photo: bytes) -> str:
    return hashlib.sha256(photo).hexdigest()[:HASH_CHARS]


# --- For templates -------------------------------------------------------------------------

def avatar_of(profile: dict) -> dict:
    """What the person_face macro needs, from a profile dict with its id and
    avatar_* columns: {"kind", "emoji", "url"}. Falls back to the initial
    when the emoji or photo is missing."""
    kind = profile.get("avatar_kind") or "initial"
    emoji = profile.get("avatar_emoji")
    stored_hash = profile.get("avatar_hash")
    if kind == "emoji" and emoji:
        return {"kind": "emoji", "emoji": emoji, "url": None}
    if kind == "photo" and stored_hash:
        return {"kind": "photo", "emoji": None, "url": f"/avatars/{profile.get('id')}-{stored_hash}"}
    return {"kind": "initial", "emoji": None, "url": None}


def attach(profile: dict, id_key: str = "id") -> dict:
    """Adds profile["avatar"] and drops the photo's bytes (templates never
    need them). Returns the same dict."""
    profile.pop("avatar_photo", None)
    profile["avatar"] = avatar_of({**profile, "id": profile.get(id_key)})
    return profile


# The avatar columns to add to a SELECT on profiles (never the photo bytes).
COLUMNS = "avatar_kind, avatar_emoji, avatar_hash"
# Every profiles column a page needs, instead of SELECT *: the photo's bytes
# (and the unused avatar_path) stay out of widget loads and /api/rev polls.
PROFILE_COLUMNS = f"id, name, colour_hex, sort_order, google_tasklist_id, school_year, is_parent, email, {COLUMNS}"


def columns(alias: str) -> str:
    return ", ".join(f"{alias}.{c.strip()}" for c in COLUMNS.split(","))
