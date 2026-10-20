import json
import random
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import camera_agent                      # noqa: E402
from app import simulator                           # noqa: E402
from app.cameras_config import create_missing_files, load_cameras   # noqa: E402
from app.preview import PreviewHub                 # noqa: E402
from app.server import create_app                  # noqa: E402
from app.store import Store                        # noqa: E402

KEY = "test-key"
ROOMS = ROOT / "tests" / "fixtures" / "rooms.json"   # 15 sample rooms, independent of rooms.json


class StubDetector:
    """Pretends to find people: returns the next count from the list (last one repeats)."""
    def __init__(self, counts):
        self.counts, self.i = list(counts), 0

    def detect(self, frame):
        n = self.counts[min(self.i, len(self.counts) - 1)]
        self.i += 1
        return [(10 + 20 * k, 10, 15, 15) for k in range(n)]


def make_video(path, frames=40):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"MJPG"), 10, (160, 120))
    assert writer.isOpened(), "could not create test video"
    for _ in range(frames):
        writer.write(np.zeros((120, 160, 3), dtype=np.uint8))
    writer.release()
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "t.db", rooms_file=ROOMS, stale_after=90, occupied_above=7)
        self.client = create_app(api_key=KEY, store=self.store).test_client()

    def post(self, room, count, key=KEY):
        return self.client.post(f"/api/rooms/{room}/occupancy", json={"count": count},
                                headers={"X-API-Key": key})


# ---- occupancy rules --------------------------------------------------------
class StoreRules(Base):
    def test_new_room_is_unknown(self):
        self.assertEqual(self.store.get_room("A-101")["status"], "unknown")

    def test_more_than_seven_students_means_occupied(self):
        self.store.record_occupancy("A-101", 8, now=1000)
        self.assertEqual(self.store.get_room("A-101", now=1005)["status"], "occupied")

    def test_seven_or_fewer_students_is_still_free(self):
        for n in (1, 3, 7):
            self.store.record_occupancy("A-101", n, now=1000)
            room = self.store.get_room("A-101", now=1005)
            self.assertEqual(room["status"], "free")
            self.assertEqual(room["count"], n)           # the number is still reported

    def test_room_is_free_immediately_when_nobody_is_counted(self):
        self.store.record_occupancy("A-101", 12, now=1000)
        self.store.record_occupancy("A-101", 0, now=1010)
        self.assertEqual(self.store.get_room("A-101", now=1011)["status"], "free")

    def test_threshold_can_be_changed(self):
        s = Store(self.tmp / "t2.db", rooms_file=ROOMS, occupied_above=1)
        s.record_occupancy("A-101", 2, now=1000)
        self.assertEqual(s.get_room("A-101", now=1001)["status"], "occupied")

    def test_never_seen_anyone_is_free_right_away(self):
        self.store.record_occupancy("A-102", 0, now=1000)
        room = self.store.get_room("A-102", now=1010)
        self.assertEqual(room["status"], "free")
        self.assertIsNone(room["free_for_seconds"])

    def test_stale_report_is_unknown_not_free(self):
        self.store.record_occupancy("A-101", 0, now=1000)
        self.assertEqual(self.store.get_room("A-101", now=1091)["status"], "unknown")

    def test_summary_counts(self):
        self.store.record_occupancy("A-101", 9, now=1000)
        self.store.record_occupancy("A-102", 0, now=1000)
        s = self.store.summary(now=1001)
        self.assertEqual((s["occupied"], s["free"], s["unknown"], s["total"]), (1, 1, 13, 15))

    def test_reseed_keeps_live_data(self):
        self.store.record_occupancy("A-101", 4)
        self.store.seed_rooms(ROOMS)
        self.assertEqual(self.store.get_room("A-101")["count"], 4)

    def test_rooms_removed_from_file_are_removed(self):
        f = self.tmp / "small.json"
        f.write_text(json.dumps([{"id": "A-101", "building": "Building A", "floor": 1}]))
        self.store.seed_rooms(f)
        self.assertEqual([r["id"] for r in self.store.list_rooms()], ["A-101"])

    def test_name_and_capacity_are_optional(self):
        f = self.tmp / "min.json"
        f.write_text(json.dumps([{"id": 301, "building": "Main", "floor": 3}]))
        s = Store(self.tmp / "m.db", rooms_file=f)
        room = s.get_room("301")
        self.assertEqual((room["name"], room["capacity"], room["floor"]), ("Room 301", 0, 3))


# ---- web API ----------------------------------------------------------------
class Api(Base):
    def test_list_and_filters(self):
        self.post("A-101", 10)
        self.post("B-101", 0)
        data = self.client.get("/api/rooms").get_json()
        self.assertEqual(len(data["rooms"]), 15)
        only_b = self.client.get("/api/rooms?building=Building B").get_json()["rooms"]
        self.assertEqual({r["building"] for r in only_b}, {"Building B"})
        floor1 = self.client.get("/api/rooms?floor=1").get_json()["rooms"]
        self.assertEqual({r["floor"] for r in floor1}, {1})
        occupied = self.client.get("/api/rooms?status=occupied").get_json()["rooms"]
        self.assertEqual([r["id"] for r in occupied], ["A-101"])

    def test_bad_query_params(self):
        self.assertEqual(self.client.get("/api/rooms?status=bogus").status_code, 400)
        self.assertEqual(self.client.get("/api/rooms?floor=abc").status_code, 400)

    def test_get_room_and_404(self):
        self.assertEqual(self.client.get("/api/rooms/A-101").status_code, 200)
        self.assertEqual(self.client.get("/api/rooms/NOPE").status_code, 404)

    def test_write_requires_key(self):
        self.assertEqual(self.post("A-101", 3, key="wrong").status_code, 401)
        r = self.client.post("/api/rooms/A-101/occupancy", json={"count": 3})
        self.assertEqual(r.status_code, 401)

    def test_write_disabled_without_server_key(self):
        client = create_app(api_key="", store=self.store).test_client()
        r = client.post("/api/rooms/A-101/occupancy", json={"count": 1},
                        headers={"X-API-Key": ""})
        self.assertEqual(r.status_code, 503)

    def test_count_validation(self):
        for bad in [-1, 1001, "5", 2.5, None, True]:
            with self.subTest(count=bad):
                self.assertEqual(self.post("A-101", bad).status_code, 400)

    def test_write_unknown_room(self):
        self.assertEqual(self.post("NOPE", 1).status_code, 404)

    def test_post_then_read_back(self):
        r = self.post("A-103", 8)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 8)
        self.assertEqual(self.client.get("/api/rooms/A-103").get_json()["status"], "occupied")

    def test_website_files_served(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"<html", home.data.lower())
        for name in ("app.js", "style.css"):
            self.assertEqual(self.client.get(f"/static/{name}").status_code, 200)

    def test_simulated_rooms_are_reported(self):
        app = create_app(api_key=KEY, store=self.store, simulated_rooms=["A-102", "A-101"])
        data = app.test_client().get("/api/rooms").get_json()
        self.assertEqual(data["simulated_rooms"], ["A-101", "A-102"])
        self.assertEqual(self.client.get("/api/rooms").get_json()["simulated_rooms"], [])


# ---- local-only camera preview ----------------------------------------------
class Preview(Base):
    def make_client(self):
        hub = PreviewHub()
        hub.update("301", b"\xff\xd8not-a-real-jpeg\xff\xd9")
        app = create_app(api_key=KEY, store=self.store, hub=hub)
        app.config["PREVIEW_ROOMS"] = ["301"]
        return app.test_client()

    def test_off_by_default(self):
        self.assertEqual(self.client.get("/preview").status_code, 404)
        self.assertEqual(self.client.get("/preview/301/stream.mjpg").status_code, 404)

    def test_page_and_stream_work_on_this_computer(self):
        client = self.make_client()
        page = client.get("/preview")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"/preview/301/stream.mjpg", page.data)
        resp = client.get("/preview/301/stream.mjpg", buffered=False)
        first = next(iter(resp.response))
        resp.close()
        self.assertIn(b"image/jpeg", first)

    def test_blocked_for_other_computers(self):
        client = self.make_client()
        other = {"REMOTE_ADDR": "192.168.1.20"}
        self.assertEqual(client.get("/preview", environ_overrides=other).status_code, 403)
        self.assertEqual(
            client.get("/preview/301/stream.mjpg", environ_overrides=other).status_code, 403)


# ---- cameras/ folder (one file per room) -------------------------------------
class CamerasConfig(unittest.TestCase):
    def folder(self, files):
        """Make a temporary cameras folder. files: {"301.json": dict-or-raw-text}."""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        for name, content in files.items():
            text = content if isinstance(content, str) else json.dumps(content)
            (Path(d.name) / name).write_text(text)
        return Path(d.name)

    def test_missing_folder_means_no_cameras(self):
        self.assertEqual(load_cameras(ROOT / "no-such-folder"), [])

    def test_room_comes_from_the_file_name(self):
        cams = load_cameras(self.folder({
            "301.json": {"source": "0", "detector": "face"},
            "302.json": {"source": "rtsp://u:p@10.0.0.5:554/s1"},
        }))
        self.assertEqual([c["room"] for c in cams], ["301", "302"])
        self.assertEqual((cams[0]["source"], cams[0]["detector"]), (0, "face"))
        self.assertEqual(cams[1]["source"], "rtsp://u:p@10.0.0.5:554/s1")
        self.assertEqual((cams[1]["detector"], cams[1]["interval"], cams[1]["max_width"]),
                         ("person", 2.0, 640))

    def test_webcam_zero_counts_as_an_address(self):
        cams = load_cameras(self.folder({"301.json": {"source": 0}}))
        self.assertEqual(len(cams), 1)
        self.assertEqual(cams[0]["source"], 0)

    def test_rooms_without_an_address_or_switched_off_are_skipped(self):
        cams = load_cameras(self.folder({
            "301.json": {"source": ""},
            "302.json": {"source": 0, "enabled": False},
            "303.json": {"detector": "face"},
            "304.json": {"source": 1},
        }))
        self.assertEqual([c["room"] for c in cams], ["304"])

    def test_other_files_in_the_folder_are_ignored(self):
        cams = load_cameras(self.folder({"README.txt": "hello", "301.json": {"source": 0}}))
        self.assertEqual([c["room"] for c in cams], ["301"])

    def test_clear_errors_name_the_file(self):
        for name, content, text in [
            ("301.json", {"source": 0, "detector": "x"}, "detector"),
            ("302.json", '{"source": 0,}', "not valid JSON"),
            ("303.json", "[1, 2]", "expected"),
            ("304.json", {"source": True}, "source"),
        ]:
            with self.subTest(file=name), self.assertRaises(ValueError) as ctx:
                load_cameras(self.folder({name: content}))
            self.assertIn(name, str(ctx.exception))
            self.assertIn(text, str(ctx.exception))

    def test_make_missing_files_only_adds_new_ones(self):
        folder = self.folder({"301.json": {"source": 0}})
        created = create_missing_files(folder, ["301", "302", "303"])
        self.assertEqual(created, ["302.json", "303.json"])
        self.assertEqual(json.loads((folder / "301.json").read_text()), {"source": 0})
        self.assertEqual(load_cameras(folder)[0]["room"], "301")      # new files have no address
        self.assertEqual(len(load_cameras(folder)), 1)
        self.assertEqual(create_missing_files(folder, ["301", "302", "303"]), [])

    def test_shipped_files_agree_with_rooms(self):
        rooms = {str(r["id"]) for r in json.loads((ROOT / "rooms.json").read_text())}
        self.assertEqual(rooms, {str(n) for n in range(301, 320)})
        files = {p.stem for p in (ROOT / "cameras").glob("*.json")}
        self.assertEqual(files, rooms)                       # exactly one file per room
        cams = load_cameras(ROOT / "cameras")                # and they all parse
        self.assertEqual([(c["room"], c["source"]) for c in cams], [("301", 0)])
        self.assertFalse((ROOT / "cameras.json").exists())  # the old single file is gone


# ---- camera worker ----------------------------------------------------------
class Worker(Base):
    def test_reports_counts_from_a_video(self):
        video = make_video(self.tmp / "cam.avi")
        got = []
        w = camera_agent.CameraWorker("301", str(video), StubDetector([3]), got.append)
        self.assertEqual(w.step(), 3)
        w.close()
        self.assertEqual(got, [3])

    def test_one_glitch_frame_is_ignored(self):
        video = make_video(self.tmp / "cam.avi")
        got = []
        w = camera_agent.CameraWorker("301", str(video), StubDetector([4, 4, 0]), got.append)
        for _ in range(3):
            w.step()
        w.close()
        self.assertEqual(got, [4, 4, 4])

    def test_unreadable_camera_reports_nothing_and_does_not_hammer(self):
        got = []
        w = camera_agent.CameraWorker("301", str(self.tmp / "missing.avi"),
                                      StubDetector([1]), got.append)
        with mock.patch.object(camera_agent, "open_source", wraps=camera_agent.open_source) as op:
            self.assertIsNone(w.step())
            self.assertIsNone(w.step())
            self.assertEqual(op.call_count, 1)    # second try waits for the retry delay
        self.assertEqual(got, [])

    def test_preview_frame_is_published_with_a_box_drawn(self):
        video = make_video(self.tmp / "cam.avi")
        hub = PreviewHub()
        w = camera_agent.CameraWorker("301", str(video), StubDetector([2]), lambda n: None, hub=hub)
        w.step()
        w.close()
        jpeg, _ = hub.latest("301")
        img = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(img.shape[:2], (120, 160))
        self.assertGreater(int(img[40:110].sum()), 0)   # started black; the green boxes add colour

    def test_background_thread_keeps_reporting_into_the_store(self):
        video = make_video(self.tmp / "long.avi", frames=400)
        w = camera_agent.CameraWorker(
            "A-101", str(video), StubDetector([9]),
            lambda n: self.store.record_occupancy("A-101", n), interval=0.05)
        w.start()
        deadline = time.time() + 5
        while time.time() < deadline and self.store.get_room("A-101")["count"] != 9:
            time.sleep(0.05)
        w.stop()
        room = self.store.get_room("A-101")
        self.assertEqual((room["count"], room["status"]), (9, "occupied"))

    def test_stand_alone_agent_reports_over_http(self):
        from werkzeug.serving import make_server
        video = make_video(self.tmp / "cam.avi")
        server = make_server("127.0.0.1", 0, create_app(api_key=KEY, store=self.store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with mock.patch.object(camera_agent, "make_detector", lambda kind: StubDetector([8])):
            camera_agent.main(["--room", "A-101", "--source", str(video), "--detector", "face",
                               "--server", f"http://127.0.0.1:{server.server_port}",
                               "--api-key", KEY, "--once"])
        room = self.store.get_room("A-101")
        self.assertEqual((room["count"], room["status"]), (8, "occupied"))


# ---- detectors (need OpenCV 4.x; skipped where the build lacks them) -------
class Detectors(unittest.TestCase):
    blank = np.zeros((480, 640, 3), dtype=np.uint8)

    def test_unknown_detector_name(self):
        with self.assertRaises(ValueError):
            camera_agent.make_detector("banana")

    @unittest.skipUnless(hasattr(cv2, "CascadeClassifier"), "needs OpenCV 4.x")
    def test_face_detector_sees_nothing_in_a_blank_frame(self):
        self.assertEqual(camera_agent.make_detector("face").detect(self.blank), [])

    @unittest.skipUnless(hasattr(cv2, "HOGDescriptor"), "needs OpenCV 4.x")
    def test_person_detector_sees_nothing_in_a_blank_frame(self):
        self.assertEqual(camera_agent.make_detector("person").detect(self.blank), [])

    @unittest.skipIf(hasattr(cv2, "CascadeClassifier"), "only where OpenCV lacks the detector")
    def test_missing_face_detector_gives_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            camera_agent.make_detector("face")
        self.assertIn("4.x", str(ctx.exception))

    @unittest.skipIf(hasattr(cv2, "HOGDescriptor"), "only where OpenCV lacks the detector")
    def test_missing_person_detector_gives_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            camera_agent.make_detector("person")
        self.assertIn("4.x", str(ctx.exception))


# ---- demo simulator ---------------------------------------------------------
class Simulator(Base):
    def test_rooms_with_a_real_camera_are_left_alone(self):
        rng, state = random.Random(1), {}
        for _ in range(60):
            simulator.tick(self.store, rng, state, skip={"A-101"})
        self.assertIsNone(self.store.get_room("A-101")["count"])
        self.assertTrue(any(r["count"] is not None for r in self.store.list_rooms()))

    def test_rooms_without_capacity_still_simulate(self):
        f = self.tmp / "nocap.json"
        f.write_text(json.dumps([{"id": "301", "building": "Main", "floor": 3}]))
        s = Store(self.tmp / "n.db", rooms_file=f)
        rng, state = random.Random(2), {}
        for _ in range(60):
            simulator.tick(s, rng, state)
        self.assertIsNotNone(s.get_room("301")["count"])


if __name__ == "__main__":
    unittest.main()


# ---- motion detector (far-away CCTV) -----------------------------------------
def _cctv_scene(rng, t, n, w=1280, h=720):
    """A noisy fixed room with n small walking 'students' (14x34 pixels)."""
    f = np.full((h, w, 3), 110, np.uint8)
    for x in range(0, w, 80):
        cv2.line(f, (x, 0), (x, h), (90, 90, 90), 3)
    f = np.clip(f.astype(np.int16) + rng.normal(0, 3, f.shape).astype(np.int16), 0, 255).astype(np.uint8)
    for k in range(n):
        x, y = int(100 + 70 * k + 4 * t) % (w - 20), 200 + 55 * (k % 5)
        cv2.rectangle(f, (x, y), (x + 14, y + 34), (40, 60, 160), -1)
    return f


class Motion(unittest.TestCase):
    def counts(self, n, frames=70):
        rng = np.random.default_rng(1)
        d = camera_agent.MotionDetector()
        out = [len(d.detect(_cctv_scene(rng, t, 0 if t < 30 else n))) for t in range(frames)]
        return d, out[40:]

    def test_counts_small_moving_people(self):
        for n in (3, 8):
            with self.subTest(students=n):
                _, c = self.counts(n)
                self.assertEqual(int(np.median(c)), n)

    def test_empty_room_counts_zero(self):
        _, c = self.counts(0)
        self.assertEqual(max(c), 0)

    def test_not_ready_while_learning_the_room(self):
        rng = np.random.default_rng(1)
        d = camera_agent.MotionDetector(warmup=10)
        for t in range(9):
            self.assertEqual(d.detect(_cctv_scene(rng, t, 5)), [])
            self.assertFalse(d.ready)
        d.detect(_cctv_scene(rng, 9, 5))
        self.assertTrue(d.ready)

    def test_sudden_change_of_the_whole_picture_relearns_instead_of_counting(self):
        rng = np.random.default_rng(1)
        d = camera_agent.MotionDetector()
        for t in range(40):
            d.detect(_cctv_scene(rng, t, 0))
        bright = np.clip(_cctv_scene(rng, 0, 0).astype(int) + 70, 0, 255).astype(np.uint8)
        self.assertEqual(d.detect(bright), [])
        self.assertFalse(d.ready)

    def test_worker_reports_nothing_until_ready_then_holds_the_peak(self):
        rng = np.random.default_rng(1)
        frames = iter(_cctv_scene(rng, t, 0 if t < 30 else 4) for t in range(200))
        reports = []
        w = camera_agent.CameraWorker("301", 0, camera_agent.MotionDetector(), reports.append,
                                      interval=0, hold=30)
        w._ensure_open = lambda: object()
        with mock.patch.object(camera_agent, "read_latest", lambda cap, flush=4: next(frames)):
            for _ in range(24):                        # warm-up is 25 frames
                w.step(detect=True)
            self.assertEqual(reports, [])              # still learning: "no signal", never "free"
            for _ in range(26):
                w.step(detect=True)
        self.assertTrue(reports)
        self.assertEqual(max(reports), 4)              # students start walking at frame 30

    def test_make_detector_and_config(self):
        self.assertEqual(camera_agent.make_detector("motion", min_area=200).min_area, 200)
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        (Path(d.name) / "302.json").write_text(json.dumps(
            {"source": "rtsp://x", "detector": "motion", "min_area": 150}))
        cam = load_cameras(d.name)[0]
        self.assertEqual((cam["max_width"], cam["hold"], cam["options"]),
                         (1280, 30.0, {"min_area": 150}))


# ---- YOLO people detector (model replaced by a stand-in, so no download needed) ---
class FakeNet:
    """Pretends to be a YOLO network: returns the prepared detections for every piece."""
    def __init__(self, dets):
        self.out = np.zeros((1, 84, 8400), np.float32)
        for k, (cx, cy, w, h, cls, score) in enumerate(dets):
            self.out[0, :4, k] = (cx, cy, w, h)
            self.out[0, 4 + cls, k] = score

    def setInput(self, blob):
        pass

    def forward(self):
        return self.out


class Yolo(unittest.TestCase):
    def detector(self, dets, **kw):
        with mock.patch.object(cv2.dnn, "readNetFromONNX", lambda path: FakeNet(dets)), \
             mock.patch.object(camera_agent.YoloDetector, "_find_model", staticmethod(lambda m: m)):
            return camera_agent.YoloDetector("fake.onnx", **kw)

    def test_counts_only_people_above_the_confidence(self):
        d = self.detector([
            (100, 200, 40, 120, 0, 0.90),     # person
            (300, 200, 40, 120, 0, 0.80),     # person
            (500, 200, 40, 120, 0, 0.10),     # too unsure
            (200, 100, 90, 60, 1, 0.95),      # class 1 is a bicycle: not a person
        ])
        boxes = d.detect(np.zeros((360, 640, 3), np.uint8))
        self.assertEqual(len(boxes), 2)
        self.assertEqual(sorted(b[0] for b in boxes), [80, 280])        # x = cx - w/2

    def test_overlapping_duplicates_count_once(self):
        d = self.detector([(100, 200, 40, 120, 0, 0.90), (102, 203, 40, 118, 0, 0.60)])
        self.assertEqual(len(d.detect(np.zeros((360, 640, 3), np.uint8))), 1)

    def test_people_cut_by_a_tile_edge_are_ignored(self):
        d = self.detector([(10, 300, 40, 120, 0, 0.9)])                 # touches the left edge
        piece = np.zeros((640, 640, 3), np.uint8)
        self.assertEqual(len(d._run(piece, cut=(False,) * 4)[0]), 1)    # real picture edge: keep
        self.assertEqual(len(d._run(piece, cut=(True, False, False, False))[0]), 0)  # a cut: drop

    def test_wide_frame_is_scanned_in_overlapping_tiles(self):
        self.assertEqual(camera_agent.YoloDetector._starts(1280, 640, 480), [0, 480, 640])
        self.assertEqual(camera_agent.YoloDetector._starts(720, 640, 480), [0, 80])
        self.assertEqual(camera_agent.YoloDetector._starts(500, 640, 480), [0])
        d = self.detector([], tile=True)
        runs = []
        orig = d._run
        d._run = lambda *a, **k: runs.append(1) or orig(*a, **k)
        d.detect(np.zeros((720, 1280, 3), np.uint8))
        self.assertEqual(len(runs), 1 + 3 * 2)                          # whole frame + 3x2 tiles

    def test_make_detector_and_config(self):
        with mock.patch.object(cv2.dnn, "readNetFromONNX", lambda path: FakeNet([])), \
             mock.patch.object(camera_agent.YoloDetector, "_find_model", staticmethod(lambda m: m)):
            d = camera_agent.make_detector("yolo", confidence=0.4, tile=False)
        self.assertEqual((d.kind, d.confidence, d.tile), ("yolo", 0.4, False))
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        (Path(folder.name) / "302.json").write_text(json.dumps(
            {"source": "rtsp://x", "detector": "yolo", "confidence": 0.25, "model": "yolo11n.onnx"}))
        cam = load_cameras(folder.name)[0]
        self.assertEqual((cam["max_width"], cam["hold"], cam["options"]),
                         (1280, 0.0, {"confidence": 0.25, "model": "yolo11n.onnx"}))

    def test_missing_model_file_gives_a_clear_error(self):
        with self.assertRaises(RuntimeError) as ctx:
            camera_agent.YoloDetector._find_model("C:/nowhere/custom.onnx")
        self.assertIn("custom.onnx", str(ctx.exception))
