"""
Background scheduler — currently just the Google Tasks sync cycle (spec
9.4: polling, not push). Started/stopped from app/main.py's lifespan.
"""

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.database import get_db
from app import task_sync

SYNC_INTERVAL_SECONDS = 60

scheduler = AsyncIOScheduler()


async def _run_sync_job():
    async with get_db() as db:
        await task_sync.run_sync(db)


def start():
    scheduler.add_job(_run_sync_job, "interval", seconds=SYNC_INTERVAL_SECONDS, id="google_tasks_sync")
    scheduler.start()


def stop():
    scheduler.shutdown(wait=False)
