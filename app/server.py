"""Flask API + static website for the classroom finder."""
import hmac
import os
import time
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory
from markupsafe import escape

from .store import DEFAULT_FREE_AFTER, MAX_COUNT, Store

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
VALID_STATUSES = {"free", "occupied", "unknown"}


def create_app(db_path=None, rooms_file=None, api_key=None, store=None,
               hub=None, simulated_rooms=()):
    """hub: optional PreviewHub (turns on the local-only camera preview).
    simulated_rooms: room ids whose data is fake; the website labels them as demo."""
    app = Flask(__name__, static_folder=None)

    db_path = db_path or os.environ.get("CF_DB", BASE_DIR / "data" / "classroom.db")
    rooms_file = rooms_file or os.environ.get("CF_ROOMS", BASE_DIR / "rooms.json")
    api_key = api_key or os.environ.get("CF_API_KEY", "")
    free_after = float(os.environ.get("CF_FREE_AFTER", DEFAULT_FREE_AFTER))
    app.config["API_KEY"] = api_key
    app.config["SIMULATED_ROOMS"] = sorted(simulated_rooms)
    app.config["PREVIEW_ROOMS"] = []
    app.extensions["store"] = store or Store(db_path, rooms_file=rooms_file, free_after=free_after)
    app.extensions["hub"] = hub
    db = app.extensions["store"]

    # Hosted demo mode, set on the hosting site instead of typing --simulate:
    #   CF_SIMULATE=1          fill rooms with made-up data (the website labels them "Demo data")
    #   CF_LIVE_ROOMS=301,302  rooms with a real camera sending counts; these are not faked
    if os.environ.get("CF_SIMULATE", "").strip().lower() in ("1", "true", "yes"):
        from . import simulator
        live = {r.strip() for r in os.environ.get("CF_LIVE_ROOMS", "").split(",") if r.strip()}
        app.config["SIMULATED_ROOMS"] = sorted(r["id"] for r in db.list_rooms() if r["id"] not in live)
        simulator.start_background(db, skip=live)

    # ---- website ---------------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:name>")
    def static_files(name):
        return send_from_directory(STATIC_DIR, name)

    # ---- read API (public) -----------------------------------------------
    @app.get("/api/health")
    def health():
        return jsonify(ok=True)

    @app.get("/api/summary")
    def summary():
        return jsonify(db.summary())

    @app.get("/api/rooms")
    def rooms():
        status = request.args.get("status")
        if status and status not in VALID_STATUSES:
            return jsonify(error=f"status must be one of {sorted(VALID_STATUSES)}"), 400
        floor = request.args.get("floor")
        if floor is not None:
            try:
                floor = int(floor)
            except ValueError:
                return jsonify(error="floor must be an integer"), 400
        data = db.list_rooms(building=request.args.get("building"), floor=floor, status=status)
        return jsonify(rooms=data, summary=db.summary(),
                       simulated_rooms=app.config["SIMULATED_ROOMS"])

    @app.get("/api/rooms/<room_id>")
    def room(room_id):
        data = db.get_room(room_id)
        if data is None:
            return jsonify(error="room not found"), 404
        return jsonify(data)

    # ---- camera preview (off by default, this computer only) ---------------
    # Shows what the program is counting, so you can check the detector works.
    # Nothing is saved, and other computers on the network cannot open it.
    def _preview_allowed():
        hub_on = app.extensions["hub"] is not None
        local = request.remote_addr in ("127.0.0.1", "::1")
        return hub_on, local

    @app.get("/preview")
    def preview_page():
        hub_on, local = _preview_allowed()
        if not hub_on:
            return "Preview is off. Start the server with: python run.py --preview", 404
        if not local:
            return "The camera preview can only be opened on the computer running the server.", 403
        rooms = app.config["PREVIEW_ROOMS"]
        if not rooms:
            return "No cameras are set up yet. Put an address in a room's file in the cameras folder.", 200
        imgs = "".join(
            f'<figure><img src="/preview/{escape(r)}/stream.mjpg" alt="Live view of room {escape(r)}">'
            f"<figcaption>Room {escape(r)}</figcaption></figure>"
            for r in rooms
        )
        page = (
            "<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Camera preview</title>"
            "<style>body{font-family:system-ui,sans-serif;margin:1.5rem;background:#111;color:#eee}"
            "img{max-width:100%;height:auto;border-radius:6px}figure{margin:0 0 1.5rem}"
            "p{max-width:40rem;color:#aaa}</style>"
            "<h1>Camera preview</h1>"
            "<p>Green boxes are what the program counts. This page only works on this computer, "
            "and nothing is saved.</p>" + imgs
        )
        return page

    @app.get("/preview/<room_id>/stream.mjpg")
    def preview_stream(room_id):
        hub_on, local = _preview_allowed()
        if not hub_on:
            return jsonify(error="preview is off"), 404
        if not local:
            return jsonify(error="preview is only available on the server computer"), 403
        hub = app.extensions["hub"]

        def frames():
            last, idle = None, 0.0
            while idle < 30:
                item = hub.latest(room_id)
                if item and item[1] != last:
                    jpeg, last = item[0], item[1]
                    idle = 0.0
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                else:
                    time.sleep(0.05)
                    idle += 0.05

        return Response(frames(), mimetype="multipart/x-mixed-replace; boundary=frame",
                        headers={"Cache-Control": "no-store"})

    # ---- write API (classroom agents, needs the key) ----------------------
    @app.post("/api/rooms/<room_id>/occupancy")
    def occupancy(room_id):
        if not app.config["API_KEY"]:
            return jsonify(error="server has no CF_API_KEY set; writes are disabled"), 503
        supplied = request.headers.get("X-API-Key", "")
        if not hmac.compare_digest(supplied.encode(), app.config["API_KEY"].encode()):
            return jsonify(error="invalid API key"), 401

        body = request.get_json(silent=True) or {}
        count = body.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_COUNT:
            return jsonify(error=f"count must be an integer from 0 to {MAX_COUNT}"), 400
        if not db.record_occupancy(room_id, count):
            return jsonify(error="room not found"), 404
        return jsonify(db.get_room(room_id)), 200

    return app
