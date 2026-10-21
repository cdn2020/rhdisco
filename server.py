#!/usr/bin/env python3
"""Red Hat Subscription Discovery Tool - local web UI.

    python3 server.py              # http://127.0.0.1:8088
    python3 server.py --port 9000 --bind 0.0.0.0

Credentials are held in memory only for the duration of a discovery job and are never written to disk.
"""
import argparse
import os
import sys
import threading
import traceback
import uuid
import webbrowser

from flask import Flask, Response, jsonify, request, send_from_directory

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rhdisco import __version__  # noqa: E402
from rhdisco.export import to_xlsx  # noqa: E402
from rhdisco.licensing import summarize  # noqa: E402
from rhdisco.util import Logger, now_iso  # noqa: E402

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE, "static"))
JOBS = {}
LOCK = threading.Lock()


def _runner(kind):
    if kind == "openshift":
        from rhdisco.openshift import discover_openshift
        return discover_openshift
    if kind == "vcenter":
        from rhdisco.vcenter import discover_vcenter
        return discover_vcenter
    if kind == "rhv":
        from rhdisco.rhv import discover_rhv
        return discover_rhv
    if kind == "rhel":
        from rhdisco.rhel import discover_rhel
        return discover_rhel
    raise ValueError(f"Unknown discovery type: {kind}")


def _run_job(jid, kind, params):
    job = JOBS[jid]

    def cb(line):
        with LOCK:
            job["log"].append(line)

    log = Logger(cb)
    try:
        job["result"] = _runner(kind)(params, log)
        job["status"] = "done"
        log("Discovery finished")
    except Exception as e:
        job["status"] = "error"
        job["error"] = f"{type(e).__name__}: {e}"
        log.error(job["error"])
        traceback.print_exc()
    finally:
        params.clear()  # drop credentials
        job["finished"] = now_iso()


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    mods = {}
    for m in ("paramiko", "pyVmomi", "requests", "openpyxl"):
        try:
            __import__(m)
            mods[m] = True
        except ImportError:
            mods[m] = False
    return jsonify({"ok": True, "version": __version__, "modules": mods})


@app.post("/api/jobs")
def start_job():
    body = request.get_json(force=True)
    kind = body.get("type")
    params = body.get("params") or {}
    _runner(kind)  # validate type early
    jid = uuid.uuid4().hex[:12]
    JOBS[jid] = {"id": jid, "type": kind, "status": "running", "started": now_iso(), "log": [], "result": None,
                 "target": params.get("api_url") or params.get("host") or params.get("url") or f"{len((params.get('hosts') or '').splitlines())} host(s)"}
    threading.Thread(target=_run_job, args=(jid, kind, params), daemon=True).start()
    return jsonify({"id": jid})


@app.get("/api/jobs/<jid>")
def job_status(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    since = int(request.args.get("since", 0))
    with LOCK:
        data = {k: v for k, v in job.items() if k not in ("log", "result")}
        data["log"] = job["log"][since:]
        data["log_total"] = len(job["log"])
    if job["status"] != "running":
        data["result"] = job["result"]
    return jsonify(data)


@app.post("/api/summary")
def api_summary():
    return jsonify(summarize(request.get_json(force=True)))


@app.post("/api/export/xlsx")
def api_xlsx():
    a = request.get_json(force=True)
    data = to_xlsx(a, summarize(a))
    name = f"rh-discovery-{(a.get('meta') or {}).get('customer') or 'assessment'}-{now_iso()[:10]}.xlsx".replace(" ", "_")
    return Response(data, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f"attachment; filename={name}"})


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="127.0.0.1", help="interface to listen on (default 127.0.0.1 - local only)")
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    url = f"http://{'127.0.0.1' if a.bind in ('0.0.0.0', '::') else a.bind}:{a.port}/"
    print(f"Red Hat Discovery Tool {__version__} -> {url}  (Ctrl+C to stop)")
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    app.run(host=a.bind, port=a.port, threaded=True, debug=False)


if __name__ == "__main__":
    main()
