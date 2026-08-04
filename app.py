import os
from dotenv import load_dotenv

# Load environment variables from .env file BEFORE importing anything else
load_dotenv()

from flask import Flask, render_template, jsonify, request
from flask_caching import Cache
from routes.dashboard import dashboard_bp
from routes.candidates import candidates_bp
from routes.analytics import analytics_bp
from routes.kpi import kpi_bp
from routes.po import po_bp
from services.po_consumer import start_po_consumer
from services.startup_warmup import start_startup_warmup

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "mega-dashboard-dev-secret-key")
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 86400

# Configure caching for performance optimization
cache = Cache(
    app,
    config={
        "CACHE_TYPE": "SimpleCache",  # In-memory cache for single-worker deployments
        "CACHE_DEFAULT_TIMEOUT": 300,  # 5 minutes default cache timeout
        "CACHE_THRESHOLD": 2000,  # Keep more warm page/query results in memory
    },
)

# Make cache available to blueprints
app.cache = cache

# Register Blueprints
app.register_blueprint(dashboard_bp)
app.register_blueprint(candidates_bp, url_prefix="/candidates")
app.register_blueprint(analytics_bp, url_prefix="/analytics")
app.register_blueprint(kpi_bp, url_prefix="/kpi")
# PO blueprint kept registered but unlinked from the UI (no nav tab / cards / columns).
# Reachable only by direct URL and still PIN-gated. Remove entirely for a full teardown.
app.register_blueprint(po_bp, url_prefix="/po")

# Start the optional PO Kafka consumer in persistent Flask runtimes.
start_po_consumer()
start_startup_warmup(app)


# Health check endpoint
@app.route("/health")
def health_check():
    """Health check endpoint for monitoring."""
    import sys

    try:
        from db import get_db

        db = get_db()
        # Quick ping to verify DB connection
        result = db.command("ping")
        return jsonify(
            {
                "status": "healthy",
                "database": "connected",
                "python_version": sys.version,
                "ping_response": result,
            }
        ), 200
    except Exception as e:
        import traceback

        return jsonify(
            {
                "status": "unhealthy",
                "error": str(e),
                "error_type": type(e).__name__,
                "traceback": traceback.format_exc(),
                "python_version": sys.version,
            }
        ), 500


# Error handlers
@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors gracefully."""
    return render_template(
        "error.html",
        error_code=500,
        error_message="Internal server error. Please contact support.",
    ), 500


@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors."""
    return render_template(
        "error.html", error_code=404, error_message="Page not found."
    ), 404


# For local development
if __name__ == "__main__":
    debug = os.getenv("FLASK_DEBUG", "True").lower() == "true"
    port = int(os.getenv("FLASK_PORT", 5000))
    app.run(debug=debug, port=port)

# Vercel serverless handler
application = app


@app.after_request
def apply_cache_headers(response):
    cache_control = response.cache_control

    if response.status_code != 200:
        cache_control.no_store = True
        return response

    if request.path.startswith("/static/"):
        cache_control.public = True
        cache_control.max_age = 86400
        cache_control.immutable = False
    elif response.mimetype == "text/html":
        cache_control.no_store = True

    return response
