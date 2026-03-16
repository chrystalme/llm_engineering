"""APScheduler: 30 min interval, respects USE_MODAL=1.

Imports _run_pipeline from app.py so Modal vs local behaviour is determined
by the USE_MODAL env var at runtime — no separate code path needed.
"""
import os
import logging
from datetime import datetime, timezone, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from config import MARKET_OPEN_HOUR_ET, MARKET_CLOSE_HOUR_ET

logger = logging.getLogger("aria.scheduler")


def _is_market_hours() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        now = datetime.now(timezone.utc) - timedelta(hours=5)
    if now.weekday() >= 5:
        return False
    return MARKET_OPEN_HOUR_ET <= now.hour < MARKET_CLOSE_HOUR_ET


def _job():
    """Run the pipeline — via Modal if USE_MODAL=1, otherwise locally."""
    try:
        from app import _run_pipeline
        records, asset_data = _run_pipeline()
        n_alerts = sum(1 for r in records if r.decision == "ALERT")
        logger.info(
            "Scheduled run complete: %d assets, %d alerts.",
            len(asset_data), n_alerts,
        )
    except Exception as e:
        logger.exception("Scheduled pipeline run failed: %s", e)


def start_scheduler():
    scheduler = BackgroundScheduler()
    scheduler.add_job(_job, IntervalTrigger(minutes=30), id="aria_pipeline")
    scheduler.start()
    use_modal = bool(os.getenv("USE_MODAL"))
    logger.info(
        "ARIA scheduler started (30 min interval, Modal=%s). "
        "Decision agent gates non-24/7 assets by market hours.",
        use_modal,
    )
    return scheduler