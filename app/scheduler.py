"""
Background jobs, started/stopped from app/main.py's lifespan:
- Google Tasks sync every 60s (spec 9.4: polling, not push)
- the end-of-day task reset, checked every 5 minutes (it only acts once per
  family-local day — see services.tasks.run_daily_reset_if_due)
- the nightly database backup (~03:30 family time; catches up at startup
  if there's none since the last 03:30), checked every 10 minutes — see app/backup.py
- the calendar outage cache refresh every 5 minutes (app/calendar_cache.py)
- the school email check (daily at 18:00 family time by default, set in
  Admin; catches up at startup), checked every 10 minutes — see app/school_email.py
- the GOV.UK bank holidays, refreshed weekly (checked every 6 hours, and
  shortly after startup so missing or stale data is fetched) — see app/bank_holidays.py
"""

from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import backup, bank_holidays, google_calendar, school_email, task_sync
from app.database import get_db
from app.services import tasks

SYNC_INTERVAL_SECONDS = 60
DAILY_RESET_CHECK_SECONDS = 300
BACKUP_CHECK_SECONDS = 600
CALENDAR_CACHE_SECONDS = 300

scheduler = AsyncIOScheduler()


async def _run_sync_job():
    async with get_db() as db:
        await task_sync.run_sync(db)


async def _daily_reset_job():
    async with get_db() as db:
        await tasks.run_daily_reset_if_due(db)


async def _calendar_cache_job():
    # Google errors are caught and logged once per outage inside google_calendar.
    async with get_db() as db:
        await google_calendar.refresh_cache(db)


def start():
    # Single-process only: every uvicorn worker would start its own scheduler
    # and push the same sync_queue rows (duplicate Google tasks).
    scheduler.add_job(
        _run_sync_job, "interval", seconds=SYNC_INTERVAL_SECONDS,
        id="google_tasks_sync", replace_existing=True, max_instances=1,
    )
    scheduler.add_job(
        _daily_reset_job, "interval", seconds=DAILY_RESET_CHECK_SECONDS,
        id="daily_task_reset", replace_existing=True, max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # catch up straight away after downtime
    )
    scheduler.add_job(
        backup.run_backup_if_due, "interval", seconds=BACKUP_CHECK_SECONDS,
        id="nightly_backup", replace_existing=True, max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # startup catch-up if stale
    )
    scheduler.add_job(
        school_email.run_if_due, "interval", seconds=school_email.CHECK_SECONDS,
        id="school_email", replace_existing=True, max_instances=1,
        next_run_time=datetime.now(timezone.utc),  # startup catch-up if a check was missed
    )
    scheduler.add_job(
        bank_holidays.run_if_due, "interval", seconds=bank_holidays.CHECK_SECONDS,
        id="bank_holidays", replace_existing=True, max_instances=1,
        next_run_time=datetime.now(timezone.utc) + bank_holidays.STARTUP_DELAY,  # fetch if missing or stale
    )
    scheduler.add_job(
        _calendar_cache_job, "interval", seconds=CALENDAR_CACHE_SECONDS,
        id="calendar_cache", replace_existing=True, max_instances=1,
    )
    scheduler.start()


def stop():
    scheduler.shutdown(wait=False)
