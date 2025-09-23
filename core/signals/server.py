# core/signals/server.py
import os
import logging
from threading import Thread
from flask import Flask, request, jsonify

def create_app():
    app = Flask(__name__)

    @app.get("/healthz")
    def health():
        return {"ok": True}, 200

    @app.post("/signal")
    def signal_in():
        try:
            # optional: your adapter; μην σπάει αν λείπει
            try:
                from core.signals.adapter import ingest_signal
            except Exception:
                ingest_signal = None
            data = request.get_json(force=True, silent=True) or {}
            out = ingest_signal(data) if ingest_signal else None
            return jsonify({"ok": True, "action": (out or {}).get("guard_action") if isinstance(out, dict) else None})
        except Exception as e:
            logging.exception("signal handler error: %s", e)
            return jsonify({"ok": False, "error": str(e)}), 400

    return app

def start_signals_server_if_enabled():
    """
    Auto-enable HTTP server on Railway when PORT exists,
    or when SIGNALS_HTTP is truthy (1/true/yes).
    Binds to 0.0.0.0:<PORT> ώστε να περνάει το healthcheck (/healthz).
    """
    auto_on = bool(os.getenv("PORT"))
    flag_on = str(os.getenv("SIGNALS_HTTP", "0")).lower() in {"1", "true", "yes"}
    if not (auto_on or flag_on):
        logging.info("signals HTTP disabled (set SIGNALS_HTTP=1 or rely on PORT)")
        return None

    port = int(os.getenv("PORT") or os.getenv("SIGNALS_PORT") or "8080")
    host_env = os.getenv("SIGNALS_HOST") or os.getenv("SIGNALS_BIND") or "0.0.0.0"
    host = (host_env.split(":")[0] or "0.0.0.0")

    app = create_app()

    def run():
        logging.info("starting signals HTTP on %s:%s", host, port)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    th = Thread(target=run, daemon=True)
    th.start()
    return th
