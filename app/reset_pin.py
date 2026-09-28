"""
Forgotten Admin PIN recovery. Run on the host that owns the data:

    docker compose exec family-display python -m app.reset_pin

Puts the PIN back to the default (1234) with the forced "Choose a new PIN"
screen switched on, clears any lockout and signs out every Admin session.
Safe while the app is running: none of this is cached in-process.
"""

import asyncio

from app import database
from app.auth import PIN_IS_DEFAULT_SETTING, bump_session_generation
from app.security import DEFAULT_PIN, hash_pin


async def reset_pin() -> None:
    await database.init_db()  # creates the tables on an empty DATA_DIR
    async with database.get_db() as db:
        await database.set_setting(db, "pin_hash", hash_pin(DEFAULT_PIN))
        await database.set_setting(db, PIN_IS_DEFAULT_SETTING, "1")
        await database.set_setting(db, "pin_lockout", "{}")
        await bump_session_generation(db)
        await db.commit()


def main() -> None:
    asyncio.run(reset_pin())
    print(f"Admin PIN reset to {DEFAULT_PIN}. Log in to Admin to choose a new one.")


if __name__ == "__main__":
    main()
