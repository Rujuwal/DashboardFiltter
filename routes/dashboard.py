from collections import Counter
from datetime import datetime

from flask import Blueprint, current_app, render_template, request, url_for

from db import get_db, get_teams_db
from routes.analytics import (
    build_interview_stats_match,
    get_expert_funnel_data,
    get_team_funnel_data,
    normalize_interview_status_bucket,
)
from routes.candidates import fetch_expert_activity_data
from routes.kpi import calculate_kpi_data
from services.interview_metrics import build_interview_date_match

dashboard_bp = Blueprint("dashboard", __name__)
DASHBOARD_CACHE_VERSION = "v3"


def display_name(value):
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    return text.split("@", 1)[0] if "@" in text else text


def get_dashboard_dates():
    start_date = request.args.get("start_date", "").strip()
    end_date = request.args.get("end_date", "").strip()

    if not start_date and not end_date:
        today = datetime.now()
        start_date = today.replace(day=1).strftime("%Y-%m-%d")
        end_date = today.strftime("%Y-%m-%d")

    return start_date, end_date


def build_received_date_filter(start_date="", end_date=""):
    # Filter on the real interview date (Date of Interview), not receivedDateTime.
    return build_interview_date_match(start_date, end_date)


def format_period_label(start_date="", end_date=""):
    if not start_date and not end_date:
        return "All Time"

    if start_date and end_date:
        try:
            start_dt = datetime.strptime(start_date[:10], "%Y-%m-%d")
            end_dt = datetime.strptime(end_date[:10], "%Y-%m-%d")
            if start_dt.year == end_dt.year and start_dt.month == end_dt.month:
                return start_dt.strftime("%B %Y")
        except ValueError:
            pass
        return f"{start_date[:10]} to {end_date[:10]}"

    return (start_date or end_date)[:10]


def safe_month_snapshot(date_text):
    try:
        return datetime.strptime(date_text[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return datetime.now()


@dashboard_bp.route("/")
def index():
    """Dashboard landing page that summarizes the remaining analytics pages."""
    cache = current_app.cache
    start_date, end_date = get_dashboard_dates()

    @cache.memoize(timeout=300)
    def get_dashboard_data(start_date, end_date, cache_version):
        db = get_db()
        teams_db = get_teams_db()

        period_label = format_period_label(start_date, end_date)
        date_filter = build_received_date_filter(start_date, end_date)
        month_snapshot = safe_month_snapshot(end_date or start_date)
        activity_month = month_snapshot.strftime("%b").upper()
        activity_year = month_snapshot.strftime("%Y")
        activity_period_label = month_snapshot.strftime("%B %Y")

        expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, None, None)
        team_stats, _ = get_team_funnel_data(db, start_date, end_date, None, None)
        kpi_data = calculate_kpi_data(db, start_date, end_date, None, None, None, [])
        activity_team_data, activity_summary = fetch_expert_activity_data(
            activity_month,
            activity_year,
            "",
            None,
            None,
            False,
            None,
        )

        ranked_experts = sorted(
            expert_stats,
            key=lambda item: (
                item.get("interview_count", 0),
                item.get("screening_to_1st", 0),
            ),
            reverse=True,
        )
        ranked_teams = sorted(
            team_stats,
            key=lambda item: (
                item.get("interview_count", 0),
                item.get("screening_to_1st", 0),
            ),
            reverse=True,
        )
        ranked_kpi = sorted(
            kpi_data.get("experts", []),
            key=lambda item: (
                item.get("match_rate", 0),
                item.get("total_interviews", 0),
            ),
            reverse=True,
        )
        ranked_activity_teams = sorted(
            activity_team_data,
            key=lambda item: (item.get("active", 0), item.get("total", 0)),
            reverse=True,
        )

        interview_match = build_interview_stats_match(start_date, end_date)

        interview_status_rows = list(
            db.taskBody.aggregate(
                [
                    {"$match": interview_match},
                    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                ]
            )
        )
        interview_status_totals = {
            "Completed": 0,
            "Cancelled": 0,
            "Rescheduled": 0,
            "Not Done": 0,
        }
        other_status_total = 0
        for row in interview_status_rows:
            status_name = normalize_interview_status_bucket(row.get("_id"))
            if status_name in interview_status_totals:
                interview_status_totals[status_name] += row["count"]
            else:
                other_status_total += row["count"]
        if other_status_total:
            interview_status_totals["Other"] = other_status_total

        total_interviews = sum(interview_status_totals.values())
        completed_interviews = interview_status_totals.get("Completed", 0)
        interview_completion_rate = (
            round((completed_interviews / total_interviews) * 100, 1)
            if total_interviews
            else 0
        )

        record_round_rows = list(
            db.taskBody.aggregate(
                [
                    {
                        "$match": {
                            **interview_match,
                            "status": "Completed",
                            "actualRound": {
                                "$nin": [
                                    "Screening",
                                    "On demand",
                                    "On Demand or AI Interview",
                                    None,
                                    "",
                                ]
                            },
                        }
                    },
                    {"$group": {"_id": "$actualRound", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 8},
                ]
            )
        )
        record_round_chart = [
            {"name": row.get("_id") or "Unknown", "count": row["count"]}
            for row in record_round_rows
        ]

        candidate_focus_rows = list(
            db.taskBody.aggregate(
                [
                    {
                        "$match": {
                            **date_filter,
                            "Candidate Name": {"$type": "string", "$ne": ""},
                        }
                    },
                    {"$group": {"_id": "$Candidate Name", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                    {"$limit": 8},
                ]
            )
        )
        candidate_focus_chart = [
            {"name": row.get("_id") or "Unknown", "count": row["count"]}
            for row in candidate_focus_rows
        ]
        top_candidate = candidate_focus_chart[0] if candidate_focus_chart else None

        recent_records_rows = list(
            db.taskBody.find(
                {
                    **date_filter,
                    "assignedTo": {"$type": "string", "$ne": ""},
                    "status": "Completed",
                },
                {
                    "_id": 0,
                    "assignedTo": 1,
                    "Candidate Name": 1,
                    "actualRound": 1,
                    "Date of Interview": 1,
                    "receivedDateTime": 1,
                },
            )
            .sort("receivedDateTime", -1)
            .limit(6)
        )
        recent_records = [
            {
                "expert": display_name(row.get("assignedTo")),
                "candidate": row.get("Candidate Name") or "Unknown",
                "round": row.get("actualRound") or "Unknown",
                "date": row.get("Date of Interview") or (row.get("receivedDateTime") or "")[:10] or "N/A",
            }
            for row in recent_records_rows
        ]

        teams = list(teams_db.teams.find({}, {"_id": 0, "name": 1, "members": 1}))
        teams_configured = len(teams)

        page_cards = [
            {
                "name": "Expert Analytics",
                "metric": f"{len(ranked_experts)} experts",
                "description": "Interview load and stage progress by expert.",
                "href": url_for("analytics.expert_analytics"),
                "tone": "blue",
            },
            {
                "name": "Team Analytics",
                "metric": f"{len(ranked_teams)} teams",
                "description": "Team output and conversion movement.",
                "href": url_for("analytics.team_analytics"),
                "tone": "purple",
            },
            {
                "name": "Candidate Analytics",
                "metric": (
                    f"{top_candidate['count']} interviews for {top_candidate['name']}"
                    if top_candidate
                    else "Candidate conversion view"
                ),
                "description": "Candidate funnel performance across interview rounds.",
                "href": url_for("analytics.candidate_analytics"),
                "tone": "cyan",
            },
            {
                "name": "Interview Stats",
                "metric": f"{total_interviews} interviews",
                "description": "Status mix across completed, cancelled, and rescheduled work.",
                "href": url_for("analytics.interview_stats"),
                "tone": "green",
            },
            {
                "name": "Interview Records",
                "metric": f"{completed_interviews} completed logs",
                "description": "Round-level record volume for completed interviews.",
                "href": url_for("analytics.interview_records"),
                "tone": "orange",
            },
            {
                "name": "Expert Activity",
                "metric": f"{activity_summary.get('active_candidates', 0)} active candidates",
                "description": f"Monthly activity snapshot for {activity_period_label}.",
                "href": url_for("candidates.expert_candidate_activity"),
                "tone": "pink",
            },
            {
                "name": "Candidate Lookup",
                "metric": (
                    f"{top_candidate['count']} interviews for {top_candidate['name']}"
                    if top_candidate
                    else "Candidate drill-down"
                ),
                "description": "See who is accumulating the most interview touchpoints.",
                "href": url_for("candidates.candidate_lookup"),
                "tone": "cyan",
            },
            {
                "name": "Team Management",
                "metric": f"{teams_configured} configured teams",
                "description": "Team structure and member coverage.",
                "href": url_for("teams.manage"),
                "tone": "yellow",
            },
            {
                "name": "Export Center",
                "metric": "Exports ready",
                "description": "Download the same views surfaced across the dashboard.",
                "href": url_for("analytics.export_center"),
                "tone": "blue",
            },
        ]

        return {
            "period_label": period_label,
            "activity_period_label": activity_period_label,
            "headline": {
                "experts": len(ranked_experts),
                "teams": len(ranked_teams),
                "interviews": total_interviews,
                "completion_rate": interview_completion_rate,
                "avg_match_rate": kpi_data.get("summary", {}).get("avg_match_rate", 0),
                "active_candidates": activity_summary.get("active_candidates", 0),
            },
            "leaders": {
                "expert": ranked_experts[0] if ranked_experts else None,
                "team": ranked_teams[0] if ranked_teams else None,
                "kpi": ranked_kpi[0] if ranked_kpi else None,
                "activity_team": ranked_activity_teams[0] if ranked_activity_teams else None,
            },
            "page_cards": page_cards,
            "expert_chart": [
                {
                    "name": display_name(item.get("expert")),
                    "count": item.get("interview_count", 0),
                }
                for item in ranked_experts[:8]
            ],
            "team_chart": [
                {"name": item.get("team", "Unknown"), "count": item.get("interview_count", 0)}
                for item in ranked_teams[:8]
            ],
            "interview_status_chart": [
                {"name": name, "count": count}
                for name, count in interview_status_totals.items()
                if count > 0
            ],
            "record_round_chart": record_round_chart,
            "kpi_chart": [
                {
                    "name": display_name(item.get("expert")),
                    "count": item.get("match_rate", 0),
                }
                for item in ranked_kpi[:8]
            ],
            "activity_chart": [
                {"name": item.get("team", "Unknown"), "count": item.get("active", 0)}
                for item in ranked_activity_teams[:8]
            ],
            "candidate_focus_chart": candidate_focus_chart,
            "top_experts": [
                {
                    "name": display_name(item.get("expert")),
                    "count": item.get("interview_count", 0),
                    "meta": f"{item.get('screening_to_1st', 0)}% screening to 1st",
                }
                for item in ranked_experts[:5]
            ],
            "recent_records": recent_records,
        }

    data = get_dashboard_data(start_date, end_date, DASHBOARD_CACHE_VERSION)
    return render_template("index.html", **data)
