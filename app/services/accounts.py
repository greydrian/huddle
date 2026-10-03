"""
What changes when a Google account stops doing a job, is removed or comes
back (spec 12.2, 12.6). The table, jobs and states are app/google_accounts.py.

- Unticking Tasks & shopping, or removing the account, leaves its lists
  linked and every item's Google id in place, dormant: sync skips them and
  their changes (one-offs, ticks, edits, deletions) wait in the queue
  (task_sync). Ticking the job again, or adding the same account back (its
  row is kept, marked removed, and revived by `sub`), pushes what waited and
  then reconciles, so nothing is duplicated or resurrected.
- Unticking School email keeps its checkpoint; ticking it again reads from
  the later of that and BACKFILL_DAYS ago, so an old checkpoint never sends
  months of backlog to Claude. Removing the account deletes its checkpoint
  and status. Approved and pending School inbox items are always kept.
- Removing an account also takes its calendars off the wall (and its cached
  events), clears the Family or school events calendar if it was in it
  (Admin warns until another is picked), and revokes and deletes its token.
  Other accounts' calendars and caches are untouched, and the family's
  timezone is kept.

Unticking Calendars or Writing events needs nothing here: the wall reads
only accounts doing Calendars, and the Family calendar counts only while its
account does Writing events.
"""

import logging
from datetime import UTC, datetime

from app import calendar_cache, google_accounts, google_oauth, school_email, sync_status, task_sync
from app.services import calendar_prefs, school_events

logger = logging.getLogger(__name__)


async def _forget_school_email(db) -> None:
    await db.execute(
        "DELETE FROM app_settings WHERE key IN (?, ?)", (school_email.CHECKPOINT_SETTING, school_email.STATUS_SETTING)
    )
    await db.commit()


async def apply_job_changes(db, account: dict, new_jobs: list[str], now: datetime | None = None) -> list[str]:
    """When an Edit changes `account`'s jobs: what that changes. Returns the
    jobs dropped (Admin says the permission stays until the account is removed)."""
    dropped = [job for job in account["jobs"] if job not in new_jobs]
    if "tasks" in dropped or ("tasks" in new_jobs and "tasks" not in account["jobs"]):
        await sync_status.reset(db)  # another account's (or no) sync history from here
    if "school_email" in new_jobs and "school_email" not in account["jobs"]:
        await school_email.clamp_checkpoint(db, now or datetime.now(UTC))
    return dropped


async def removal_preview(db, account: dict) -> dict:
    """Exactly what Remove will change, for its confirm step."""
    profiles, shopping = await task_sync.lists_in(db, account)
    saved = await google_oauth.get_saved_calendars(db) or []
    family = await calendar_prefs.get_family_setting(db)
    target = await school_events.get_target_calendar(db)
    return {
        "calendars": [cal.get("summary") or cal["id"] for cal in saved if cal["account_id"] == account["id"]],
        "people": [p["name"] for p in profiles],
        "shopping": shopping is not None,
        "family_calendar": family["summary"] if family and family["account_id"] == account["id"] else None,
        "school_calendar": target["summary"] if target and target["account_id"] == account["id"] else None,
        "school_email": "school_email" in account["jobs"],
    }


async def remove(db, account_id: int) -> bool:
    """Removes an account (spec 12.6). False if it no longer exists."""
    account = await google_accounts.get(db, account_id)
    if account is None:
        return False
    preview = await removal_preview(db, account)
    await google_oauth.revoke(await google_accounts.load_tokens(db, account_id))

    if "school_email" in account["jobs"]:
        await _forget_school_email(db)
    if "tasks" in account["jobs"]:
        await sync_status.reset(db)

    saved = await google_oauth.get_saved_calendars(db)
    if saved is not None:
        removed_ids = [cal["id"] for cal in saved if cal["account_id"] == account_id]
        if removed_ids:
            await google_oauth.set_selected_calendars(db, [c for c in saved if c["account_id"] != account_id])
            await google_accounts.add_removed_notice(db, calendars=removed_ids)
    if preview["family_calendar"]:
        await calendar_prefs.set_family_calendar(db, None)
    if preview["school_calendar"]:
        await school_events.set_target_calendar(db, None)
    await google_accounts.add_removed_notice(
        db, family=preview["family_calendar"], school_events=preview["school_calendar"]
    )
    await calendar_cache.clear_account(db, account_id)
    await google_accounts.soft_remove(db, account_id)
    await db.commit()
    logger.info("Google account %d removed", account_id)
    return True
