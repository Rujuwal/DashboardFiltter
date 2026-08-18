import logging
import os
import threading

from db import get_db
from services.reference_data import (
    get_active_expert_emails,
    get_active_task_experts,
    get_export_filter_options,
    get_kpi_round_titles,
    get_teams_reference,
)

LOGGER = logging.getLogger(__name__)

_warmup_lock = threading.Lock()
_warmup_started = False


def start_startup_warmup(app):
    """
    Warm Mongo connections and the heaviest cached pages in the background.

    Skipped on Vercel (same guard start_po_consumer already uses): each cold
    start there gets a brand-new process with an empty in-memory cache, so
    this thread would just replay ~4 of the app's heaviest pages -- Expert
    Analytics, Interview Stats, Interview Records, KPI Sidebar -- in the
    background of every cold start, fighting the real incoming request for
    the same limited CPU and making that request slower, not faster. Warming
    only pays off with a long-lived process where the cache actually survives
    between requests, which is the local-dev case this stays enabled for.
    """
    global _warmup_started

    if os.getenv("VERCEL"):
        LOGGER.info("Skipping startup warmup in the Vercel runtime.")
        return

    with _warmup_lock:
        if _warmup_started:
            return
        _warmup_started = True

    def runner():
        try:
            with app.app_context():
                get_db().command("ping")
                get_teams_reference()
                get_active_expert_emails()
                get_active_task_experts(completed_only=False)
                get_export_filter_options()
                get_kpi_round_titles()

            with app.test_client() as client:
                for path in (
                    "/",
                    "/analytics/experts",
                    "/analytics/interview-stats",
                    "/analytics/interview-records",
                    "/candidates/active",
                    "/kpi/sidebar",
                ):
                    response = client.get(path)
                    if response.status_code >= 400:
                        LOGGER.warning("Startup warmup request failed for %s with %s", path, response.status_code)
        except Exception:
            LOGGER.exception("Startup warmup failed")

    thread = threading.Thread(target=runner, name="startup-warmup", daemon=True)
    thread.start()
