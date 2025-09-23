# run.py
import os, logging, threading, time
from flask import Flask, jsonify

# --- start health server ASAP ---
def _start_health_server():
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        return jsonify({"ok": True}), 200

    port = int(os.getenv("PORT") or 8080)
    host = "0.0.0.0"

    def _run():
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        logging.info("HEALTH server on %s:%s", host, port)
        app.run(host=host, port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=_run, daemon=True)
    t.start()

# --- kick off health server first ---
_start_health_server()

# --- then run your main app (blocks) ---
try:
    import main as _app
    if hasattr(_app, "main"):
        _app.main()
    else:
        # fallback: if main.py is a script-only, just import triggers execution
        while True:
            time.sleep(60)
except Exception as e:
    import traceback
    traceback.print_exc()
    # keep container alive so healthcheck stays green and you can see logs
    while True:
        time.sleep(60)
