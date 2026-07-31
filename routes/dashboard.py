from flask import Blueprint, redirect, url_for

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/")
def index():
    """Site root -- redirect to Expert Analytics, the new default landing page."""
    return redirect(url_for("analytics.expert_analytics"))
