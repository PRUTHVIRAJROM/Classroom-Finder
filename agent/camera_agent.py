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

DETECTORS = ("yolo", "face", "person", "motion")

MODEL_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models")
MODEL_URL = "https://github.com/ultralytics/assets/releases/download/v8.4.0/{name}"


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


class YoloDetector:
    """Counts PEOPLE with a YOLO neural network (the recommended detector for CCTV).

    Unlike the motion detector it only reports people, not shadows or curtains, and it
    also finds people who sit perfectly still. It runs through OpenCV, so no PyTorch is
    needed; it only needs a model file (downloaded once into the models/ folder).

    YOLO reads a 640x640 picture, so a far-away student would shrink to a few pixels.
    With tile=True (the default) a wide frame is cut into overlapping 640x640 pieces
    that are checked one by one at full sharpness, plus one pass over the whole frame
    for people who are close and big. Results are merged so nobody is counted twice.

    model:      "yolo11s.onnx" (default), or "yolo11n.onnx" (faster) / "yolo11m.onnx"
                (more accurate, slower). A path to your own .onnx file also works.
    confidence: 0.3 default. Lower finds more far-away students but allows false ones.
    """
    kind = "yolo"
    SIZE = 640

    def __init__(self, model="yolo11s.onnx", confidence=0.3, tile=True, nms=0.45):
        if not hasattr(cv2, "dnn") or not hasattr(cv2.dnn, "readNetFromONNX"):
            raise RuntimeError(
                f"This OpenCV build ({cv2.__version__}) cannot run YOLO. "
                "Install the 4.x line: pip install \"opencv-python>=4.9,<5\"")
        self.confidence = float(confidence)
        self.nms = float(nms)
        self.tile = bool(tile)
        self._net = cv2.dnn.readNetFromONNX(self._find_model(str(model)))

    @staticmethod
    def _find_model(model):
        if os.path.isfile(model):
            return model
        name = os.path.basename(model)
        path = os.path.join(MODEL_DIR, name)
        if os.path.isfile(path):
            return path
        if os.sep in model or "/" in model or not name.endswith(".onnx"):
            raise RuntimeError(f"Could not find the model file {model!r}.")
        os.makedirs(MODEL_DIR, exist_ok=True)
        print(f"Downloading the {name} people-detection model (one time only)...", flush=True)
        tmp = path + ".part"
        try:
            with requests.get(MODEL_URL.format(name=name), stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
            os.replace(tmp, path)
        except (requests.RequestException, OSError) as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise RuntimeError(
                f"Could not download the model ({e}). Download {MODEL_URL.format(name=name)} "
                f"by hand and put it in {MODEL_DIR}") from e
        return path

    def _run(self, img, scale=1.0, ox=0, oy=0, cut=(False, False, False, False)):
        """Run the network on one <=640x640 piece. Returns boxes/scores in full-frame pixels.

        cut = (left, top, right, bottom): which edges of this piece are cuts through the
        picture (not the real edge). A person touching a cut is only partly visible, so
        that box is dropped; the overlapping neighbour piece sees them whole.
        """
        s = self.SIZE
        h, w = img.shape[:2]
        canvas = np.full((s, s, 3), 114, np.uint8)
        canvas[:h, :w] = img
        self._net.setInput(cv2.dnn.blobFromImage(canvas, 1 / 255.0, (s, s), swapRB=True))
        out = np.squeeze(self._net.forward(), 0).T       # (8400, 84): cx, cy, w, h, 80 class scores
        scores = out[:, 4]                               # class 0 = person
        keep = scores >= self.confidence
        out, scores = out[keep], scores[keep]
        margin = 4
        boxes, kept = [], []
        for (cx, cy, bw, bh), sc in zip(out[:, :4], scores):
            x0, y0 = cx - bw / 2, cy - bh / 2
            if ((cut[0] and x0 < margin) or (cut[1] and y0 < margin)
                    or (cut[2] and x0 + bw > w - margin) or (cut[3] and y0 + bh > h - margin)):
                continue
            boxes.append([int(x0 / scale + ox), int(y0 / scale + oy),
                          int(bw / scale), int(bh / scale)])
            kept.append(float(sc))
        return boxes, kept

    @staticmethod
    def _starts(length, size, step):
        if length <= size:
            return [0]
        return list(range(0, length - size, step)) + [length - size]

    def detect(self, frame):
        s = self.SIZE
        h, w = frame.shape[:2]
        boxes, scores = [], []
        if not self.tile or (w <= s and h <= s):
            f = min(1.0, s / max(w, h))                  # fits in one pass
            img = cv2.resize(frame, (int(w * f), int(h * f))) if f < 1.0 else frame
            boxes, scores = self._run(img, scale=f)
        else:
            f = s / max(w, h)                            # whole frame, shrunk: for big/close people
            b, sc = self._run(cv2.resize(frame, (int(w * f), int(h * f))), scale=f)
            boxes += b; scores += sc
            step = int(s * 0.75)                         # 25% overlap so nobody is cut in half
            for y0 in self._starts(h, s, step):
                for x0 in self._starts(w, s, step):
                    cut = (x0 > 0, y0 > 0, x0 + s < w, y0 + s < h)
                    b, sc = self._run(frame[y0:y0 + s, x0:x0 + s], ox=x0, oy=y0, cut=cut)
                    boxes += b; scores += sc
        if not boxes:
            return []
        keep = cv2.dnn.NMSBoxes(boxes, scores, self.confidence, self.nms)
        order = sorted((int(i) for i in np.array(keep).reshape(-1)), key=lambda i: -scores[i])
        result = []                                      # drop boxes mostly inside a stronger one
        for i in order:
            x, y, bw, bh = boxes[i]
            x, y = max(0, x), max(0, y)
            box = (x, y, max(1, min(bw, w - x)), max(1, min(bh, h - y)))
            if all(self._overlap(box, r) < 0.8 for r in result):
                result.append(box)
        return result

    @staticmethod
    def _overlap(a, b):
        """Share of the smaller box that lies inside the other one."""
        iw = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
        ih = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
        if iw <= 0 or ih <= 0:
            return 0.0
        return iw * ih / min(a[2] * a[3], b[2] * b[3])


class MotionDetector:
    """Counts MOVING people-sized shapes. Made for a far-away CCTV camera.

    A face is only a few pixels wide from far away, so a face detector finds nothing.
    This one learns what the empty room looks like (background), then reports every
    shape that moves against it. Each shape counts as one student.

    Things to know:
      * It needs a fixed camera (not one that pans or zooms).
      * It sees people who MOVE (walking in, standing up, writing). Someone who sits
        perfectly still for a long time slowly fades into the background, which is why
        the worker keeps the highest recent count for `hold` seconds.
      * Students standing very close together can merge into one shape.
      * While it is still learning the room, or after a sudden change of the whole
        picture (lights switched, camera moved), it reports nothing, so the room
        shows "No signal" instead of a wrong "Free".

    min_area / max_area are in pixels of the (shrunk) picture. Raise min_area if
    shadows, curtains or flickering screens are counted; lower it if far-away
    students are missed.
    """
    kind = "motion"
    continuous = True          # must see every frame to learn the background

    def __init__(self, min_area=80, max_area=None, warmup=25, learning_rate=0.003,
                 sensitivity=32, max_change=0.4):
        if not hasattr(cv2, "createBackgroundSubtractorMOG2"):
            raise RuntimeError(
                f"This OpenCV build ({cv2.__version__}) has no motion detector. "
                "Install the 4.x line: pip install \"opencv-python>=4.9,<5\"")
        self.min_area = float(min_area)
        self.max_area = None if max_area in (None, "") else float(max_area)
        self.warmup = int(warmup)
        self.learning_rate = float(learning_rate)
        self.max_change = float(max_change)
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=300, varThreshold=float(sensitivity), detectShadows=True)
        self._open = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
        self._join = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
        self._seen = 0

    @property
    def ready(self):
        return self._seen >= self.warmup

    def detect(self, frame):
        frame = cv2.GaussianBlur(frame, (5, 5), 0)
        # learn quickly while warming up, slowly afterwards
        fg = self._bg.apply(frame, learningRate=self.learning_rate if self.ready else 0.05)
        self._seen += 1
        if not self.ready:
            return []
        _, mask = cv2.threshold(fg, 200, 255, cv2.THRESH_BINARY)   # 127 = shadow, dropped
        if mask.mean() / 255.0 > self.max_change:                  # whole picture changed
            self._seen = 0                                         # relearn the room
            return []
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self._open)  # remove speckle noise
        mask = cv2.dilate(mask, self._join, iterations=2)          # join head, body and legs
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_area or (self.max_area is not None and area > self.max_area):
                continue
            boxes.append(tuple(int(v) for v in cv2.boundingRect(c)))
        return boxes


def make_detector(kind, **options):
    """options only apply to the yolo and motion detectors (see their docstrings)."""
    if kind == "yolo":
        return YoloDetector(**options)
    if kind == "face":
        return FaceDetector()
    if kind == "person":
        return PersonDetector()
    if kind == "motion":
        return MotionDetector(**options)
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
                 hub=None, retry_after=5.0, hold=0.0):
        self.room = str(room)
        self.source = source
        self.detector = detector
        self.report = report
        self.interval = interval
        self.max_width = max_width
        self.hub = hub                      # optional: receives preview JPEGs
        self.retry_after = retry_after
        self.hold = float(hold)             # >0: report the highest count of the last N seconds
        self._peaks = deque()
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
        continuous = getattr(self.detector, "continuous", False)
        ready = True
        if detect or continuous:                 # a motion detector must watch every frame
            boxes = self.detector.detect(frame)
            ready = getattr(self.detector, "ready", True)
            self.boxes = boxes if ready else []
        if detect and ready:
            n = len(self.boxes)
            self.recent.append(n)
            if self.hold > 0:
                now = time.monotonic()
                self._peaks.append((now, n))
                while self._peaks and now - self._peaks[0][0] > self.hold:
                    self._peaks.popleft()
                count = max(v for _, v in self._peaks)
            else:
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
            continuous = getattr(self.detector, "continuous", False)
            if continuous and self.hub is None:
                pause = 0.2                       # ~5 frames a second to learn the background
            elif self.hub is not None:
                pause = 0.1                       # with a preview we refresh ~10 times a second
            else:
                pause = max(0.0, next_detect - time.monotonic())
            stop.wait(pause)
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
def post_count(server, room, api_key, count, timeout=5):
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
    p.add_argument("--detector", choices=DETECTORS, default="yolo")
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--api-key", default=os.environ.get("CF_API_KEY", ""))
    p.add_argument("--interval", type=float, default=10.0, help="seconds between reports")
    p.add_argument("--max-width", type=int, default=0, help="0 = automatic (1280 for yolo and motion, else 640)")
    p.add_argument("--hold", type=float, default=-1, help="seconds to keep the highest count (-1 = automatic)")
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

    motion = args.detector == "motion"
    worker = CameraWorker(args.room, args.source, make_detector(args.detector), report,
                          interval=args.interval,
                          max_width=args.max_width or (1280 if args.detector in ("yolo", "motion") else 640),
                          hold=args.hold if args.hold >= 0 else (30.0 if motion else 0.0))
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
