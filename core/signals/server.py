import logging
import os
from threading import Thread
from typing import Optional

from core.signals.adapter import ingest_signal

try:  # pragma: no cover - import guard
    from flask import Flask, request, jsonify
except ImportError:  # pragma: no cover - import guard
    Flask = None  # type: ignore[assignment]
    request = None  # type: ignore[assignment]
    jsonify = None  # type: ignore[assignment]
    HAVE_FLASK = False
else:
    HAVE_FLASK = True

logger = logging.getLogger(__name__)

_server_thread: Optional[Thread] = None


def _create_app():
    if not HAVE_FLASK:
        raise RuntimeError("Flask is required to create the signals server application")

    app = Flask(__name__)

    @app.get("/healthz")
    def _healthcheck():
        return {"ok": True}

    @app.post("/signal")
    def _signal():
        try:
            data = request.get_json(force=True, silent=True) or {}
            out = dispatch(data)
            return jsonify({"ok": bool(out), "action": (out or {}).get("guard_action")})
        except Exception as exc:  # pragma: no cover - safety guard
            return jsonify({"ok": False, "error": str(exc)}), 400

    return app


def start_server():
    """Start the HTTP signals server if Flask is available."""

    global _server_thread

    if not HAVE_FLASK:
        logger.warning("Flask is not installed; signals server will not start.")
        return None

    if _server_thread and _server_thread.is_alive():
        return _server_thread

    bind = os.getenv("SIGNALS_BIND", "0.0.0.0:8080")
    host, port = (bind.split(":", 1) + ["8080"])[:2]
    app = _create_app()

    def _run():
        app.run(host=host, port=int(port), debug=False, use_reloader=False)

    thread = Thread(target=_run, daemon=True)
    thread.start()
    _server_thread = thread
    return thread


def stop_server():
    """Attempt to stop the HTTP signals server."""

    if not HAVE_FLASK:
        logger.warning("Flask is not installed; no signals server to stop.")
        return False

    if not _server_thread or not _server_thread.is_alive():
        return False

    logger.warning("Signals server thread cannot be stopped programmatically; ignoring request.")
    return False


def dispatch(payload):
    if not HAVE_FLASK:
        logger.warning("Flask is not installed; cannot dispatch signal payload.")
        return None

    return ingest_signal(payload)


def start_signals_server_if_enabled():
    enable = os.getenv("SIGNALS_HTTP", "0").lower() in {"1", "true", "yes"}
    if not enable:
        return None

    return start_server()

