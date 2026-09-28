"""
Background jobs, started/stopped from app/main.py's lifespan:
- Google Tasks sync every 60s (spec 9.4: polling, not push)
- the end-of-day task reset, checked every 5 minutes (it only acts once per
  family-local day — see services.tasks.run_daily_reset_if_due)
- the nightly database backup (~03:30 family time, or at startup when the
  newest backup is over 24h old), checked every 10 minutes — see app/backup.py
"""

from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app import backup, task_sync
from app.database import get_db
from app.services import tasks

SYNC_INTERVAL_SECONDS = 60
DAILY_RESET_CHECK_SECONDS = 300
BACKUP_CHECK_SECONDS = 600

scheduler = AsyncIOScheduler()


async def _run_sync_job():
    async with get_db() as db:
        await task_sync.run_sync(db)


async def _daily_reset_job():
    async with get_db() as db:
        await tasks.run_daily_reset_if_due(db)


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
    scheduler.start()


def stop():
    scheduler.shutdown(wait=False)
