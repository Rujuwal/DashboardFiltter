import logging
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
    """Warm Mongo connections and the heaviest cached pages in the background."""
    global _warmup_started

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
