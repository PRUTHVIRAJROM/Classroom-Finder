"""Reads the cameras/ folder: one small file per classroom.

cameras/301.json holds the video address for room 301, cameras/302.json for room 302,
and so on. The room is taken from the file name.
"""
import json
from pathlib import Path

DETECTORS = ("yolo", "face", "person", "motion")
# extra settings each detector understands, passed through from the camera file
DETECTOR_OPTIONS = {
    "yolo": ("model", "confidence", "tile", "nms"),
    "motion": ("min_area", "max_area", "sensitivity", "warmup", "learning_rate", "max_change"),
}

PLACEHOLDER = '{\n  "source": "",\n  "detector": "person"\n}\n'


def normalize_source(source):
    """0 or "0" -> webcam number 0. Anything else is kept as text (file path or URL)."""
    if isinstance(source, bool):
        raise ValueError("source must be a webcam number or a video address")
    if isinstance(source, int):
        return source
    s = str(source).strip()
    if not s:
        raise ValueError("source is empty")
    return int(s) if s.isdigit() else s


def load_cameras(folder):
    """Return the settings of every room that has a camera address.

    A file with an empty "source" (no address yet) or "enabled": false is skipped.
    A missing folder means no cameras.
    """
    folder = Path(folder)
    if not folder.is_dir():
        return []

    cameras = []
    for f in sorted(folder.glob("*.json")):
        room = f.stem
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ValueError(f"{f.name}: this is not valid JSON ({e.msg}, line {e.lineno}). "
                             "Check the quotes and commas, and use / instead of \\ in file paths.")
        if not isinstance(data, dict):
            raise ValueError(f"{f.name}: expected a block like {{ \"source\": 0 }}")
        if data.get("enabled", True) is False:
            continue
        source = data.get("source", "")
        if source is None or (isinstance(source, str) and not source.strip()):
            continue                                   # no address for this room yet
        detector = data.get("detector", "person")
        if detector not in DETECTORS:
            raise ValueError(f'{f.name}: detector must be "yolo", "face", "person" or "motion"')
        motion = detector == "motion"
        wide = detector in ("yolo", "motion")
        try:
            cameras.append({
                "room": room,
                "source": normalize_source(source),
                "detector": detector,
                "interval": float(data.get("interval", 2)),
                # far-away people are tiny, so yolo and motion keep a sharper picture
                "max_width": int(data.get("max_width", 1280 if wide else 640)),
                "hold": float(data.get("hold", 30 if motion else 0)),
                "options": {k: data[k] for k in DETECTOR_OPTIONS.get(detector, ()) if k in data},
            })
        except (TypeError, ValueError) as e:
            raise ValueError(f"{f.name}: {e}")
    return cameras


def create_missing_files(folder, room_ids):
    """Create an empty camera file for every room that does not have one. Returns the new names."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    created = []
    for rid in room_ids:
        path = folder / f"{rid}.json"
        if not path.exists():
            path.write_text(PLACEHOLDER, encoding="utf-8")
            created.append(path.name)
    return created
