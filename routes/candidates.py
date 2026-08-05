from flask import Blueprint, render_template, request, redirect, url_for, current_app

from db import get_db
from services.reference_data import get_candidate_lookup_names
from services.team_management import mongo_normalized_text, normalize_lookup_text

candidates_bp = Blueprint('candidates', __name__)


@candidates_bp.route('/', methods=['GET'])
def search():
    query_name = request.args.get('q', '')
    results = []

    if query_name:
        db = get_db()
        mongo_query = {
            "Candidate Name": {"$regex": query_name, "$options": 'i'}
        }
        cursor = db.candidateDetails.find(
            mongo_query,
            {
                "Candidate Name": 1,
                "workflowStatus": 1,
                "Technology": 1,
                "_id": 1
            }
        ).limit(50)
        results = list(cursor)

    return render_template('search.html', query=query_name, results=results)


@candidates_bp.route('/lookup', methods=['GET'])
def candidate_lookup():
    """
    Candidate interview lookup - shows detailed interview statistics
    for a specific candidate from the taskBody collection.
    """
    db = get_db()
    candidate_name = request.args.get('name', '').strip()
    candidate_name_key = normalize_lookup_text(candidate_name)

    # Get list of all unique candidate names for autocomplete/dropdown - OPTIMIZED with limit
    all_candidates = get_candidate_lookup_names(limit=500)

    candidate_data = None
    interview_records = []

    if candidate_name_key:
        cache_key = f"candidate-lookup:summary:{candidate_name_key}"
        cache = getattr(current_app, "cache", None)
        cached = cache.get(cache_key) if cache else None

        if cached is None:
            pipeline = [
                {
                    "$match": {
                        "$expr": {
                            "$eq": [
                                mongo_normalized_text("Candidate Name"),
                                candidate_name_key,
                            ]
                        }
                    }
                },
                {
                    "$facet": {
                        "totalInterviews": [
                            {"$count": "count"}
                        ],
                        "byRound": [
                            {"$group": {"_id": "$actualRound", "count": {"$sum": 1}}},
                            {"$sort": {"count": -1}}
                        ],
                        "byStatus": [
                            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                            {"$sort": {"count": -1}}
                        ],
                        "byExpert": [
                            {"$group": {"_id": "$assignedTo", "count": {"$sum": 1}}},
                            {"$sort": {"count": -1}}
                        ],
                        "timeline": [
                            {"$sort": {"receivedDateTime": -1}},
                            {"$limit": 50},
                            {"$project": {
                                "subject": 1,
                                "actualRound": 1,
                                "status": 1,
                                "assignedTo": 1,
                                "receivedDateTime": 1,
                                "scheduledDateTime": 1
                            }}
                        ]
                    }
                }
            ]

            results = list(db.taskBody.aggregate(pipeline))

            payload = {
                "candidate_data": None,
                "interview_records": [],
            }

            if results:
                data = results[0]
                total = data["totalInterviews"][0]["count"] if data["totalInterviews"] else 0

                by_round = []
                for r in data["byRound"]:
                    round_name = r['_id'] if r['_id'] else 'Unknown'
                    by_round.append({'round': round_name, 'count': r['count']})

                by_status = []
                completed_count = 0
                cancelled_count = 0
                rescheduled_count = 0
                for s in data["byStatus"]:
                    status_name = s['_id'] if s['_id'] else 'Unknown'
                    by_status.append({'status': status_name, 'count': s['count']})
                    if status_name == 'Completed':
                        completed_count = s['count']
                    elif status_name == 'Cancelled':
                        cancelled_count = s['count']
                    elif status_name == 'Rescheduled':
                        rescheduled_count = s['count']

                by_expert = []
                for e in data["byExpert"]:
                    expert_name = e['_id'] if e['_id'] else 'Unknown'
                    by_expert.append({'expert': expert_name, 'count': e['count']})

                interview_records = data["timeline"]
                completion_rate = round((completed_count / total) * 100, 1) if total > 0 else 0

                payload = {
                    "candidate_data": {
                        'name': candidate_name,
                        'total_interviews': total,
                        'completed_count': completed_count,
                        'cancelled_count': cancelled_count,
                        'rescheduled_count': rescheduled_count,
                        'completion_rate': completion_rate,
                        'by_round': by_round,
                        'by_status': by_status,
                        'by_expert': by_expert,
                        'unique_rounds': len(by_round),
                        'unique_experts': len(by_expert)
                    },
                    "interview_records": interview_records,
                }

            if cache:
                cache.set(cache_key, payload, timeout=300)
            cached = payload

        candidate_data = cached["candidate_data"]
        interview_records = cached["interview_records"]

    return render_template(
        'candidate_lookup.html',
        all_candidates=all_candidates,
        candidate_name=candidate_name,
        candidate_data=candidate_data,
        interview_records=interview_records
    )


@candidates_bp.route('/active', methods=['GET'])
def active_candidates():
    return redirect(url_for('dashboard.index'))
