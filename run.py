"""Start the classroom finder.

    python run.py                       the website + the cameras set up in the cameras/ folder
    python run.py --preview             also open http://localhost:8000/preview to watch the camera
    python run.py --simulate            fill rooms that have no camera with made-up demo data
    python run.py --share               let phones on the same Wi-Fi open the website
    python run.py --min-people 1        change the rule: 1 person or more = In use (default 7)
    python run.py --make-camera-files   create a cameras/<room>.json for any room that lacks one

Options can be combined, e.g.  python run.py --simulate --preview
"""
import argparse
import os
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--simulate", action="store_true",
                   help="fake data for rooms without a camera (labelled demo on the website)")
    p.add_argument("--preview", action="store_true",
                   help="turn on the camera preview page (this computer only)")
    p.add_argument("--share", action="store_true",
                   help="listen on the network so phones on the same Wi-Fi can open the site")
    p.add_argument("--min-people", type=int, default=7,
                   help="this many people or more = In use; fewer = Free (default 7)")
    p.add_argument("--free-after", type=float, default=30,
                   help="seconds below that number before a room shows as Free (default 30)")
    p.add_argument("--cameras", default=str(BASE / "cameras"),
                   help="folder with one file per room (default: the cameras folder)")
    p.add_argument("--make-camera-files", action="store_true",
                   help="create an empty camera file for every room that does not have one, then exit")
    args = p.parse_args()

    os.environ["CF_FREE_AFTER"] = str(args.free_after)
    os.environ["CF_MIN_PEOPLE"] = str(args.min_people)

    from app import simulator
    from app.cameras_config import create_missing_files, load_cameras
    from app.preview import PreviewHub
    from app.server import create_app

    if args.make_camera_files:
        import json
        rooms = json.loads((BASE / "rooms.json").read_text(encoding="utf-8"))
        created = create_missing_files(args.cameras, [str(r["id"]) for r in rooms])
        print(f"Created {len(created)} new file(s) in {args.cameras}" + (": " + ", ".join(created) if created else "."))
        return

    try:
        cameras = load_cameras(args.cameras)
    except (ValueError, OSError) as e:
        sys.exit(f"Problem with a file in the cameras folder: {e}")

    hub = PreviewHub() if args.preview else None
    app = create_app(hub=hub)
    store = app.extensions["store"]

    known = {r["id"] for r in store.list_rooms()}
    for c in cameras:
        if c["room"] not in known:
            sys.exit(f'The file cameras/{c["room"]}.json has an address, but rooms.json has no room '
                     f'"{c["room"]}". Rename the file to match a room, or add the room to rooms.json.')

    workers = []
    if cameras:
        try:
            from agent import camera_agent
        except ImportError:
            sys.exit("OpenCV is not installed. Run:  pip install -r requirements.txt")
        for c in cameras:
            try:
                detector = camera_agent.make_detector(c["detector"])
            except RuntimeError as e:
                sys.exit(str(e))
            worker = camera_agent.CameraWorker(
                c["room"], c["source"], detector,
                report=lambda n, rid=c["room"]: store.record_occupancy(rid, n),
                interval=c["interval"], max_width=c["max_width"], hub=hub)
            worker.start()
            workers.append(worker)
            print(f'Camera on: room {c["room"]}, source {c["source"]!r}, {c["detector"]} detector')
    else:
        print("No camera addresses set in the cameras folder, so every room will show 'No signal'.")

    live = {c["room"] for c in cameras}
    if args.simulate:
        app.config["SIMULATED_ROOMS"] = sorted(known - live)
        simulator.start_background(store, skip=live)
        print(f"Demo data ON for {len(known - live)} rooms without a camera.")
    elif cameras and len(live) < len(known):
        print(f"{len(known) - len(live)} room(s) have no camera address yet and show 'No signal'. "
              "Add --simulate to fill them with demo data.")

    if args.preview:
        app.config["PREVIEW_ROOMS"] = [c["room"] for c in cameras]
        print(f"Camera preview: http://localhost:{args.port}/preview")

    print(f"Website: http://localhost:{args.port}   (press Ctrl+C to stop)")
    app.run(host="0.0.0.0" if args.share else "127.0.0.1", port=args.port,
            threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
