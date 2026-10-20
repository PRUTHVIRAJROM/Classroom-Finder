import json
import os
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
        # tests must not depend on settings that happen to be set on this computer
        env = mock.patch.dict("os.environ")
        env.start()
        self.addCleanup(env.stop)
        for name in [k for k in os.environ if k.startswith("CF_")]:
            del os.environ[name]
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.store = Store(self.tmp / "t.db", rooms_file=ROOMS, stale_after=90, free_after=120)
        self.client = create_app(api_key=KEY, store=self.store).test_client()

    def post(self, room, count, key=KEY):
        return self.client.post(f"/api/rooms/{room}/occupancy", json={"count": count},
                                headers={"X-API-Key": key})


# ---- occupancy rules --------------------------------------------------------
class StoreRules(Base):
    def test_new_room_is_unknown(self):
        self.assertEqual(self.store.get_room("A-101")["status"], "unknown")

    def test_people_means_occupied(self):
        self.store.record_occupancy("A-101", 12, now=1000)
        self.assertEqual(self.store.get_room("A-101", now=1005)["status"], "occupied")

    def test_grace_period_then_free(self):
        self.store.record_occupancy("A-101", 5, now=1000)
        self.store.record_occupancy("A-101", 0, now=1010)
        self.assertEqual(self.store.get_room("A-101", now=1060)["status"], "occupied")
        self.store.record_occupancy("A-101", 0, now=1120)
        room = self.store.get_room("A-101", now=1125)
        self.assertEqual(room["status"], "free")
        self.assertEqual(room["free_for_seconds"], 125)   # last person seen at t=1000

    def test_never_seen_anyone_is_free_right_away(self):
        self.store.record_occupancy("A-102", 0, now=1000)
        room = self.store.get_room("A-102", now=1010)
        self.assertEqual(room["status"], "free")
        self.assertIsNone(room["free_for_seconds"])

    def test_stale_report_is_unknown_not_free(self):
        self.store.record_occupancy("A-101", 0, now=1000)
        self.assertEqual(self.store.get_room("A-101", now=1091)["status"], "unknown")

    def test_summary_counts(self):
        self.store.record_occupancy("A-101", 3, now=1000)
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
        r = self.post("A-103", 7)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 7)
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
            "A-101", str(video), StubDetector([5]),
            lambda n: self.store.record_occupancy("A-101", n), interval=0.05)
        w.start()
        deadline = time.time() + 5
        while time.time() < deadline and self.store.get_room("A-101")["count"] != 5:
            time.sleep(0.05)
        w.stop()
        room = self.store.get_room("A-101")
        self.assertEqual((room["count"], room["status"]), (5, "occupied"))

    def test_stand_alone_agent_reports_over_http(self):
        from werkzeug.serving import make_server
        video = make_video(self.tmp / "cam.avi")
        server = make_server("127.0.0.1", 0, create_app(api_key=KEY, store=self.store))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        with mock.patch.object(camera_agent, "make_detector", lambda kind: StubDetector([4])):
            camera_agent.main(["--room", "A-101", "--source", str(video), "--detector", "face",
                               "--server", f"http://127.0.0.1:{server.server_port}",
                               "--api-key", KEY, "--once"])
        room = self.store.get_room("A-101")
        self.assertEqual((room["count"], room["status"]), (4, "occupied"))


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


# ---- hosted demo switch (environment variables, as set on the hosting site) --
class HostedMode(Base):
    def make(self, **env):
        with mock.patch.dict("os.environ", env, clear=False):
            return create_app(api_key=KEY, store=self.store)

    def test_off_by_default(self):
        app = self.make()
        self.assertEqual(app.config["SIMULATED_ROOMS"], [])

    def test_demo_switch_marks_every_room_except_live_ones(self):
        app = self.make(CF_SIMULATE="1", CF_LIVE_ROOMS="A-101, B-301")
        fake = app.config["SIMULATED_ROOMS"]
        self.assertEqual(len(fake), 13)
        self.assertNotIn("A-101", fake)
        self.assertNotIn("B-301", fake)

    def test_demo_switch_zero_means_off(self):
        self.assertEqual(self.make(CF_SIMULATE="0").config["SIMULATED_ROOMS"], [])

    def test_live_room_is_never_overwritten_by_the_demo(self):
        self.make(CF_SIMULATE="1", CF_LIVE_ROOMS="A-101")
        self.post("A-101", 6)
        for _ in range(3):
            simulator.tick(self.store, random.Random(5), {}, skip={"A-101"})
        self.assertEqual(self.store.get_room("A-101")["count"], 6)


if __name__ == "__main__":
    unittest.main()
