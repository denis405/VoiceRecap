import asyncio
import logging
import time
from datetime import datetime, timedelta

from bot.db import Database

log = logging.getLogger(__name__)

DAY = 86400


def seconds_until(hour: int) -> float:
    """Until the next hour:00 server time (UTC in the Docker container)."""
    now = datetime.now()
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def purge_cache_nightly(db: Database, ttl_days: int, hour: int = 3) -> None:
    """Delete recap_cache entries older than ttl_days, once a night."""
    while True:
        await asyncio.sleep(seconds_until(hour))
        try:
            deleted = await db.purge_recap_cache(int(time.time()) - ttl_days * DAY)
            log.info("Recap cache cleanup: %d entries older than %d days deleted", deleted, ttl_days)
        except Exception:
            log.exception("Recap cache cleanup failed")
