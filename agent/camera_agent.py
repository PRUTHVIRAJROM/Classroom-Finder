"""Camera side of the classroom finder.

It reads a live video feed (laptop webcam, or later a CCTV stream), counts the
people it can see, and reports ONLY that number. Video is never saved.

Normally you do not run this file yourself: `python run.py` starts one worker per
room that has an address in the cameras folder. Run this file directly only on a separate classroom
computer that should report to a server over the network:

    python agent/camera_agent.py --room 301 --source 0 --detector face \\
        --server http://SERVER-IP:8000 --api-key YOUR_KEY
"""
import argparse
import os
import statistics
import sys
import threading
import time
from collections import deque

import cv2
import numpy as np
import requests

DETECTORS = ("face", "person")


# ---- detectors --------------------------------------------------------------
class FaceDetector:
    """Counts faces looking at the camera. Best for a webcam on a desk."""
    kind = "face"

    def __init__(self, min_size=30, min_neighbors=5):
        if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
            raise RuntimeError(
                f"This OpenCV build ({cv2.__version__}) has no face detector. "
                "Install the 4.x line: pip install \"opencv-python>=4.9,<5\""
            )
        path = os.path.join(cv2.data.haarcascades, "haarcascade_frontalface_default.xml")
        self._cascade = cv2.CascadeClassifier(path)
        if self._cascade.empty():
            raise RuntimeError(f"Could not load the face model from {path}")
        self.min_size = min_size
        self.min_neighbors = min_neighbors

    def detect(self, frame):
        gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
        faces = self._cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=self.min_neighbors,
            minSize=(self.min_size, self.min_size))
        return [tuple(int(v) for v in f) for f in faces]


class PersonDetector:
    """Counts whole bodies (OpenCV's built-in HOG people detector).

    Suited to a camera that sees people standing or walking from a distance.
    Weak on people sitting at desks seen from above.
    """
    kind = "person"

    def __init__(self, min_score=0.3, nms_threshold=0.4):
        if not hasattr(cv2, "HOGDescriptor"):
            raise RuntimeError(
                f"This OpenCV build ({cv2.__version__}) has no people detector. "
                "Install the 4.x line: pip install \"opencv-python>=4.9,<5\""
            )
        self._hog = cv2.HOGDescriptor()
        self._hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
        self.min_score = min_score
        self.nms_threshold = nms_threshold

    def detect(self, frame):
        rects, scores = self._hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(8, 8), scale=1.05)
        if len(rects) == 0:
            return []
        scores = np.asarray(scores, dtype=float).reshape(-1)
        boxes = [[int(x), int(y), int(w), int(h)] for x, y, w, h in rects]
        keep = cv2.dnn.NMSBoxes(boxes, scores.tolist(), self.min_score, self.nms_threshold)
        return [tuple(boxes[int(i)]) for i in np.array(keep).reshape(-1)]


def make_detector(kind):
    if kind == "face":
        return FaceDetector()
    if kind == "person":
        return PersonDetector()
    raise ValueError(f"unknown detector {kind!r}; choose one of {DETECTORS}")


# ---- video source -----------------------------------------------------------
def _normalize_source(source):
    if isinstance(source, int):
        return source
    s = str(source).strip()
    return int(s) if s.isdigit() else s


def open_source(source):
    """Open a webcam number (0, 1, ...), a video file, or a CCTV stream URL."""
    source = _normalize_source(source)
    if isinstance(source, int) and os.name == "nt" and hasattr(cv2, "CAP_DSHOW"):
        cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)   # more reliable for webcams on Windows
    else:
        cap = cv2.VideoCapture(source)
    if cap.isOpened():
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    return cap


def read_latest(cap, flush=4):
    """Read the freshest frame (skips buffered ones). Returns None on failure."""
    for _ in range(max(0, flush - 1)):
        cap.grab()
    ok, frame = cap.read()
    return frame if ok else None


def shrink(frame, max_width):
    h, w = frame.shape[:2]
    if max_width and w > max_width:
        return cv2.resize(frame, (max_width, int(h * max_width / w)))
    return frame


# ---- worker -----------------------------------------------------------------
class CameraWorker:
    """Watches one camera and calls report(count) every `interval` seconds.

    If the camera cannot be read, nothing is reported, so the website shows the
    room as "no signal" instead of wrongly showing it as free.
    """

    def __init__(self, room, source, detector, report, interval=2.0, max_width=640,
                 hub=None, retry_after=5.0):
        self.room = str(room)
        self.source = source
        self.detector = detector
        self.report = report
        self.interval = interval
        self.max_width = max_width
        self.hub = hub                      # optional: receives preview JPEGs
        self.retry_after = retry_after
        self.recent = deque(maxlen=3)       # median of last 3 counts smooths out flicker
        self.boxes = []
        self.last_count = None
        self._cap = None
        self._retry_at = 0.0
        self._warned_at = 0.0
        self._stop = threading.Event()
        self._thread = None

    # -- logging (throttled so a dead camera does not flood the terminal)
    def _warn(self, msg, every=30.0):
        now = time.monotonic()
        if now - self._warned_at >= every:
            self._warned_at = now
            print(f"[camera {self.room}] {msg}", file=sys.stderr, flush=True)

    def _release(self):
        if self._cap is not None:
            self._cap.release()
        self._cap = None
        self._retry_at = time.monotonic() + self.retry_after

    def _ensure_open(self):
        if self._cap is not None and self._cap.isOpened():
            return self._cap
        if time.monotonic() < self._retry_at:
            return None
        cap = open_source(self.source)
        if not cap.isOpened():
            cap.release()
            self._retry_at = time.monotonic() + self.retry_after
            if isinstance(_normalize_source(self.source), int):
                hint = ("Close other apps that use the webcam, and in Windows go to "
                        "Settings > Privacy & security > Camera and allow desktop apps.")
            else:
                hint = ("Check the address, username and password, and that this "
                        "computer can reach the camera.")
            self._warn(f"cannot open video source {self.source!r}. {hint}")
            return None
        self._cap = cap
        return cap

    def step(self, detect=True):
        """Read one frame; optionally count people and report. Returns the count or None."""
        cap = self._ensure_open()
        frame = read_latest(cap, 1 if self.hub is not None else 4) if cap is not None else None
        if frame is None:
            if cap is not None:
                self._warn("the video source opened but gave no picture")
                self._release()
            self.boxes = []
            return None

        frame = shrink(frame, self.max_width)
        count = None
        if detect:
            self.boxes = self.detector.detect(frame)
            self.recent.append(len(self.boxes))
            count = int(statistics.median(self.recent))
            self.last_count = count
        if self.hub is not None:
            self._publish(frame)
        if count is not None:
            self.report(count)
        return count

    def _publish(self, frame):
        out = frame.copy()
        for x, y, w, h in self.boxes:
            cv2.rectangle(out, (x, y), (x + w, y + h), (0, 200, 0), 2)
        shown = "..." if self.last_count is None else self.last_count
        cv2.rectangle(out, (0, 0), (out.shape[1], 28), (0, 0, 0), -1)
        cv2.putText(out, f"Room {self.room}: {shown} counted", (8, 20),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 70])
        if ok:
            self.hub.update(self.room, buf.tobytes())

    def run(self, stop=None):
        stop = stop or self._stop
        next_detect = 0.0
        while not stop.is_set():
            now = time.monotonic()
            due = now >= next_detect
            try:
                self.step(detect=due)
            except Exception as e:   # keep the worker alive through odd camera errors
                self._warn(f"error: {e!r}")
            if due:
                next_detect = now + self.interval
            # with a preview we refresh ~10 times a second, otherwise sleep until due
            stop.wait(0.1 if self.hub is not None else max(0.0, next_detect - time.monotonic()))
        self.close()

    def start(self):
        self._thread = threading.Thread(target=self.run, args=(self._stop,),
                                        name=f"camera-{self.room}", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def close(self):
        if self._cap is not None:
            self._cap.release()
            self._cap = None


# ---- stand-alone mode: report to a server over HTTP --------------------------
def post_count(server, room, api_key, count, timeout=30):
    # 30 s because a free host can take ~a minute to wake up; the next report retries anyway
    r = requests.post(
        f"{server.rstrip('/')}/api/rooms/{room}/occupancy",
        json={"count": count},
        headers={"X-API-Key": api_key},
        timeout=timeout,
    )
    r.raise_for_status()


def main(argv=None):
    p = argparse.ArgumentParser(description="Report people counts from one camera to a server")
    p.add_argument("--room", required=True, help="room id, e.g. 301 (must exist in rooms.json)")
    p.add_argument("--source", default="0", help="webcam number, video file, or CCTV stream URL")
    p.add_argument("--detector", choices=DETECTORS, default="person")
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--api-key", default=os.environ.get("CF_API_KEY", ""))
    p.add_argument("--interval", type=float, default=10.0, help="seconds between reports")
    p.add_argument("--max-width", type=int, default=640)
    p.add_argument("--once", action="store_true", help="report one time and exit")
    args = p.parse_args(argv)

    if not args.api_key:
        sys.exit("No API key: pass --api-key or set CF_API_KEY.")

    def report(count):
        try:
            post_count(args.server, args.room, args.api_key, count)
            print(f"{time.strftime('%H:%M:%S')} room={args.room} counted={count}", flush=True)
        except requests.RequestException as e:
            print(f"could not reach server: {e}", file=sys.stderr, flush=True)

    worker = CameraWorker(args.room, args.source, make_detector(args.detector), report,
                          interval=args.interval, max_width=args.max_width)
    if args.once:
        worker.step(detect=True)
        worker.close()
        return
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.close()


if __name__ == "__main__":
    main()
