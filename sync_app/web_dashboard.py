"""Flask web dashboard for monitoring and controlling sync operations."""

import logging
import threading
from functools import wraps

from flask import Flask, jsonify, redirect, request, session, url_for

from sync_app.config import Config
from sync_app.failure_manager import FailureManager
from sync_app.models import SyncState

logger = logging.getLogger("github_sync")

with open("dashboard.html", "r") as _fh:
    DASHBOARD_HTML = _fh.read()

with open("login.html", "r") as _fh:
    LOGIN_HTML = _fh.read()

def _login_required(f):
    """Decorator: require authenticated session. Redirects to /login or returns 401 for API."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            if request.path.startswith("/api/"):
                return jsonify({"ok": False, "message": "Unauthorized"}), 401
            return redirect(url_for("login_page"))
        return f(*args, **kwargs)
    return decorated


def _maybe_protect(app, auth_enabled: bool):
    """Apply login_required decorator to routes if auth is enabled."""
    if not auth_enabled:
        return

    # Re-register protected routes by wrapping each endpoint
    for rule in list(app.url_map.iter_rules()):
        if rule.endpoint in ("login_page", "api_login", "api_logout", "static"):
            continue
        view = app.view_functions.get(rule.endpoint)
        if view:
            app.view_functions[rule.endpoint] = _login_required(view)


def create_app(config: Config, sync_state: SyncState, failure_manager: FailureManager, sync_trigger: callable) -> Flask:
    """Build the Flask dashboard application."""
    app = Flask(__name__)
    app.secret_key = config.auth_secret_key

    flask_log = logging.getLogger("werkzeug")
    flask_log.setLevel(logging.WARNING)

    # ------------------------------------------------------------------
    # Auth routes (always available)
    # ------------------------------------------------------------------

    @app.route("/login")
    def login_page():
        if session.get("authenticated"):
            return redirect(url_for("index"))
        return LOGIN_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    @app.route("/api/auth/login", methods=["POST"])
    def api_login():
        if not config.auth_enabled:
            session["authenticated"] = True
            return jsonify({"ok": True, "message": "Auth disabled, logged in automatically"})

        data = request.get_json(silent=True) or {}
        req_user = data.get("username", "")
        req_pass = data.get("password", "")

        if req_user == config.auth_username and req_pass == config.auth_password:
            session["authenticated"] = True
            session["username"] = req_user
            logger.info("Dashboard login successful for user: %s", req_user)
            return jsonify({"ok": True, "message": "Login successful"})

        logger.warning("Failed dashboard login attempt for user: %s", req_user)
        return jsonify({"ok": False, "message": "Invalid username or password"}), 401

    @app.route("/api/auth/logout", methods=["POST"])
    def api_logout():
        session.clear()
        return jsonify({"ok": True, "message": "Logged out"})

    @app.route("/api/auth/status")
    def api_auth_status():
        return jsonify({
            "authenticated": session.get("authenticated", False),
            "auth_enabled": config.auth_enabled,
        })

    # ------------------------------------------------------------------
    # Protected routes
    # ------------------------------------------------------------------
    # Build view functions first, then optionally wrap them below.

    def _index():
        return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}

    def _api_status():
        return jsonify(sync_state.to_dict())

    def _api_start():
        if sync_state.is_running:
            return jsonify({"ok": False, "message": "Sync is already running"}), 409
        sync_trigger()
        return jsonify({"ok": True, "message": "Sync started"})

    def _api_stop():
        if not sync_state.is_running:
            return jsonify({"ok": False, "message": "No sync is running"}), 409
        sync_state.stop_requested = True
        logger.info("Stop requested via dashboard")
        return jsonify({"ok": True, "message": "Stop signal sent"})

    def _api_failures():
        return jsonify(failure_manager.get_all_failures())

    def _api_retry_failures():
        if sync_state.is_running:
            return jsonify({"ok": False, "message": "Cannot retry while sync is running"}), 409
        count = len(failure_manager.get_all_failures())
        failure_manager.clear_all_failures()
        logger.info("Failure records cleared. %d record(s) will be retried on next sync.", count)
        return jsonify({"ok": True, "message": f"Cleared {count} failure record(s). Next sync will retry these files."})

    # Register routes
    app.add_url_rule("/", "index", _index)
    app.add_url_rule("/api/status", "api_status", _api_status)
    app.add_url_rule("/api/sync/start", "api_start", _api_start, methods=["POST"])
    app.add_url_rule("/api/sync/stop", "api_stop", _api_stop, methods=["POST"])
    app.add_url_rule("/api/failures", "api_failures", _api_failures)
    app.add_url_rule("/api/failures/retry", "api_retry_failures", _api_retry_failures, methods=["POST"])

    # Apply auth protection if enabled
    if config.auth_enabled:
        for name in ("index", "api_status", "api_start", "api_stop", "api_failures", "api_retry_failures"):
            app.view_functions[name] = _login_required(app.view_functions[name])

    return app


class DashboardRunner:
    """Runs the Flask dashboard in a daemon thread."""

    def __init__(
        self,
        config: Config,
        sync_state: SyncState,
        failure_manager: FailureManager,
        sync_trigger: callable,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.config = config
        self.app = create_app(config, sync_state, failure_manager, sync_trigger)
        self.host = host
        self.port = port
        self._thread: threading.Thread | None = None

    def start(self):
        """Start the dashboard in a daemon thread."""
        self._thread = threading.Thread(target=self._run, daemon=True, name="dashboard")
        self._thread.start()
        auth_status = "with auth" if self.config.auth_enabled else "no auth"
        logger.info("Dashboard started at http://%s:%d (%s)", self.host, self.port, auth_status)

    def _run(self):
        try:
            self.app.run(host=self.host, port=self.port, debug=False, use_reloader=False)
        except Exception as e:
            logger.error("Dashboard server error: %s", e)
