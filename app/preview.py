"""Holds the latest annotated camera frame per room, for the local-only preview page."""
import threading
import time


class PreviewHub:
    def __init__(self):
        self._frames = {}
        self._lock = threading.Lock()

    def update(self, room, jpeg_bytes):
        with self._lock:
            self._frames[str(room)] = (jpeg_bytes, time.monotonic())

    def latest(self, room):
        with self._lock:
            return self._frames.get(str(room))
