from flask import current_app, has_app_context

from db import get_db
from services.team_management import (
    clean_text,
    normalize_lookup_text,
    normalize_person_name,
    derive_names_from_email,
)

CACHE_VERSION = "v3"


def _cache_key(name, *parts):
    serialized = ":".join(str(part) for part in parts if part not in (None, ""))
    return f"ref:{CACHE_VERSION}:{name}" + (f":{serialized}" if serialized else "")


def _cache_result(key, timeout, builder):
    if has_app_context():
        cache = getattr(current_app, "cache", None)
        if cache:
            cached = cache.get(key)
            if cached is not None:
                return cached

            value = builder()
            cache.set(key, value, timeout=timeout)
            return value

    return builder()


def get_teams_reference():
    """
    Team / team-lead mapping, derived entirely from the main interviewSupport
    cluster's ``users`` collection -- no separate teams database.

    Logic (per user, 2026-07-02):
      1. Read every user's explicit ``teamLead`` field (falling back to
         ``TeamLead`` / ``Team Lead`` / ``team`` if ``teamLead`` is absent).
      2. A user whose own role IS "teamLead" is grouped under their OWN name
         (not their manager's), matching how teams are named everywhere else
         (e.g. "Team Anusree Vasudevan").
      3. Everyone else is grouped under their resolved team-lead name.
    """
    def build():
        db = get_db()
        users = list(
            db.users.find(
                {"manager": "Harsh Patel", "active": True},
                {
                    "_id": 0,
                    "email": 1,
                    "role": 1,
                    "teamLead": 1,
                    "TeamLead": 1,
                    "Team Lead": 1,
                    "team": 1,
                    "profile.displayName": 1,
                },
            )
        )

        expert_to_team = {}
        teams_map_sets = {}

        for user in users:
            email = clean_text(user.get("email")).lower()
            if not email:
                continue

            role = clean_text(user.get("role")).lower()
            if role and role != "expert":
                # Team leads, assistant managers, managers etc. are grouped
                # under their OWN name, not upward under whoever they report to
                # (matches the established "Team <name>" convention elsewhere,
                # e.g. Rujuwal Garg's own team).
                display = normalize_person_name((user.get("profile") or {}).get("displayName"))
                if not display:
                    derived = derive_names_from_email(email)
                    display = derived[0] if derived else email
                team_name = display
            else:
                raw_team_lead = (
                    user.get("teamLead")
                    or user.get("TeamLead")
                    or user.get("Team Lead")
                    or user.get("team")
                )
                team_name = normalize_person_name(raw_team_lead) if raw_team_lead else ""

            if not team_name:
                continue

            expert_to_team[email] = team_name
            teams_map_sets.setdefault(team_name, set()).add(email)

        teams_map = {team: sorted(members) for team, members in teams_map_sets.items()}
        return {
            "teams_map": teams_map,
            "teams_list": sorted(teams_map.keys()),
            "expert_to_team": expert_to_team,
            "all_experts": sorted(expert_to_team.keys()),
        }

    return _cache_result(_cache_key("teams"), 600, build)


def get_active_expert_emails(manager_name="Harsh Patel"):
    def build():
        db = get_db()
        active_experts_cursor = db.users.find(
            {"manager": manager_name, "active": True},
            {"email": 1, "_id": 0},
        )
        return sorted(
            {
                str(user["email"]).strip().lower()
                for user in active_experts_cursor
                if user.get("email")
            }
        )

    return _cache_result(_cache_key("active-experts", manager_name), 600, build)


def get_active_task_experts(completed_only=True, manager_name="Harsh Patel"):
    def build():
        db = get_db()
        active_experts = set(get_active_expert_emails(manager_name))
        query = {"assignedTo": {"$type": "string", "$ne": ""}}
        if completed_only:
            query["status"] = "Completed"

        task_experts = db.taskBody.distinct("assignedTo", query)
        return sorted(
            {
                normalize_lookup_text(expert)
                for expert in task_experts
                if normalize_lookup_text(expert) in active_experts
            }
        )

    return _cache_result(
        _cache_key("task-experts", "completed" if completed_only else "all", manager_name),
        600,
        build,
    )


def get_candidate_lookup_names(limit=500):
    def build():
        db = get_db()
        names_by_key = {}
        for value in db.taskBody.distinct(
            "Candidate Name",
            {"Candidate Name": {"$type": "string", "$ne": ""}},
        ):
            name = clean_text(value)
            name_key = normalize_lookup_text(name)
            if name and name_key and name_key not in names_by_key:
                names_by_key[name_key] = name

        return sorted(names_by_key.values())[:limit]

    return _cache_result(_cache_key("candidate-lookup", limit), 300, build)


def get_export_filter_options():
    def build():
        db = get_db()
        technologies = sorted(
            [value for value in db.candidateDetails.distinct("Technology") if value not in (None, "")]
        )
        workflow_statuses = sorted(
            [value for value in db.candidateDetails.distinct("workflowStatus") if value not in (None, "")]
        )
        return {
            "technologies": technologies,
            "workflow_statuses": workflow_statuses,
        }

    return _cache_result(_cache_key("export-options"), 600, build)


def get_kpi_round_titles():
    def build():
        db = get_db()
        rounds = db.taskBody.distinct("actualRound", {"status": "Completed"})
        return sorted([value for value in rounds if value and isinstance(value, str)])

    return _cache_result(_cache_key("kpi-rounds"), 600, build)
