"""Serve recorded inference states to the real Flutter UI for a replay capture.

No labels or parking decisions are generated here. Run create_inference_demo.py
first and supply its JSON report. This local-only server never changes app data.
"""
import argparse
import bisect
import json
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, send_from_directory


def create_replay_app(report_path, web_root):
    report = json.loads(Path(report_path).read_text())
    rows = report["observations"]
    if not rows:
        raise ValueError("The inference report contains no observations")
    timestamps = [r["source_seconds"] for r in rows]
    if timestamps != sorted(timestamps):
        raise ValueError("Observations must be ordered by source time")
    state = {"started": None, "requests": []}
    lock = threading.Lock()
    app = Flask(__name__, static_folder=None)

    @app.after_request
    def cors(response):
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.post("/replay/start")
    def start():
        with lock:
            state["started"] = time.monotonic()
            state["requests"] = []
        return jsonify(source_start=report["start_seconds"],
                       duration=report["duration_seconds"])

    @app.get("/api/status")
    def status():
        with lock:
            elapsed = 0 if state["started"] is None else time.monotonic() - state["started"]
            target = report["start_seconds"] + elapsed
            index = max(0, min(len(rows) - 1, bisect.bisect_right(timestamps, target) - 1))
            row = rows[index]
            spaces = [{"id": sid, "status": value["status"], "vehicle_type": None}
                      for sid, value in row["states"].items()]
            occupied = sum(s["status"] == "occupied" for s in spaces)
            total = len(spaces)
            state["requests"].append({"elapsed": round(elapsed, 3),
                                      "source_seconds": row["source_seconds"],
                                      "B5": row["states"].get("B5", {}).get("status"),
                                      "available": total - occupied})
        return jsonify({report["lot_id"]: {
            "total_spaces": total, "occupied_spaces": occupied,
            "available_spaces": total - occupied,
            "occupancy_rate": round(occupied / total * 100, 2),
            "spaces": spaces}})

    @app.get("/replay/log")
    def log():
        with lock:
            return jsonify(list(state["requests"]))

    @app.get("/api/parking_lots")
    def lots():
        return jsonify([])

    @app.get("/")
    @app.get("/<path:name>")
    def web(name="index.html"):
        return send_from_directory(str(Path(web_root).resolve()), name)

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--web-root", type=Path, default=Path("build/web"))
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    create_replay_app(args.report, args.web_root).run(host="127.0.0.1", port=args.port,
                                                    threaded=True, use_reloader=False)
