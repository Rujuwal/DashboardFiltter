from flask import Blueprint, render_template, request, jsonify, current_app, has_app_context, send_file, redirect, url_for
from db import get_db
from datetime import datetime
from collections import Counter, defaultdict
from functools import lru_cache
import pandas as pd
import re
import json
from io import BytesIO
from services.interview_metrics import (
    EXCLUDED_ACTUAL_ROUNDS,
    build_interview_date_match,
    fetch_completed_interviews,
    parse_interview_date,
    stage_counts_by_group,
)
from services.reference_data import (
    get_active_expert_emails,
    get_active_task_experts,
    get_candidate_lookup_names,
    get_export_filter_options,
    get_teams_reference,
)
from services.team_management import (
    clean_text,
    get_team_management_directory,
    mongo_normalized_text,
    normalize_lookup_text,
    resolve_expert_management,
)

analytics_bp = Blueprint('analytics', __name__)
ANALYTICS_CACHE_VERSION = "v11"

# Round mapping from actualRound to funnel stages
ROUND_BUCKETS = {
    "screening": "Screening",
    "1st round": "1st",
    "first round": "1st",
    "2nd round": "2nd",
    "second round": "2nd",
    "3rd round": "3rd/Technical",
    "third round": "3rd/Technical",
    "tech round": "3rd/Technical",
    "technical": "3rd/Technical",
    "technical round": "3rd/Technical",
    "loop": "Loop Round",
    "loop round": "Loop Round",
    "final": "Final",
    "final round": "Final",
}

ROUND_BUCKET_PATTERNS = (
    (re.compile(r"\bscreen(?:ing)?\b"), "Screening"),
    (re.compile(r"\b(1st|first)\b"), "1st"),
    (re.compile(r"\b(2nd|second)\b"), "2nd"),
    (re.compile(r"\b(3rd|third)\b|\btech(?:nical)?\b"), "3rd/Technical"),
    (re.compile(r"\bloop\b"), "Loop Round"),
    (re.compile(r"\bfinal\b"), "Final"),
)

PIPELINE_ORDER = ["Screening", "1st", "2nd", "3rd/Technical", "Loop Round", "Final"]
INTERVIEW_STATUS_BUCKETS = {
    "completed": "Completed",
    "cancelled": "Cancelled",
    "canceled": "Cancelled",
    "rescheduled": "Rescheduled",
    "not done": "Not Done",
    "notdone": "Not Done",
    # "acknowledged": "Not Done",
    # "assigned": "Not Done",
    # "pending": "Not Done",
}

SUBJECT_MONTH_MAP = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'sept': '09', 'oct': '10', 'nov': '11', 'dec': '12',
    'january': '01', 'february': '02', 'march': '03', 'april': '04',
    'june': '06', 'july': '07', 'august': '08', 'september': '09',
    'october': '10', 'november': '11', 'december': '12',
}

SUBJECT_MONTH_TOKEN_PATTERN = (
    r'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|'
    r'Nov(?:ember)?|Dec(?:ember)?'
)

SUBJECT_WEEKDAY_TOKEN_PATTERN = (
    r'Mon(?:day)?|Tue(?:s(?:day)?)?|Wed(?:nesday)?|Thu(?:rs(?:day)?)?|'
    r'Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?'
)


def normalize_round(r):
    """Normalize actualRound string to funnel stage."""
    if not r:
        return None
    key = re.sub(r"[^a-z0-9]+", " ", str(r).strip().lower()).strip()
    if not key:
        return None

    mapped = ROUND_BUCKETS.get(key)
    if mapped:
        return mapped

    for pattern, stage in ROUND_BUCKET_PATTERNS:
        if pattern.search(key):
            return stage

    return None


def build_funnel_metrics(stages):
    """Build consistent funnel metrics from normalized stage counts."""
    scr = stages.get("Screening", 0)
    r1 = stages.get("1st", 0)
    r2 = stages.get("2nd", 0)
    r3 = stages.get("3rd/Technical", 0)
    loop = stages.get("Loop Round", 0)
    fin = stages.get("Final", 0)

    return {
        'interview_count': r1 + r2 + r3 + loop + fin,
        'screening': scr,
        'first': r1,
        'second': r2,
        'third_tech': r3,
        'loop_round': loop,
        'final': fin,
        'screening_to_1st': pct(r1, scr),
        'first_to_2nd': pct(r2, r1),
        'second_to_3rd': pct(r3, r2),
        'third_to_loop': pct(loop, r3),
        'loop_to_final': pct(fin, loop),
        'third_to_final': pct(fin, loop or r3),
    }


def pct(num, den):
    """Calculate percentage."""
    if den and den > 0:
        return round((num / den) * 100, 1)
    return 0.0


def get_date_filter_strings():
    """
    Get date filter as strings for taskBody collection.
    Defaults to current month if not provided.
    """
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')

    if not start_date and not end_date:
        today = datetime.now()
        start_date = today.replace(day=1).strftime('%Y-%m-%d')
        end_date = today.strftime('%Y-%m-%d')

    return start_date, end_date


def build_received_date_match(start_date="", end_date=""):
    # Filters on the real interview date (Date of Interview), not receivedDateTime.
    return build_interview_date_match(start_date, end_date)


def non_screening_round_expr():
    """
    Excludes Screening / On-Demand / AI-Interview rounds by SUBSTRING match
    (case-insensitive), not exact string equality. Some actualRound values are
    corrupted with the full raw email body instead of a clean round label
    (~3% of completed rows) -- an exact-match exclusion list silently lets
    those through as if they were real interviews. This matches the same
    "screen"/"demand"/"ai interview" substring rule bucket_of() (in
    services/interview_metrics.py) already uses for the funnel pages, so a
    corrupted-but-really-a-Screening row is excluded consistently everywhere.
    """
    return {
        "$not": {
            "$regexMatch": {
                "input": {"$toLower": {"$ifNull": ["$actualRound", ""]}},
                "regex": "screen|demand|ai interview",
            }
        }
    }


def build_interview_stats_match(start_date="", end_date=""):
    # Both the date filter and the round-exclusion filter use "$expr" -- merge them with
    # $and rather than spreading two dicts that would otherwise silently clobber each other.
    date_match = build_interview_date_match(start_date, end_date)
    exprs = [non_screening_round_expr()]
    if "$expr" in date_match:
        exprs.append(date_match["$expr"])
    return {
        "assignedTo": {"$type": "string", "$ne": ""},
        "$expr": exprs[0] if len(exprs) == 1 else {"$and": exprs},
    }


def build_interview_activity_match(statuses=None):
    match_query = {
        "assignedTo": {"$type": "string", "$ne": ""},
        "$expr": non_screening_round_expr(),
    }
    if statuses:
        normalized_statuses = [str(status).strip() for status in statuses if str(status).strip()]
        if len(normalized_statuses) == 1:
            match_query["status"] = normalized_statuses[0]
        elif normalized_statuses:
            match_query["status"] = {"$in": normalized_statuses}
    return match_query


def normalize_interview_status_bucket(status_value, default_bucket=None):
    normalized_bucket = INTERVIEW_STATUS_BUCKETS.get(normalize_lookup_text(status_value))
    return normalized_bucket or default_bucket


def resolve_interview_stats_expert_key(raw_expert, active_experts, expert_team_map, directory=None):
    expert_key = normalize_lookup_text(raw_expert)
    if not expert_key:
        return ""

    if expert_key in active_experts or expert_key in expert_team_map:
        return expert_key

    resolved = resolve_expert_management(raw_expert, directory=directory)
    resolved_email = normalize_lookup_text((resolved or {}).get("email"))
    return resolved_email or expert_key


def resolve_completed_interview_context(raw_expert, expert_team_map, directory=None):
    cleaned_expert = clean_text(raw_expert)
    if not cleaned_expert:
        return None

    expert_key = normalize_lookup_text(cleaned_expert)
    resolved = resolve_expert_management(raw_expert, directory=directory)
    resolved_email = normalize_lookup_text((resolved or {}).get("email"))
    if resolved_email:
        expert_key = resolved_email

    if not expert_key:
        return None

    team_name = (
        expert_team_map.get(expert_key)
        or clean_text((resolved or {}).get("team_name"))
        or "Unmapped"
    )
    return {
        "expert_key": expert_key,
        "team_name": team_name,
    }


def build_completed_interview_query():
    return {
        "status": "Completed",
        "assignedTo": {"$type": "string", "$ne": ""},
        "$expr": non_screening_round_expr(),
    }


def aggregate_interview_stats_by_expert(db, start_date="", end_date="", active_experts=None, expert_team_map=None):
    if active_experts is None:
        active_experts = get_active_experts(db)
    if expert_team_map is None:
        expert_team_map = get_expert_team_map(db)[0]

    expert_stats_map = {}

    def get_stats(expert_key, team_name):
        return expert_stats_map.setdefault(
            expert_key,
            {
                "Expert": expert_key,
                "Team": team_name,
                "CompletedCount": 0,
                "CancelledCount": 0,
                "RescheduledCount": 0,
                "NotDoneCount": 0,
                "TotalInterviews": 0,
            },
        )

    # Completed counts come from the exact same funnel computation Expert/Team Analytics
    # use (get_expert_funnel_data -> fetch_completed_interviews + bucket_of, with loop
    # rounds deduped by candidate+end client) -- a single source of truth, so "Completed"
    # here can never drift from those pages again, including for actualRound values
    # corrupted with raw email-body text that an exact-string exclusion list would miss.
    funnel_stats, _ = get_expert_funnel_data(db, start_date, end_date, None, None)
    for stat in funnel_stats:
        expert_key = stat['expert']
        if expert_key not in active_experts:
            continue
        stats = get_stats(expert_key, expert_team_map.get(expert_key, "Unmapped"))
        stats["CompletedCount"] = stat['interview_count']
        stats["TotalInterviews"] += stat['interview_count']

    # Cancelled / Rescheduled / Not Done have no funnel-stage equivalent to align to, so
    # they still come from the raw activity records (with the same substring-based round
    # exclusion as everywhere else, via get_interview_stats_records/build_interview_activity_match).
    for record in get_interview_stats_records(
        db,
        start_date=start_date,
        end_date=end_date,
        active_experts=active_experts,
        expert_team_map=expert_team_map,
    ):
        status_bucket = record["status_bucket"]
        if status_bucket == "Completed" or not status_bucket:
            continue

        expert_key = record["expert_key"]
        stats = get_stats(expert_key, record["team_name"])
        if status_bucket == "Cancelled":
            stats["CancelledCount"] += 1
        elif status_bucket == "Rescheduled":
            stats["RescheduledCount"] += 1
        elif status_bucket == "Not Done":
            stats["NotDoneCount"] += 1
        stats["TotalInterviews"] += 1

    return expert_stats_map


def get_active_experts(db):
    return set(get_active_expert_emails())


def get_expert_team_map(db=None):
    reference = get_teams_reference()
    return reference["expert_to_team"], reference["teams_map"]


def get_analytics_filter_options(completed_only=True):
    reference = get_teams_reference()
    return reference["teams_list"], get_active_task_experts(completed_only=completed_only), reference["teams_map"]


def get_completed_interview_filter_options(db):
    cache_key = analytics_cache_key("completed-interview-filter-options")
    cache = getattr(current_app, "cache", None) if has_app_context() else None
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    expert_team_map, teams_map = get_expert_team_map(db)
    records = get_completed_interview_records(
        db,
        expert_team_map=expert_team_map,
    )
    teams_map_view = defaultdict(set)
    for team_name, members in teams_map.items():
        teams_map_view[team_name].update(members)
    for record in records:
        teams_map_view[record["team_name"]].add(record["expert_key"])

    teams_list = sorted(teams_map_view.keys())
    experts = sorted({record["expert_key"] for record in records})
    value = (teams_list, experts, {team: sorted(members) for team, members in teams_map_view.items()})
    if cache:
        cache.set(cache_key, value, timeout=300)
    return value


def get_interview_stats_filter_options(db):
    cache_key = analytics_cache_key("interview-stats-filter-options")
    cache = getattr(current_app, "cache", None) if has_app_context() else None
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    expert_team_map, teams_map = get_expert_team_map(db)
    directory = get_team_management_directory()
    teams_map_view = defaultdict(set)
    for team_name, members in teams_map.items():
        teams_map_view[team_name].update(members)

    raw_experts = db.taskBody.distinct("assignedTo", build_interview_stats_match())
    for raw_expert in raw_experts:
        context = resolve_completed_interview_context(
            raw_expert,
            expert_team_map,
            directory=directory,
        )
        if not context:
            continue
        teams_map_view[context["team_name"]].add(context["expert_key"])

    teams_list = sorted(teams_map_view.keys())
    experts = sorted({member for members in teams_map_view.values() for member in members})
    value = (teams_list, experts, {team: sorted(members) for team, members in teams_map_view.items()})
    if cache:
        cache.set(cache_key, value, timeout=300)
    return value


def analytics_cache_key(name, *parts):
    serialized = ":".join(str(part) if part not in (None, "") else "all" for part in parts)
    return f"analytics:{ANALYTICS_CACHE_VERSION}:{name}:{serialized}"


def build_task_query(start_date='', end_date=''):
    """Base query for taskBody: completed interviews filtered by the real interview date
    (Date of Interview), excluding On-Demand/AI rounds."""
    match_filters = {
        "status": "Completed",
        "assignedTo": {"$type": "string", "$ne": ""},
        "actualRound": {"$nin": EXCLUDED_ACTUAL_ROUNDS},
    }
    match_filters.update(build_interview_date_match(start_date, end_date))
    return match_filters


def get_expert_funnel_data(db, start_date='', end_date='', filter_team=None, filter_expert=None):
    """
    Get expert funnel data from taskBody collection.

    Expert-Level Filtration Rules:
    0. ONLY include experts who are active (active=true) and manager="Harsh Patel" from users collection
    1. Determine expert's team using case-insensitive email-to-team mapping
    2. Apply Team Filter first:
       - If filter_team is not null, include only experts from that team
       - Exclude all experts from other teams
    3. Apply Expert Filter:
       - If filter_expert is not null, include only that specific expert
       - Exclude all other experts
    4. If BOTH filters are provided:
       - Expert must satisfy BOTH conditions
       - Must belong to filter_team AND email must match filter_expert

    Experts that do not meet active filters will NOT appear in output.
    """
    cache = current_app.cache
    cache_key = analytics_cache_key("expert-funnel", start_date, end_date, filter_team, filter_expert)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    filter_expert_key = normalize_lookup_text(filter_expert)

    # Get active experts first
    active_experts = get_active_experts(db)

    expert_team_map, teams_map = get_expert_team_map(db)

    # Classify completed interviews on the real interview date (Date of Interview),
    # excluding On-Demand/AI, and deduping loop rounds per (candidate, end client) --
    # identical rules to the daily conversion brief / PO report.
    records = fetch_completed_interviews(db, start_date, end_date)
    expert_stage_counts = stage_counts_by_group(
        records, lambda r: normalize_lookup_text(r.get("assignedTo"))
    )

    # Build expert stats with filtration
    expert_stats = []
    for expert, stages in expert_stage_counts.items():
        if expert not in active_experts:
            continue

        team_name = expert_team_map.get(expert, "Unmapped")
        if filter_team is not None and team_name != filter_team:
            continue
        if filter_expert_key and expert != filter_expert_key:
            continue

        expert_stats.append({
            'expert': expert,
            'team': team_name,
            **build_funnel_metrics(stages),
        })

    expert_stats.sort(key=lambda x: (x['screening_to_1st'], x['interview_count']), reverse=True)

    for idx, stat in enumerate(expert_stats):
        stat['rank'] = idx + 1

    value = (expert_stats, teams_map)
    cache.set(cache_key, value, timeout=300)
    return value


def get_candidate_funnel_data(
    db,
    start_date='',
    end_date='',
    filter_team=None,
    filter_expert=None,
    filter_candidate=None,
):
    cache = current_app.cache
    cache_key = analytics_cache_key(
        "candidate-funnel",
        start_date,
        end_date,
        filter_team,
        filter_expert,
        filter_candidate,
    )
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    filter_expert_key = normalize_lookup_text(filter_expert)
    filter_candidate_key = normalize_lookup_text(filter_candidate)
    expert_team_map, teams_map = get_expert_team_map(db)

    # Interview-date basis, On-Demand/AI excluded, loops deduped per (candidate, end client).
    records = fetch_completed_interviews(db, start_date, end_date)

    def _team_for(record):
        expert_key = normalize_lookup_text(record.get("assignedTo"))
        return expert_key, (expert_team_map.get(expert_key, "Unmapped") if expert_key else "Unmapped")

    kept = []
    for record in records:
        expert_key, team_name = _team_for(record)
        if filter_team and team_name != filter_team:
            continue
        if filter_expert_key and expert_key != filter_expert_key:
            continue
        if filter_candidate_key and record.get("candidate_key") != filter_candidate_key:
            continue
        kept.append(record)

    candidate_stage_counts = stage_counts_by_group(kept, lambda r: r.get("candidate_key"))
    candidate_name_counts = defaultdict(Counter)
    candidate_expert_counts = defaultdict(Counter)
    candidate_team_counts = defaultdict(Counter)
    for record in kept:
        candidate_key = record.get("candidate_key")
        if not candidate_key:
            continue
        candidate_name_counts[candidate_key][clean_text(record.get("Candidate Name")) or "Unknown"] += 1
        expert_key, team_name = _team_for(record)
        if expert_key:
            candidate_expert_counts[candidate_key][expert_key] += 1
        candidate_team_counts[candidate_key][team_name] += 1

    candidate_stats = []
    for candidate_key, stages in candidate_stage_counts.items():
        name_counter = candidate_name_counts.get(candidate_key, Counter())
        expert_counter = candidate_expert_counts.get(candidate_key, Counter())
        team_counter = candidate_team_counts.get(candidate_key, Counter())
        funnel_metrics = build_funnel_metrics(stages)
        activity_total = sum(stages.values())

        candidate_stats.append({
            "candidate_key": candidate_key,
            "candidate": name_counter.most_common(1)[0][0] if name_counter else "Unknown",
            "lead_team": team_counter.most_common(1)[0][0] if team_counter else "",
            "lead_expert": expert_counter.most_common(1)[0][0] if expert_counter else "",
            "teams_count": len(team_counter),
            "experts_count": len(expert_counter),
            "activity_total": activity_total,
            **funnel_metrics,
        })

    candidate_stats.sort(
        key=lambda item: (
            item["activity_total"],
            item["screening"],
            item["interview_count"],
            item["first"],
            item["final"],
        ),
        reverse=True,
    )

    for idx, stat in enumerate(candidate_stats):
        stat["rank"] = idx + 1

    # PO/client data removed from the dashboard.
    client_data = {"state": "disabled", "client_counts_by_candidate": {}}
    value = (candidate_stats, teams_map, client_data)
    cache.set(cache_key, value, timeout=300)
    return value


def get_candidate_detail_data(
    db,
    candidate_key,
    start_date='',
    end_date='',
    filter_team=None,
    filter_expert=None,
):
    cache_key = analytics_cache_key(
        "candidate-detail",
        start_date,
        end_date,
        filter_team,
        filter_expert,
        candidate_key,
    )
    cached = current_app.cache.get(cache_key)
    if cached is not None:
        return cached

    expert_team_map, _ = get_expert_team_map(db)
    filter_expert_key = normalize_lookup_text(filter_expert)
    task_query = build_task_query(start_date, end_date)
    # Merge the candidate-name match with any existing interview-date $expr (don't clobber).
    candidate_expr = {
        "$eq": [
            mongo_normalized_text("Candidate Name"),
            candidate_key,
        ]
    }
    if "$expr" in task_query:
        task_query["$expr"] = {"$and": [task_query["$expr"], candidate_expr]}
    else:
        task_query["$expr"] = candidate_expr

    raw_tasks = list(
        db.taskBody.find(
            task_query,
            {
                "Candidate Name": 1,
                "actualRound": 1,
                "assignedTo": 1,
                "receivedDateTime": 1,
            },
        ).sort("receivedDateTime", -1).limit(100)
    )

    candidate_tasks = []
    round_distribution = Counter()
    expert_distribution = Counter()

    for task in raw_tasks:
        expert_value = clean_text(task.get("assignedTo"))
        expert_key = normalize_lookup_text(expert_value)
        team_name = expert_team_map.get(expert_key, "Unmapped") if expert_key else "Unmapped"
        if filter_team and team_name != filter_team:
            continue
        if filter_expert_key and expert_key != filter_expert_key:
            continue

        stage = normalize_round(task.get("actualRound"))
        if stage:
            round_distribution[stage] += 1
        if expert_value:
            expert_distribution[expert_value] += 1

        candidate_tasks.append({
            "round": clean_text(task.get("actualRound")) or "Unknown",
            "expert": expert_value or "Unassigned",
            "team": team_name,
            "date": (task.get("receivedDateTime") or "")[:10] if task.get("receivedDateTime") else "-",
        })

    value = {
        "candidate_tasks": candidate_tasks[:25],
        "round_distribution": {
            stage: round_distribution[stage]
            for stage in PIPELINE_ORDER
            if round_distribution.get(stage)
        },
        "expert_distribution": dict(expert_distribution.most_common(10)),
    }
    current_app.cache.set(cache_key, value, timeout=300)
    return value


def get_team_funnel_data(db, start_date='', end_date='', filter_team=None, filter_expert=None):
    """
    Get team funnel data from taskBody collection.

    Filtration Rules:
    1. If filter_team is set: only include that team
    2. If filter_expert is set: only include that expert's data within teams
    3. If both are set: expert must belong to filter_team AND match filter_expert
    4. Team aggregations only include data from experts that pass all filters
    """
    cache = current_app.cache
    cache_key = analytics_cache_key("team-funnel", start_date, end_date, filter_team, filter_expert)
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    _, teams_map = get_expert_team_map(db)
    expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
    expert_stats_map = {stat['expert']: stat for stat in expert_stats}

    team_stats = []
    for team_name, members in teams_map.items():
        if filter_team and team_name != filter_team:
            continue

        effective_members = members
        if filter_expert:
            effective_members = [member for member in members if member == filter_expert]
            if not effective_members:
                continue

        agg = Counter()
        contributing_members = 0
        for member in effective_members:
            stats = expert_stats_map.get(member)
            if stats:
                contributing_members += 1
                agg['Screening'] += stats['screening']
                agg['1st'] += stats['first']
                agg['2nd'] += stats['second']
                agg['3rd/Technical'] += stats['third_tech']
                agg['Loop Round'] += stats['loop_round']
                agg['Final'] += stats['final']

        if contributing_members == 0:
            continue

        team_stats.append({
            'team': team_name,
            'member_count': len(members),
            'active_member_count': contributing_members,
            **build_funnel_metrics(agg),
        })

    team_stats.sort(key=lambda x: (x['screening_to_1st'], x['interview_count']), reverse=True)

    for idx, stat in enumerate(team_stats):
        stat['rank'] = idx + 1

    value = (team_stats, teams_map)
    cache.set(cache_key, value, timeout=300)
    return value


def get_active_candidate_stats_by_expert(db):
    """
    Per-expert live caseload: how many candidates they currently have Active in
    candidateDetails, and what those candidates' top technologies are. This is
    the simple replacement for the old Expert Activity page.
    """
    cache_key = analytics_cache_key("active-candidate-stats")
    cache = getattr(current_app, "cache", None) if has_app_context() else None
    if cache:
        cached = cache.get(cache_key)
        if cached is not None:
            return cached

    rows = list(db.candidateDetails.aggregate([
        {"$match": {"status": "Active", "Expert": {"$type": "string", "$ne": ""}}},
        {"$group": {
            "_id": {"expert": {"$toLower": "$Expert"}, "tech": {"$ifNull": ["$Technology", "Unspecified"]}},
            "n": {"$sum": 1},
        }},
    ]))

    by_expert = defaultdict(Counter)
    for row in rows:
        row_id = row.get("_id") or {}
        expert = row_id.get("expert")
        if not expert:
            continue
        tech = clean_text(row_id.get("tech")) or "Unspecified"
        by_expert[expert][tech] += row.get("n", 0)

    stats = {}
    for expert, tech_counts in by_expert.items():
        stats[expert] = {
            "active_count": sum(tech_counts.values()),
            "top_technologies": ", ".join(f"{name} ({n})" for name, n in tech_counts.most_common(3)),
        }

    if cache:
        cache.set(cache_key, stats, timeout=300)
    return stats


@analytics_bp.route('/experts')
def expert_analytics():
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    # Get filter parameters
    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None

    teams_list, all_experts, teams_map = get_analytics_filter_options(completed_only=True)
    # Get expert funnel data
    expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)

    # Attach live active-candidate caseload (count + top technologies)
    active_candidate_stats = get_active_candidate_stats_by_expert(db)
    for stat in expert_stats:
        extra = active_candidate_stats.get(stat['expert'], {})
        stat['active_candidates'] = extra.get('active_count', 0)
        stat['candidate_types'] = extra.get('top_technologies', '')

    # Get selected expert detail
    selected_expert = normalize_lookup_text(request.args.get('view_expert', ''))
    expert_detail = None
    expert_tasks = []

    if selected_expert:
        # Find expert stats
        for stat in expert_stats:
            if stat['expert'] == selected_expert:
                expert_detail = stat
                break

        if not expert_detail:
            # Get data for this specific expert
            single_stats, _ = get_expert_funnel_data(db, start_date, end_date, None, selected_expert)
            if single_stats:
                expert_detail = single_stats[0]
                extra = active_candidate_stats.get(expert_detail['expert'], {})
                expert_detail['active_candidates'] = extra.get('active_count', 0)
                expert_detail['candidate_types'] = extra.get('top_technologies', '')

        if expert_detail:
            detail_cache_key = analytics_cache_key("expert-detail", start_date, end_date, selected_expert)
            cached_detail = current_app.cache.get(detail_cache_key)

            if cached_detail is None:
                # Get recent tasks for this expert
                task_query = build_task_query(start_date, end_date)
                task_query['assignedTo'] = {"$regex": f"^{re.escape(selected_expert)}$", "$options": "i"}

                expert_tasks = list(db.taskBody.find(task_query).sort('receivedDateTime', -1).limit(25))

                # Get round distribution for charts
                round_pipeline = [
                    {'$match': task_query},
                    {'$group': {'_id': '$actualRound', 'count': {'$sum': 1}}},
                    {'$sort': {'count': -1}}
                ]
                round_dist = list(db.taskBody.aggregate(round_pipeline))
                cached_detail = {
                    'expert_tasks': expert_tasks,
                    'round_distribution': {
                        (item['_id'] or 'Unknown'): item['count']
                        for item in round_dist
                    }
                }
                current_app.cache.set(detail_cache_key, cached_detail, timeout=300)

            expert_tasks = cached_detail.get('expert_tasks', [])
            expert_detail['round_distribution'] = cached_detail.get('round_distribution', {})

    return render_template(
        'expert_analytics.html',
        expert_stats=expert_stats,
        teams=teams_list,
        experts=all_experts,
        teams_map=teams_map,
        selected_team=filter_team or '',
        selected_expert=filter_expert or '',
        view_expert=selected_expert,
        expert_detail=expert_detail,
        expert_tasks=expert_tasks,
        start_date=start_date,
        end_date=end_date,
        total_experts=len(expert_stats)
    )


@analytics_bp.route('/candidates')
def candidate_analytics():
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None
    filter_candidate_name = clean_text(request.args.get('candidate', ''))
    filter_candidate = normalize_lookup_text(filter_candidate_name) or None

    teams_list, all_experts, _ = get_analytics_filter_options(completed_only=True)
    all_candidates = sorted(
        {
            name
            for name in [
                *get_candidate_lookup_names(limit=2000),
                filter_candidate_name,
                clean_text(request.args.get('view_candidate', '')),
            ]
            if name
        }
    )

    candidate_stats, _, client_data = get_candidate_funnel_data(
        db,
        start_date,
        end_date,
        filter_team,
        filter_expert,
        filter_candidate,
    )

    selected_candidate_name = clean_text(request.args.get('view_candidate', ''))
    selected_candidate = normalize_lookup_text(selected_candidate_name)
    candidate_detail = None
    candidate_tasks = []

    if selected_candidate:
        for stat in candidate_stats:
            if stat["candidate_key"] == selected_candidate:
                candidate_detail = dict(stat)
                break

        if not candidate_detail:
            single_stats, _, _ = get_candidate_funnel_data(
                db,
                start_date,
                end_date,
                filter_team,
                filter_expert,
                selected_candidate,
            )
            if single_stats:
                candidate_detail = dict(single_stats[0])

        if candidate_detail:
            detail_data = get_candidate_detail_data(
                db,
                selected_candidate,
                start_date,
                end_date,
                filter_team,
                filter_expert,
            )
            candidate_tasks = detail_data.get("candidate_tasks", [])
            candidate_detail["round_distribution"] = detail_data.get("round_distribution", {})
            candidate_detail["expert_distribution"] = detail_data.get("expert_distribution", {})

    return render_template(
        'candidate_analytics.html',
        candidate_stats=candidate_stats,
        teams=teams_list,
        experts=all_experts,
        candidates=all_candidates,
        selected_team=filter_team or '',
        selected_expert=filter_expert or '',
        selected_candidate=filter_candidate_name,
        view_candidate=selected_candidate_name,
        candidate_detail=candidate_detail,
        candidate_tasks=candidate_tasks,
        start_date=start_date,
        end_date=end_date,
        total_candidates=len(candidate_stats),
    )


@analytics_bp.route('/candidates/export')
def export_candidate_analytics():
    db = get_db()
    start_date = request.args.get('start_date', '')
    end_date = request.args.get('end_date', '')
    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None
    candidate_name = clean_text(request.args.get('candidate') or request.args.get('view_candidate'))
    filter_candidate = normalize_lookup_text(candidate_name) or None
    export_format = (request.args.get('format') or 'excel').lower()
    if export_format not in {'excel', 'csv'}:
        export_format = 'excel'

    candidate_stats, _, client_data = get_candidate_funnel_data(
        db,
        start_date,
        end_date,
        filter_team,
        filter_expert,
        filter_candidate,
    )
    return export_candidate_analytics_excel(candidate_stats, client_data, start_date, end_date, export_format)


@analytics_bp.route('/teams')
def team_analytics():
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    # Get filter parameters
    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None

    teams_list, all_experts, teams_map = get_analytics_filter_options(completed_only=True)
    # Get team funnel data (with filters applied for alignment)
    team_stats, _ = get_team_funnel_data(db, start_date, end_date, filter_team, filter_expert)

    # Get selected team detail
    selected_team = request.args.get('view_team', '')
    team_detail = None
    member_stats = []

    if selected_team:
        # Find team stats from filtered results
        for stat in team_stats:
            if stat['team'] == selected_team:
                team_detail = stat
                break

        if team_detail:
            # Get member-level breakdown with same filters applied
            # This ensures alignment: if filter_expert is set, only that expert shows
            expert_stats, _ = get_expert_funnel_data(
                db, start_date, end_date,
                selected_team,  # Force team filter to the viewed team
                filter_expert   # Maintain expert filter for alignment
            )

            # All returned experts should be from this team already
            members = teams_map.get(selected_team, [])
            member_stats = [s for s in expert_stats if s['expert'] in members]

            # Team Lead -> Expert -> Candidate (with interview count) drill-down.
            # interview_count here is computed the exact same way as an expert's own
            # interview_count (get_candidate_funnel_data shares fetch_completed_interviews /
            # build_funnel_metrics with get_expert_funnel_data) -- just attributed per candidate.
            candidate_stats, _, _ = get_candidate_funnel_data(
                db, start_date, end_date, selected_team, filter_expert
            )
            candidates_by_expert = defaultdict(list)
            for c in candidate_stats:
                if c.get('interview_count', 0) <= 0:
                    continue
                # Same funnel breakdown an expert row gets, just per candidate --
                # build_funnel_metrics() already put all of this on c.
                candidates_by_expert[c['lead_expert']].append({
                    'name': c['candidate'],
                    'interview_count': c['interview_count'],
                    'screening': c['screening'],
                    'first': c['first'],
                    'second': c['second'],
                    'third_tech': c['third_tech'],
                    'loop_round': c['loop_round'],
                    'final': c['final'],
                    'screening_to_1st': c['screening_to_1st'],
                    'first_to_2nd': c['first_to_2nd'],
                    'second_to_3rd': c['second_to_3rd'],
                    'third_to_loop': c['third_to_loop'],
                    'loop_to_final': c['loop_to_final'],
                })
            for stat in member_stats:
                members_candidates = candidates_by_expert.get(stat['expert'], [])
                members_candidates.sort(key=lambda x: x['interview_count'], reverse=True)
                stat['candidates'] = members_candidates

    return render_template(
        'team_analytics.html',
        team_stats=team_stats,
        teams=teams_list,
        experts=all_experts,
        teams_map=teams_map,
        selected_team=filter_team or '',
        selected_expert=filter_expert or '',
        view_team=selected_team,
        team_detail=team_detail,
        member_stats=member_stats,
        start_date=start_date,
        end_date=end_date,
        total_teams=len(team_stats)
    )


@analytics_bp.route('/funnel')
def funnel_analytics():
    return redirect(url_for('dashboard.index'))


@analytics_bp.route('/interview-stats')
def interview_stats():
    """
    Interview Statistics page showing Completed, Cancelled, Rescheduled, Not Done counts
    per expert/team with date filtering.
    """
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    # Get filter parameters
    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None

    expert_team_map, teams_map = get_expert_team_map(db)
    teams_list, all_experts, _ = get_interview_stats_filter_options(db)

    cache_key = analytics_cache_key(
        "interview-stats-page",
        start_date,
        end_date,
        filter_team,
        filter_expert,
    )
    cached = current_app.cache.get(cache_key)
    if cached is None:
        expert_stats_map = aggregate_interview_stats_by_expert(
            db,
            start_date,
            end_date,
            expert_team_map=expert_team_map,
        )

        team_data = []
        expert_data = []
        team_members_map = defaultdict(list)

        for expert_key, data in expert_stats_map.items():
            team_name = data.get("Team") or "Unmapped"
            if filter_team and team_name != filter_team:
                continue
            if filter_expert and expert_key != filter_expert:
                continue

            member_stat = {
                'expert': expert_key,
                'completed': data["CompletedCount"],
                'cancelled': data["CancelledCount"],
                'rescheduled': data["RescheduledCount"],
                'notdone': data["NotDoneCount"],
                'total': data["TotalInterviews"],
            }
            team_members_map[team_name].append(member_stat)
            expert_data.append({
                'team': team_name,
                **member_stat,
            })

        if filter_team and filter_team not in team_members_map and filter_team in teams_list:
            team_members_map[filter_team] = []

        for team_name, member_stats in team_members_map.items():
            reference_members = teams_map.get(team_name, [])
            dynamic_members = [member['expert'] for member in member_stats]
            member_count = len(set(reference_members) | set(dynamic_members))
            team_data.append({
                'team': team_name,
                'member_count': member_count,
                'active_members': len([m for m in member_stats if m['total'] > 0]),
                'completed': sum(member['completed'] for member in member_stats),
                'cancelled': sum(member['cancelled'] for member in member_stats),
                'rescheduled': sum(member['rescheduled'] for member in member_stats),
                'notdone': sum(member['notdone'] for member in member_stats),
                'total': sum(member['total'] for member in member_stats),
                'members': sorted(member_stats, key=lambda x: x['total'], reverse=True)
            })

        team_data.sort(key=lambda x: (x['total'], x['team']), reverse=True)
        expert_data.sort(key=lambda x: (x['total'], x['expert']), reverse=True)
        teams_map_view = {
            team_name: sorted(set(teams_map.get(team_name, [])) | {member['expert'] for member in team['members']})
            for team_name, team in ((item['team'], item) for item in team_data)
        }

        cached = {
            'team_data': team_data,
            'expert_data': expert_data,
            'overall_completed': sum(t['completed'] for t in team_data),
            'overall_cancelled': sum(t['cancelled'] for t in team_data),
            'overall_rescheduled': sum(t['rescheduled'] for t in team_data),
            'overall_notdone': sum(t['notdone'] for t in team_data),
            'overall_total': sum(t['total'] for t in team_data),
            'teams_map_view': teams_map_view,
        }
        current_app.cache.set(cache_key, cached, timeout=300)

    return render_template(
        'interview_stats.html',
        teams=teams_list,
        experts=all_experts,
        teams_map=cached['teams_map_view'],
        team_data=cached['team_data'],
        expert_data=cached['expert_data'],
        selected_team=filter_team or '',
        selected_expert=filter_expert or '',
        start_date=start_date,
        end_date=end_date,
        overall_completed=cached['overall_completed'],
        overall_cancelled=cached['overall_cancelled'],
        overall_rescheduled=cached['overall_rescheduled'],
        overall_notdone=cached['overall_notdone'],
        overall_total=cached['overall_total'],
    )

@lru_cache(maxsize=4096)
def extract_interview_date_candidates_from_subject(subject):
    """
    Extract possible interview dates from a subject line.
    """
    if not subject:
        return ()

    normalized_subject = " ".join(
        str(subject)
        .replace("\xa0", " ")
        .replace("\u202f", " ")
        .split()
    )
    if not normalized_subject:
        return ()

    candidates = []
    seen = set()

    def add_candidate(year_value, month_value, day_value):
        try:
            candidate = datetime(
                int(year_value),
                int(month_value),
                int(day_value),
            ).strftime("%Y-%m-%d")
        except (TypeError, ValueError):
            return

        if candidate not in seen:
            seen.add(candidate)
            candidates.append(candidate)

    month_first_pattern = re.compile(
        rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?({SUBJECT_MONTH_TOKEN_PATTERN})\.?\s*(?:,)?\s*(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:,)?\s*(\d{{4}})\b',
        re.IGNORECASE,
    )
    day_first_pattern = re.compile(
        rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+({SUBJECT_MONTH_TOKEN_PATTERN})\.?\s*(?:,)?\s*(\d{{4}})\b',
        re.IGNORECASE,
    )
    iso_pattern = re.compile(r'\b(\d{4})[/-](\d{1,2})[/-](\d{1,2})\b')
    numeric_pattern = re.compile(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{4})\b')
    numeric_short_year_pattern = re.compile(r'\b(\d{1,2})[/-](\d{1,2})[/-](\d{2})\b')

    for month_str, day, year in month_first_pattern.findall(normalized_subject):
        month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
        if month:
            add_candidate(year, month, day)

    for day, month_str, year in day_first_pattern.findall(normalized_subject):
        month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
        if month:
            add_candidate(year, month, day)

    for year, month, day in iso_pattern.findall(normalized_subject):
        add_candidate(year, month, day)

    for first, second, year in numeric_pattern.findall(normalized_subject):
        first_num = int(first)
        second_num = int(second)

        if 1 <= first_num <= 12 and 1 <= second_num <= 31:
            add_candidate(year, first_num, second_num)
        if 1 <= first_num <= 31 and 1 <= second_num <= 12:
            add_candidate(year, second_num, first_num)

    for first, second, year in numeric_short_year_pattern.findall(normalized_subject):
        expanded_year = f"20{year}"
        first_num = int(first)
        second_num = int(second)

        if 1 <= first_num <= 12 and 1 <= second_num <= 31:
            add_candidate(expanded_year, first_num, second_num)
        if 1 <= first_num <= 31 and 1 <= second_num <= 12:
            add_candidate(expanded_year, second_num, first_num)

    return tuple(candidates)


def parse_interview_date_from_subject(subject, reference_date=None):
    """
    Extract interview date from subject line and return YYYY-MM-DD.
    """
    candidates = extract_interview_date_candidates_from_subject(subject)
    if not candidates:
        candidates = ()

    reference_value = str(reference_date or "").strip()[:10]
    try:
        reference_dt = datetime.strptime(reference_value, "%Y-%m-%d")
    except ValueError:
        reference_dt = None

    if not candidates and reference_dt:
        normalized_subject = " ".join(
            str(subject or "")
            .replace("\xa0", " ")
            .replace("\u202f", " ")
            .split()
        )
        inferred_candidates = []
        seen = set()

        def add_inferred_candidate(year_value, month_value, day_value):
            try:
                candidate = datetime(
                    int(year_value),
                    int(month_value),
                    int(day_value),
                ).strftime("%Y-%m-%d")
            except (TypeError, ValueError):
                return

            if candidate not in seen:
                seen.add(candidate)
                inferred_candidates.append(candidate)

        month_first_partial_year_pattern = re.compile(
            rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?({SUBJECT_MONTH_TOKEN_PATTERN})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:,)?\s*(\d{{3}})\b',
            re.IGNORECASE,
        )
        day_first_partial_year_pattern = re.compile(
            rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+({SUBJECT_MONTH_TOKEN_PATTERN})\.?\s*(?:,)?\s*(\d{{3}})\b',
            re.IGNORECASE,
        )
        month_first_no_year_pattern = re.compile(
            rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?({SUBJECT_MONTH_TOKEN_PATTERN})\.?\s*(\d{{1,2}})(?:st|nd|rd|th)?\b',
            re.IGNORECASE,
        )
        day_first_no_year_pattern = re.compile(
            rf'\b(?:(?:{SUBJECT_WEEKDAY_TOKEN_PATTERN})\.?,?\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+({SUBJECT_MONTH_TOKEN_PATTERN})\.?\b',
            re.IGNORECASE,
        )

        for month_str, day, year_fragment in month_first_partial_year_pattern.findall(normalized_subject):
            if str(reference_dt.year).startswith(year_fragment):
                month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
                if month:
                    add_inferred_candidate(reference_dt.year, month, day)

        for day, month_str, year_fragment in day_first_partial_year_pattern.findall(normalized_subject):
            if str(reference_dt.year).startswith(year_fragment):
                month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
                if month:
                    add_inferred_candidate(reference_dt.year, month, day)

        if not inferred_candidates:
            for month_str, day in month_first_no_year_pattern.findall(normalized_subject):
                month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
                if month:
                    add_inferred_candidate(reference_dt.year, month, day)

            for day, month_str in day_first_no_year_pattern.findall(normalized_subject):
                month = SUBJECT_MONTH_MAP.get(month_str.lower().rstrip('.'))
                if month:
                    add_inferred_candidate(reference_dt.year, month, day)

        candidates = tuple(inferred_candidates)

    if not candidates:
        return None

    if len(candidates) == 1 or not reference_dt:
        return candidates[0]

    def candidate_distance(candidate_value):
        try:
            candidate_dt = datetime.strptime(candidate_value, "%Y-%m-%d")
        except ValueError:
            return (float("inf"), candidate_value)
        return (abs((candidate_dt - reference_dt).days), candidate_value)

    return min(candidates, key=candidate_distance)


def get_effective_interview_date(record):
    # Prefer the authoritative Date of Interview field (MM/DD/YYYY) used by the daily brief.
    doi = parse_interview_date(record.get("Date of Interview"))
    if doi:
        return doi.isoformat()

    subject_date = parse_interview_date_from_subject(
        record.get("subject", ""),
        reference_date=record.get("receivedDateTime"),
    )
    if subject_date:
        return subject_date

    received_date = str(record.get("receivedDateTime") or "").strip()
    return received_date[:10] if received_date else ""


def get_interview_activity_records(
    db,
    start_date="",
    end_date="",
    filter_team=None,
    filter_expert=None,
    active_experts=None,
    expert_team_map=None,
    statuses=None,
):
    if expert_team_map is None:
        expert_team_map = get_expert_team_map(db)[0]

    directory = get_team_management_directory()
    records = list(
        db.taskBody.find(
            build_interview_activity_match(statuses=statuses),
            {
                "assignedTo": 1,
                "status": 1,
                "subject": 1,
                "receivedDateTime": 1,
                "actualRound": 1,
                "Candidate Name": 1,
                "Date of Interview": 1,
                "_id": 0,
            },
        ).limit(50000)
    )

    normalized_records = []
    for record in records:
        context = resolve_completed_interview_context(
            record.get("assignedTo"),
            expert_team_map,
            directory=directory,
        )
        if not context:
            continue

        expert_key = context["expert_key"]
        team_name = context["team_name"]
        if filter_team and team_name != filter_team:
            continue
        if filter_expert and expert_key != filter_expert:
            continue

        interview_date = get_effective_interview_date(record)
        if start_date or end_date:
            if not interview_date:
                continue
            if start_date and interview_date < start_date:
                continue
            if end_date and interview_date > end_date:
                continue

        normalized_records.append(
            {
                **record,
                "expert_key": expert_key,
                "team_name": team_name,
                "interview_date": interview_date or None,
                "status_bucket": normalize_interview_status_bucket(
                    record.get("status"),
                ),
            }
        )

    return normalized_records


def get_completed_interview_records(
    db,
    start_date="",
    end_date="",
    filter_team=None,
    filter_expert=None,
    active_experts=None,
    expert_team_map=None,
):
    return get_interview_activity_records(
        db,
        start_date=start_date,
        end_date=end_date,
        filter_team=filter_team,
        filter_expert=filter_expert,
        active_experts=active_experts,
        expert_team_map=expert_team_map,
        statuses=["Completed"],
    )


def get_interview_stats_records(
    db,
    start_date="",
    end_date="",
    filter_team=None,
    filter_expert=None,
    active_experts=None,
    expert_team_map=None,
):
    return get_interview_activity_records(
        db,
        start_date=start_date,
        end_date=end_date,
        filter_team=filter_team,
        filter_expert=filter_expert,
        active_experts=active_experts,
        expert_team_map=expert_team_map,
    )


@analytics_bp.route('/interview-records')
def interview_records():
    """
    Interview Records page showing detailed interview records with subjects
    per expert/team with date filtering.
    Uses the interview date parsed from the subject line as the primary filter date.
    """
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    # Get filter parameters
    filter_team = request.args.get('team', '') or None
    filter_expert = normalize_lookup_text(request.args.get('expert', '')) or None

    expert_team_map, teams_map = get_expert_team_map(db)
    teams_list, all_experts, _ = get_interview_stats_filter_options(db)

    cache_key = analytics_cache_key("interview-records-page", start_date, end_date, filter_team, filter_expert)
    cached = current_app.cache.get(cache_key)
    if cached is None:
        records = get_interview_stats_records(
            db,
            start_date=start_date,
            end_date=end_date,
            filter_team=filter_team,
            filter_expert=filter_expert,
            expert_team_map=expert_team_map,
        )

        filtered_teams_map = defaultdict(list)
        expert_records = defaultdict(list)
        all_records = []

        for r in records:
            team_name = r["team_name"]
            expert_key = r["expert_key"]
            if expert_key not in filtered_teams_map[team_name]:
                filtered_teams_map[team_name].append(expert_key)

            subject = r.get('subject', '')
            interview_date = r.get("interview_date")
            display_date = interview_date or 'N/A'
            sort_date = interview_date or ''
            raw_status = clean_text(r.get('status')) or r.get('status_bucket') or 'Not Done'

            expert_records[(team_name, expert_key)].append({
                'subject': subject or 'N/A',
                'candidate': r.get('Candidate Name', 'N/A'),
                'round': r.get('actualRound', 'N/A'),
                'status': raw_status,
                'status_bucket': r.get('status_bucket', 'Not Done'),
                'date': display_date,
                'sort_date': sort_date,
            })

            all_records.append({
                'team': team_name,
                'expert': expert_key,
                'subject': subject or 'N/A',
                'candidate': r.get('Candidate Name', 'N/A'),
                'round': r.get('actualRound', 'N/A'),
                'status': raw_status,
                'status_bucket': r.get('status_bucket', 'Not Done'),
                'date': sort_date,
            })

        if filter_team and filter_team not in filtered_teams_map and filter_team in teams_list:
            filtered_teams_map[filter_team] = []

        team_data = []
        overall_total = 0
        for team_name, members in filtered_teams_map.items():
            expert_list = []
            team_total = 0
            for expert in members:
                recs = expert_records.get((team_name, expert), [])
                recs.sort(key=lambda item: item['sort_date'], reverse=True)
                for item in recs:
                    item.pop('sort_date', None)
                team_total += len(recs)
                expert_list.append({
                    'expert': expert,
                    'count': len(recs),
                    'records': recs
                })

            expert_list.sort(key=lambda x: x['count'], reverse=True)
            overall_total += team_total
            team_data.append({
                'team': team_name,
                'total': team_total,
                'member_count': len(set(teams_map.get(team_name, [])) | set(members)),
                'active_count': len([e for e in expert_list if e['count'] > 0]),
                'experts': expert_list
            })

        team_data.sort(key=lambda x: x['total'], reverse=True)
        all_records.sort(key=lambda item: item['date'], reverse=True)
        teams_map_view = {
            team['team']: sorted(set(teams_map.get(team['team'], [])) | {expert['expert'] for expert in team['experts']})
            for team in team_data
        }
        cached = {
            'team_data': team_data,
            'all_records': all_records,
            'overall_total': overall_total,
            'total_records': len(all_records),
            'teams_map_view': teams_map_view,
        }
        current_app.cache.set(cache_key, cached, timeout=300)

    return render_template(
        'interview_records.html',
        teams=teams_list,
        experts=all_experts,
        teams_map=cached['teams_map_view'],
        team_data=cached['team_data'],
        all_records=cached['all_records'][:500],
        selected_team=filter_team or '',
        selected_expert=filter_expert or '',
        start_date=start_date,
        end_date=end_date,
        overall_total=cached['overall_total'],
        total_records=cached['total_records']
    )


@analytics_bp.route('/export')
def export_center():
    db = get_db()
    start_date, end_date = get_date_filter_strings()

    teams_list, experts, teams_map = get_interview_stats_filter_options(db)
    export_options = get_export_filter_options()

    return render_template(
        'export_center.html',
        teams=teams_list,
        experts=experts,
        technologies=export_options['technologies'],
        workflow_statuses=export_options['workflow_statuses'],
        teams_map=teams_map,
        start_date=start_date,
        end_date=end_date
    )


@analytics_bp.route('/export/preview', methods=['POST'])
def export_preview():
    """Return preview data for the export (first 20 records)."""
    db = get_db()

    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')
    export_type = request.form.get('export_type', 'interview_records')
    filter_team = request.form.get('team', '') or None
    filter_expert = normalize_lookup_text(request.form.get('expert', '')) or None

    # Get active experts
    active_experts = get_active_experts(db)
    expert_team_map, teams_map = get_expert_team_map(db)

    preview_data = []
    total_count = 0
    fields = []

    if export_type == 'interview_records':
        records = get_interview_stats_records(
            db,
            start_date=start_date,
            end_date=end_date,
            filter_team=filter_team,
            filter_expert=filter_expert,
            active_experts=active_experts,
            expert_team_map=expert_team_map,
        )

        for r in records:
            expert_key = r['expert_key']
            team = r['team_name']
            interview_date = r.get('interview_date') or 'N/A'
            preview_data.append({
                'Team': team,
                'Expert': expert_key.split('@')[0] if '@' in expert_key else expert_key,
                'Subject': (r.get('subject', '') or '')[:50] + '...' if len(r.get('subject', '') or '') > 50 else r.get('subject', ''),
                'Round': r.get('actualRound', ''),
                'Date': interview_date,
                'Status': clean_text(r.get('status')) or r.get('status_bucket') or 'Not Done',
            })
        fields = ['Team', 'Expert', 'Subject', 'Round', 'Date', 'Status']

    elif export_type == 'team_summary':
        expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
        for exp in expert_stats:
            preview_data.append({
                'Team': exp.get('team', ''),
                'Expert': exp.get('expert', '').split('@')[0],
                'Interviews': exp.get('interview_count', 0),
                'Screening': exp.get('screening', 0),
                'Final': exp.get('final', 0)
            })
        fields = ['Team', 'Expert', 'Interviews', 'Screening', 'Final']

    elif export_type == 'funnel_combined':
        expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
        for exp in expert_stats:
            preview_data.append({
                'Expert': exp.get('expert', '').split('@')[0],
                'Team': exp.get('team', ''),
                'Screening': exp.get('screening', 0),
                '1st': exp.get('first', 0),
                '2nd': exp.get('second', 0),
                '3rd/Tech': exp.get('third_tech', 0),
                'Loop Round': exp.get('loop_round', 0),
                'Final': exp.get('final', 0),
                'Conv%': exp.get('screening_to_1st', 0)
            })
        fields = ['Expert', 'Team', 'Screening', '1st', '2nd', '3rd/Tech', 'Loop Round', 'Final', 'Conv%']

    elif export_type == 'experts':
        expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
        for exp in expert_stats:
            preview_data.append({
                'Rank': exp.get('rank', 0),
                'Expert': exp.get('expert', '').split('@')[0],
                'Team': exp.get('team', ''),
                'Screening': exp.get('screening', 0),
                '1st': exp.get('first', 0),
                '2nd': exp.get('second', 0),
                '3rd/Tech': exp.get('third_tech', 0),
                'Loop Round': exp.get('loop_round', 0),
                'Final': exp.get('final', 0),
                'Total': exp.get('interview_count', 0),
                'Conv%': exp.get('screening_to_1st', 0)
            })
        fields = ['Rank', 'Expert', 'Team', 'Screening', '1st', '2nd', '3rd/Tech', 'Loop Round', 'Final', 'Total', 'Conv%']

    elif export_type == 'interview_stats':
        # Interview stats: all-status interview counts per expert using the same record rules as the page.
        expert_stats_map = aggregate_interview_stats_by_expert(
            db,
            start_date,
            end_date,
            active_experts=active_experts,
            expert_team_map=expert_team_map,
        )

        for expert_key, stats in sorted(
            expert_stats_map.items(),
            key=lambda item: item[1]['TotalInterviews'],
            reverse=True,
        ):
            team = expert_team_map.get(expert_key, "Unmapped")
            if filter_team and team != filter_team:
                continue
            if filter_expert and expert_key != filter_expert:
                continue

            preview_data.append({
                'Team': team,
                'Expert': expert_key.split('@')[0] if '@' in expert_key else expert_key,
                'Completed': stats.get('CompletedCount', 0),
                'Cancelled': stats.get('CancelledCount', 0),
                'Rescheduled': stats.get('RescheduledCount', 0),
                'Not Done': stats.get('NotDoneCount', 0),
                'Total': stats.get('TotalInterviews', 0),
            })
        fields = ['Team', 'Expert', 'Completed', 'Cancelled', 'Rescheduled', 'Not Done', 'Total']

    total_count = len(preview_data)
    preview_data = preview_data[:20]  # Return only first 20 for preview

    return jsonify({
        'success': True,
        'data': preview_data,
        'total_count': total_count,
        'fields': fields,
        'field_count': len(fields)
    })


@analytics_bp.route('/export/download', methods=['POST'])
def export_download():
    """Handle export downloads with Excel, CSV, and JSON support."""
    db = get_db()

    start_date = request.form.get('start_date', '')
    end_date = request.form.get('end_date', '')
    export_type = request.form.get('export_type', 'experts')
    export_format = request.form.get('format', 'excel')
    filter_team = request.form.get('team', '') or None
    filter_expert = normalize_lookup_text(request.form.get('expert', '')) or None

    # Get active experts
    active_experts = get_active_experts(db)
    expert_team_map, teams_map = get_expert_team_map(db)

    if export_type == 'interview_records':
        return export_interview_records_excel(db, start_date, end_date, filter_team, filter_expert,
                                             active_experts, expert_team_map, teams_map, export_format)

    elif export_type == 'team_summary':
        return export_team_summary_excel(db, start_date, end_date, filter_team, filter_expert,
                                        active_experts, expert_team_map, teams_map, export_format)

    elif export_type == 'funnel_combined':
        return export_funnel_combined_excel(db, start_date, end_date, filter_team, filter_expert, export_format)

    elif export_type == 'experts':
        expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
        return export_experts_excel(expert_stats, start_date, end_date, export_format)

    elif export_type == 'teams':
        team_stats, _ = get_team_funnel_data(db, start_date, end_date, filter_team, filter_expert)
        return export_teams_excel(team_stats, start_date, end_date, export_format)

    elif export_type == 'interview_stats':
        return export_interview_stats_excel(db, start_date, end_date, filter_team, filter_expert,
                                           active_experts, expert_team_map, export_format)

    else:
        return jsonify({'success': False, 'error': 'Invalid export type'})


def export_interview_records_excel(db, start_date, end_date, filter_team, filter_expert,
                                   active_experts, expert_team_map, teams_map, export_format='excel'):
    """
    EXPORT TYPE 1: Interview Records
    Columns: Team, Expert, Subject, Candidate, Round, InterviewDate, Status
    """
    records = get_interview_stats_records(
        db,
        start_date=start_date,
        end_date=end_date,
        filter_team=filter_team,
        filter_expert=filter_expert,
        active_experts=active_experts,
        expert_team_map=expert_team_map,
    )

    excel_rows = []
    for r in records:
        expert_key = r['expert_key']
        team = r['team_name']

        excel_rows.append({
            'Team': team,
            'Expert': expert_key,
            'Subject': r.get('subject', ''),
            'Candidate': r.get('Candidate Name', ''),
            'Round': r.get('actualRound', ''),
            'InterviewDate': r.get('interview_date') or '',
            'ReceivedDateTime': r.get('receivedDateTime', ''),
            'Status': clean_text(r.get('status')) or r.get('status_bucket') or 'Not Done',
        })

    if not excel_rows:
        return jsonify({'success': False, 'error': 'No records found for the given filters'})

    # Create DataFrame and sort
    df = pd.DataFrame(excel_rows)
    df = df.sort_values(by=["Team", "Expert", "InterviewDate", "ReceivedDateTime"], ascending=[True, True, True, True])

    # Generate filename base
    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"interviews_{start_str}_to_{end_str}.csv"
        )
    else:
        # Excel format (default)
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Interview_Records', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"interviews_{start_str}_to_{end_str}.xlsx"
        )


def export_team_summary_excel(db, start_date, end_date, filter_team, filter_expert,
                              active_experts, expert_team_map, teams_map, export_format='excel'):
    """
    EXPORT TYPE 2: Team Summary
    Columns: Team, Expert, Total_Interviews, Completed, Cancelled, Rescheduled
    """
    # Build query
    match_filters = build_task_query(start_date, end_date)
    match_filters["status"] = {"$in": ["Completed", "Cancelled", "Rescheduled"]}

    # Get interview stats by expert
    pipeline = [
        {"$match": match_filters},
        {
            "$group": {
                "_id": {
                    "expert": "$assignedTo",
                    "status": "$status"
                },
                "count": {"$sum": 1}
            }
        }
    ]

    results = list(db.taskBody.aggregate(pipeline))

    # Process results
    expert_stats = {}
    for r in results:
        expert = r['_id']['expert']
        expert_key = normalize_lookup_text(expert)
        status = r['_id']['status']
        count = r['count']

        # Skip inactive experts
        if expert_key not in active_experts:
            continue

        if expert_key not in expert_stats:
            expert_stats[expert_key] = {'Completed': 0, 'Cancelled': 0, 'Rescheduled': 0}

        expert_stats[expert_key][status] += count

    excel_rows = []
    for expert, stats in expert_stats.items():
        team = expert_team_map.get(expert, "Unmapped")

        # Apply filters
        if filter_team and team != filter_team:
            continue
        if filter_expert and expert != filter_expert:
            continue

        total = stats['Completed'] + stats['Cancelled'] + stats['Rescheduled']

        excel_rows.append({
            'Team': team,
            'Expert': expert,
            'Total_Interviews': total,
            'Completed': stats['Completed'],
            'Cancelled': stats['Cancelled'],
            'Rescheduled': stats['Rescheduled']
        })

    if not excel_rows:
        return jsonify({'success': False, 'error': 'No data to export for the given filters'})

    # Create DataFrame and sort
    df = pd.DataFrame(excel_rows)
    df = df.sort_values(by=["Team", "Expert"], ascending=[True, True])

    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"team_summary_{start_str}_to_{end_str}.csv"
        )
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Team_Summary', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"team_summary_{start_str}_to_{end_str}.xlsx"
        )


def export_funnel_combined_excel(db, start_date, end_date, filter_team, filter_expert, export_format='excel'):
    """
    EXPORT TYPE 3 & 4: Expert & Team Funnel Combined
    Two sheets: Expert_Funnel and Team_Funnel
    """
    # Get expert and team funnel data
    expert_stats, _ = get_expert_funnel_data(db, start_date, end_date, filter_team, filter_expert)
    team_stats, _ = get_team_funnel_data(db, start_date, end_date, filter_team, filter_expert)

    # Create expert DataFrame
    expert_rows = []
    for exp in expert_stats:
        expert_rows.append({
            'Rank': exp.get('rank', 0),
            'Expert': exp.get('expert', ''),
            'Team': exp.get('team', ''),
            'Screening': exp.get('screening', 0),
            '1st': exp.get('first', 0),
            '2nd': exp.get('second', 0),
            '3rd/Technical': exp.get('third_tech', 0),
            'Loop Round': exp.get('loop_round', 0),
            'Final': exp.get('final', 0),
            'Total_Interviews': exp.get('interview_count', 0),
            'Screening_to_1st_%': exp.get('screening_to_1st', 0),
            '1st_to_2nd_%': exp.get('first_to_2nd', 0),
            '2nd_to_3rd_%': exp.get('second_to_3rd', 0),
            '3rd_to_Loop_%': exp.get('third_to_loop', 0),
            'Loop_to_Final_%': exp.get('loop_to_final', 0),
        })

    # Create team DataFrame
    team_rows = []
    for team in team_stats:
        team_rows.append({
            'Rank': team.get('rank', 0),
            'Team': team.get('team', ''),
            'Members': team.get('member_count', 0),
            'Screening': team.get('screening', 0),
            '1st': team.get('first', 0),
            '2nd': team.get('second', 0),
            '3rd/Technical': team.get('third_tech', 0),
            'Loop Round': team.get('loop_round', 0),
            'Final': team.get('final', 0),
            'Total_Interviews': team.get('interview_count', 0),
            'Screening_to_1st_%': team.get('screening_to_1st', 0),
            '1st_to_2nd_%': team.get('first_to_2nd', 0),
            '2nd_to_3rd_%': team.get('second_to_3rd', 0),
            '3rd_to_Loop_%': team.get('third_to_loop', 0),
            'Loop_to_Final_%': team.get('loop_to_final', 0),
        })

    if not expert_rows and not team_rows:
        return jsonify({'success': False, 'error': 'No funnel data available'})

    expert_df = pd.DataFrame(expert_rows)
    team_df = pd.DataFrame(team_rows)

    if export_format == 'csv':
        # For CSV, combine both dataframes with a separator
        output = BytesIO()
        if not expert_df.empty:
            expert_df.to_csv(output, index=False)
            output.write(b'\n\n--- Team Funnel ---\n\n')
        if not team_df.empty:
            team_df.to_csv(output, index=False, header=True)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name="expert_team_conversion_funnel.csv"
        )
    else:
        # Create Excel file with multiple sheets
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            if not expert_df.empty:
                expert_df.to_excel(writer, sheet_name='Expert_Funnel', index=False)
            if not team_df.empty:
                team_df.to_excel(writer, sheet_name='Team_Funnel', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name="expert_team_conversion_funnel.xlsx"
        )


def export_candidate_analytics_excel(candidate_stats, client_data, start_date, end_date, export_format='excel'):
    rows = []
    po_state = client_data.get("state")
    for candidate in candidate_stats:
        row = {
            'Rank': candidate.get('rank', 0),
            'Candidate': candidate.get('candidate', ''),
            'Screening': candidate.get('screening', 0),
            'Scr to 1st %': min(candidate.get('screening_to_1st', 0), 100),
            '1st Round': candidate.get('first', 0),
            '1st to 2nd %': min(candidate.get('first_to_2nd', 0), 100),
            '2nd Round': candidate.get('second', 0),
            '2nd to 3rd %': min(candidate.get('second_to_3rd', 0), 100),
            '3rd/Technical': candidate.get('third_tech', 0),
            '3rd to Loop %': min(candidate.get('third_to_loop', 0), 100),
            'Loop Round': candidate.get('loop_round', 0),
            'Loop to Final %': min(candidate.get('loop_to_final', 0), 100),
            'Final': candidate.get('final', 0),
            'Total Interviews': candidate.get('interview_count', 0),
        }
        if po_state == 'ready':
            row['Unique Clients'] = candidate.get('unique_clients') or 0
            row['POs'] = candidate.get('po_count') or 0
            row['Client Names'] = ", ".join(candidate.get('client_names') or [])
        rows.append(row)

    if not rows:
        return jsonify({'success': False, 'error': 'No candidate analytics rows found for the given filters'})

    df = pd.DataFrame(rows)
    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"candidate_analytics_{start_str}_to_{end_str}.csv"
        )

    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, sheet_name='Candidates', index=False)
    output.seek(0)
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=f"candidate_analytics_{start_str}_to_{end_str}.xlsx"
    )


def export_experts_excel(expert_stats, start_date, end_date, export_format='excel'):
    """Simple expert funnel export."""
    rows = []
    for exp in expert_stats:
        rows.append({
            'Rank': exp.get('rank', 0),
            'Expert': exp.get('expert', ''),
            'Team': exp.get('team', ''),
            'Screening': exp.get('screening', 0),
            '1st': exp.get('first', 0),
            '2nd': exp.get('second', 0),
            '3rd/Technical': exp.get('third_tech', 0),
            'Loop Round': exp.get('loop_round', 0),
            'Final': exp.get('final', 0),
            'Total': exp.get('interview_count', 0),
            'Conversion_%': exp.get('screening_to_1st', 0)
        })

    df = pd.DataFrame(rows)
    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"expert_analytics_{start_str}_to_{end_str}.csv"
        )
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Experts', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"expert_analytics_{start_str}_to_{end_str}.xlsx"
        )


def export_teams_excel(team_stats, start_date, end_date, export_format='excel'):
    """Simple team funnel export."""
    rows = []
    for team in team_stats:
        rows.append({
            'Rank': team.get('rank', 0),
            'Team': team.get('team', ''),
            'Members': team.get('member_count', 0),
            'Screening': team.get('screening', 0),
            '1st': team.get('first', 0),
            '2nd': team.get('second', 0),
            '3rd/Technical': team.get('third_tech', 0),
            'Loop Round': team.get('loop_round', 0),
            'Final': team.get('final', 0),
            'Total': team.get('interview_count', 0),
            'Conversion_%': team.get('screening_to_1st', 0)
        })

    df = pd.DataFrame(rows)
    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"team_analytics_{start_str}_to_{end_str}.csv"
        )
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Teams', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"team_analytics_{start_str}_to_{end_str}.xlsx"
        )


def export_interview_stats_excel(db, start_date, end_date, filter_team, filter_expert,
                                 active_experts, expert_team_map, export_format='excel'):
    """
    Export interview stats using the same all-status rules as the page.
    """
    expert_stats_map = aggregate_interview_stats_by_expert(
        db,
        start_date,
        end_date,
        active_experts=active_experts,
        expert_team_map=expert_team_map,
    )

    merged_rows = {}
    for expert_key, stats in expert_stats_map.items():
        team = expert_team_map.get(expert_key, "Unmapped")
        if filter_team and team != filter_team:
            continue
        if filter_expert and expert_key != filter_expert:
            continue
        merged_rows.setdefault(
            expert_key,
            {
                'Team': team,
                'Expert': expert_key,
                'Completed': 0,
                'Cancelled': 0,
                'Rescheduled': 0,
                'Not Done': 0,
                'Total': 0,
            },
        )
        merged_rows[expert_key]['Completed'] += stats.get('CompletedCount', 0)
        merged_rows[expert_key]['Cancelled'] += stats.get('CancelledCount', 0)
        merged_rows[expert_key]['Rescheduled'] += stats.get('RescheduledCount', 0)
        merged_rows[expert_key]['Not Done'] += stats.get('NotDoneCount', 0)
        merged_rows[expert_key]['Total'] += stats.get('TotalInterviews', 0)

    excel_rows = list(merged_rows.values())

    if not excel_rows:
        return jsonify({'success': False, 'error': 'No data to export for the given filters'})

    df = pd.DataFrame(excel_rows)
    df = df.sort_values(by=["Team", "Expert"], ascending=[True, True])

    start_str = start_date[:10] if start_date else 'all'
    end_str = end_date[:10] if end_date else 'all'

    if export_format == 'csv':
        output = BytesIO()
        df.to_csv(output, index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='text/csv',
            as_attachment=True,
            download_name=f"interview_stats_{start_str}_to_{end_str}.csv"
        )
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Interview_Stats', index=False)
        output.seek(0)
        return send_file(
            output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name=f"interview_stats_{start_str}_to_{end_str}.xlsx"
        )
