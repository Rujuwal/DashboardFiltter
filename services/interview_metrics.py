"""
Shared interview classification, matching the Daily Conversion Brief / PO report exactly.

Rules (agreed 2026-07-02):
  * Round category comes from ``actualRound`` (recruiter-verified).
  * "On Demand / AI Interview" rounds are EXCLUDED entirely.
  * "Screening" is split out and excluded from the interview count.
  * Loop rounds for the SAME end client count once (deduped by candidate + end-client key),
    using the same ``client_key`` normalization the daily report uses.
  * Every page filters the month by the real interview date (``Date of Interview``,
    MM/DD/YYYY) -- NOT by ``receivedDateTime`` (the email-received timestamp).

This module is intentionally free of Flask imports so it can be unit-verified against
MongoDB on its own.
"""

import re
from collections import defaultdict
from datetime import date

# Rounds that never count as an interview or a screening (asynchronous / automated).
AI_ROUND_TOKENS = ("demand", "ai interview")


def norm(s):
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def client_key(s):
    """End-client normalization -- ported verbatim from the daily report's clientKey()."""
    n = re.sub(r"\s+", " ", re.sub(r"[&.,()/+\-]", " ", norm(s))).strip()
    compact = n.replace(" ", "")
    if compact in ("jpmc", "jpmorganchase", "jpmorgan", "jpmorganbank", "chase", "jpmchase") \
            or "jp morgan chase" in n or "jpmorgan chase" in n:
        return "jpmc"
    if compact in ("dbtlabs", "dbt"):
        return "dbt labs"
    if compact in ("bny", "bnymellon", "bankofnewyorkmellon"):
        return "bny"
    if compact in ("statestreet",) or "charles river development" in n:
        return "state street"
    if compact in ("travelersinsurance", "thetravelerscompanies"):
        return "travelers"
    if compact in ("wavicledatasolution", "wahicledatasolution", "wavicledata", "wahicledata"):
        return "wavicle data solution"
    return n


def candidate_key(s):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", norm(s))).strip()


def bucket_of(actual_round):
    """Map a raw ``actualRound`` string to a funnel bucket. Mirrors the report's bucketOf()."""
    s = str(actual_round or "").strip()
    if len(s) < 3:
        return "uncat"
    l = s.lower()
    if "screen" in l:
        return "screen"
    if "loop" in l:
        return "loop"
    if "final" in l:
        return "final"
    if any(tok in l for tok in AI_ROUND_TOKENS):
        return "ai"  # excluded
    if "coding" in l or "technical" in l:
        return "tech"
    if re.search(r"3rd|third|4th|fourth", l):
        return "third"
    if re.search(r"2nd|second", l):
        return "second"
    if re.search(r"1st|first", l):
        return "first"
    return "other"


# Buckets that count as a "real" interview (1 row = 1), before loop dedup is added.
REGULAR_BUCKETS = ("first", "second", "third", "tech", "final")

# actualRound values whose rows are dropped from every count (AI/On-Demand).
EXCLUDED_ACTUAL_ROUNDS = ["On demand", "On Demand", "On Demand or AI Interview"]


def parse_interview_date(value):
    """Parse a ``Date of Interview`` string (MM/DD/YYYY) into a ``date``; None if unparseable."""
    m = re.match(r"^\s*(\d{1,2})/(\d{1,2})/(\d{4})\s*$", str(value or ""))
    if not m:
        return None
    month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _range_bounds(start_date, end_date):
    start = None
    end = None
    if start_date:
        try:
            y, m, d = (int(p) for p in str(start_date)[:10].split("-"))
            start = date(y, m, d)
        except (ValueError, TypeError):
            start = None
    if end_date:
        try:
            y, m, d = (int(p) for p in str(end_date)[:10].split("-"))
            end = date(y, m, d)
        except (ValueError, TypeError):
            end = None
    return start, end


def _year_regex(start, end):
    """A cheap Mongo prefilter on ``Date of Interview`` covering the years in range."""
    years = set()
    if start:
        years.add(start.year)
    if end:
        years.add(end.year)
    if start and end:
        years.update(range(start.year, end.year + 1))
    if not years:
        return {"$regex": r"^\d{1,2}/\d{1,2}/\d{4}$"}
    alt = "|".join(str(y) for y in sorted(years))
    return {"$regex": r"^\d{1,2}/\d{1,2}/(" + alt + r")$"}


def build_interview_date_match(start_date="", end_date=""):
    """
    A Mongo match (usable in find/aggregate) that filters on the real interview date
    (``Date of Interview``, MM/DD/YYYY) instead of ``receivedDateTime``.

    Uses $expr + $dateFromString so callers keep a plain match dict. Rows whose
    ``Date of Interview`` is missing/unparseable are excluded when a range is given.
    """
    start, end = _range_bounds(start_date, end_date)
    if not start and not end:
        return {}

    parsed = {
        "$dateFromString": {
            "dateString": "$Date of Interview",
            "format": "%m/%d/%Y",
            "onError": None,
            "onNull": None,
        }
    }
    conds = [{"$ne": [parsed, None]}]
    if start:
        conds.append({"$gte": [parsed, {"$dateFromString": {"dateString": start.isoformat(), "format": "%Y-%m-%d"}}]})
    if end:
        conds.append({"$lte": [parsed, {"$dateFromString": {"dateString": end.isoformat(), "format": "%Y-%m-%d"}}]})
    return {"$expr": {"$and": conds}}


def fetch_completed_interviews(db, start_date="", end_date="", extra_match=None):
    """
    Return completed taskBody rows within the interview-date range, each annotated with
    ``bucket``, ``candidate_key`` and ``client_key``. Filtering is on ``Date of Interview``.
    """
    start, end = _range_bounds(start_date, end_date)
    query = {
        "status": "Completed",
        "assignedTo": {"$type": "string", "$ne": ""},
        "Date of Interview": _year_regex(start, end),
    }
    if extra_match:
        query.update(extra_match)

    rows = []
    cursor = db.taskBody.find(
        query,
        {
            "_id": 0,
            "assignedTo": 1,
            "actualRound": 1,
            "Candidate Name": 1,
            "End Client": 1,
            "Date of Interview": 1,
        },
    )
    for doc in cursor:
        interview_date = parse_interview_date(doc.get("Date of Interview"))
        if start and (interview_date is None or interview_date < start):
            continue
        if end and (interview_date is None or interview_date > end):
            continue
        doc["bucket"] = bucket_of(doc.get("actualRound"))
        doc["candidate_key"] = candidate_key(doc.get("Candidate Name"))
        doc["client_key"] = client_key(doc.get("End Client"))
        doc["interview_date"] = interview_date.isoformat() if interview_date else None
        rows.append(doc)
    return rows


def blank_stage_counts():
    return {
        "Screening": 0,
        "1st": 0,
        "2nd": 0,
        "3rd/Technical": 0,
        "Loop Round": 0,
        "Final": 0,
    }


def stage_counts_by_group(records, key_getter):
    """
    Aggregate classified records into per-group funnel-stage counts, deduping loop rounds
    by (candidate_key, client_key) within each group.

    Returns {group_key: stage_counts_dict}. Stage names align with build_funnel_metrics().
    """
    stages = defaultdict(blank_stage_counts)
    loop_sets = defaultdict(set)

    for r in records:
        key = key_getter(r)
        if not key:
            continue
        bucket = r.get("bucket")
        if bucket == "screen":
            stages[key]["Screening"] += 1
        elif bucket == "first":
            stages[key]["1st"] += 1
        elif bucket == "second":
            stages[key]["2nd"] += 1
        elif bucket in ("third", "tech"):
            stages[key]["3rd/Technical"] += 1
        elif bucket == "final":
            stages[key]["Final"] += 1
        elif bucket == "loop":
            loop_sets[key].add((r.get("candidate_key", ""), r.get("client_key", "")))
        # "ai", "other", "uncat" -> excluded

    for key, loops in loop_sets.items():
        stages[key]["Loop Round"] = len(loops)

    # Ensure groups that only had loops still exist.
    for key in loop_sets:
        stages.setdefault(key, blank_stage_counts())

    return {key: dict(counts) for key, counts in stages.items()}
