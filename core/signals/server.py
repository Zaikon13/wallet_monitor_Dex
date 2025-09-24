import os
from threading import Thread
from flask import Flask, request, jsonify
from core.signals.adapter import ingest_signal
def create_app():
    app=Flask(__name__)
    @app.get('/healthz')
    def h(): return {'ok': True}
    @app.post('/signal')
    def s():
        try:
            data=request.get_json(force=True, silent=True) or {}
            out=ingest_signal(data)
            return jsonify({'ok': bool(out), 'action': (out or {}).get('guard_action')})
        except Exception as e:
            return jsonify({'ok': False, 'error': str(e)}), 400
    return app
def start_signals_server_if_enabled():
    enable = os.getenv('SIGNALS_HTTP','0').lower() in {'1','true','yes'}
    if not enable: return None
    bind=os.getenv('SIGNALS_BIND','0.0.0.0:8080'); host,port=(bind.split(':',1)+['8080'])[:2]
    app=create_app()
    def run(): app.run(host=host, port=int(port), debug=False, use_reloader=False)
    th=Thread(target=run, daemon=True); th.start(); return th
