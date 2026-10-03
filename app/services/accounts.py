"""
What changes when a Google account stops doing a job or is removed (spec
12.2, 12.6). The table, jobs and states are app/google_accounts.py.

- Unticking Tasks & shopping, or removing the account, unlinks its lists
  through the relink path: local tasks and shopping items are kept (now on
  this display only) and nothing is deleted locally or on Google.
- Unticking School email, or removing the account, deletes its school email
  checkpoint and status (stage 1: there's one, for the one account doing it);
  approved and pending School inbox items are kept.
- Removing an account also takes its calendars off the wall (and their
  cached events and person links), clears the Family or school events
  calendar if it was in it (Admin warns until another is picked), revokes
  its token with Google and deletes the row.

Unticking Calendars or Writing events needs nothing here: the wall reads
only accounts doing Calendars, and the Family calendar counts only while its
account does Writing events.
"""

import logging

from app import calendar_cache, google_accounts, google_oauth, school_email, sync_status, task_sync
from app.services import calendar_prefs, school_events

logger = logging.getLogger(__name__)


async def _linked_lists(db, account: dict) -> tuple[list[dict], dict | None]:
    """(profiles whose task list is in this account, the shopping list if it
    is). Lists linked before accounts belong to the account doing Tasks."""
    does_tasks = "tasks" in account["jobs"]
    profiles = await (
        await db.execute(
            """SELECT id, name FROM profiles WHERE google_tasklist_id IS NOT NULL
                 AND (google_account_id = ? OR (google_account_id IS NULL AND ?))
               ORDER BY sort_order""",
            (account["id"], int(does_tasks)),
        )
    ).fetchall()
    shopping = await task_sync.get_shopping_tasklist(db)
    if shopping and shopping.get("account", account["id"] if does_tasks else None) != account["id"]:
        shopping = None
    return [dict(p) for p in profiles], shopping


async def _unlink_lists(db, account: dict) -> None:
    profiles, shopping = await _linked_lists(db, account)
    for profile in profiles:
        await db.execute(
            "UPDATE profiles SET google_tasklist_id = NULL, google_account_id = NULL WHERE id = ?", (profile["id"],)
        )
        await task_sync.relink_profile(db, profile["id"])
    if shopping:
        await task_sync.clear_shopping_tasklist(db)
        await task_sync.relink_shopping(db)


async def _forget_school_email(db) -> None:
    await db.execute(
        "DELETE FROM app_settings WHERE key IN (?, ?)", (school_email.CHECKPOINT_SETTING, school_email.STATUS_SETTING)
    )
    await db.commit()


async def apply_job_changes(db, account: dict, new_jobs: list[str]) -> list[str]:
    """Before `account` drops jobs: what that changes. Returns the jobs
    dropped (Admin says the permission stays until the account is removed)."""
    dropped = [job for job in account["jobs"] if job not in new_jobs]
    if "tasks" in dropped:
        await _unlink_lists(db, account)
        await sync_status.reset(db)
    if "school_email" in dropped:
        await _forget_school_email(db)
    return dropped


async def removal_preview(db, account: dict) -> dict:
    """Exactly what Remove will change, for its confirm step."""
    profiles, shopping = await _linked_lists(db, account)
    saved = await google_oauth.get_saved_calendars(db) or []
    family = await calendar_prefs.get_family_setting(db)
    target = await school_events.get_target_calendar(db)
    return {
        "calendars": [cal.get("summary") or cal["id"] for cal in saved if cal["account"] == account["id"]],
        "people": [p["name"] for p in profiles],
        "shopping": shopping is not None,
        "family_calendar": family["summary"] if family and family["account"] == account["id"] else None,
        "school_calendar": target["summary"] if target and target["account"] == account["id"] else None,
        "school_email": "school_email" in account["jobs"],
    }


async def remove(db, account_id: int) -> bool:
    """Removes an account (spec 12.6). False if it no longer exists."""
    account = await google_accounts.get(db, account_id)
    if account is None:
        return False
    preview = await removal_preview(db, account)
    await google_oauth.revoke(await google_accounts.load_tokens(db, account_id))

    await _unlink_lists(db, account)
    if "school_email" in account["jobs"]:
        await _forget_school_email(db)
    if "tasks" in account["jobs"]:
        await sync_status.reset(db)

    saved = await google_oauth.get_saved_calendars(db)
    if saved is not None:
        removed_ids = [cal["id"] for cal in saved if cal["account"] == account_id]
        if removed_ids:
            await google_oauth.set_selected_calendars(db, [c for c in saved if c["account"] != account_id])
            await google_accounts.add_removed_notice(db, calendars=removed_ids)
    links = await calendar_prefs.get_saved_people_links(db)
    await calendar_prefs.set_people_links(
        db, {key: owner for key, owner in links.items() if not key.startswith(f"{account_id}:")}
    )
    if preview["family_calendar"]:
        await calendar_prefs.set_family_calendar(db, None)
    if preview["school_calendar"]:
        await school_events.set_target_calendar(db, None)
    await google_accounts.add_removed_notice(
        db, family=preview["family_calendar"], school_events=preview["school_calendar"]
    )
    await calendar_cache.clear_account(db, account_id)
    await google_accounts.delete(db, account_id)
    await db.commit()
    logger.info("Google account %d removed", account_id)
    return True
