from flask import Blueprint, render_template, request, redirect, url_for

from db import get_db

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


@candidates_bp.route('/active', methods=['GET'])
def active_candidates():
    return redirect(url_for('dashboard.index'))
