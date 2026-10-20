"""Fake classroom activity so the website looks alive without a camera in every room.

Rooms that have a real camera (listed in `skip`) are left alone. A few simulated
rooms go "offline" now and then so the 'no signal' state shows up too.
The website labels every simulated room as demo data.
"""
import random
import threading
import time


def tick(store, rng, state, skip=()):
    """Advance the simulation one step and write counts into the store."""
    for room in store.list_rooms():
        rid = room["id"]
        if rid in skip:
            continue
        cap = room["capacity"] or 40
        s = state.setdefault(rid, {"count": 0, "offline_until": 0.0})
        now = time.time()
        if now < s["offline_until"]:
            continue                      # camera "offline": send nothing
        if rng.random() < 0.01:
            s["offline_until"] = now + rng.randint(100, 200)
            continue
        if s["count"] == 0:
            if rng.random() < 0.15:       # a class starts
                s["count"] = rng.randint(max(3, cap // 4), cap)
        else:
            if rng.random() < 0.10:       # a class ends
                s["count"] = 0
            else:                         # people come and go
                s["count"] = max(1, min(cap, s["count"] + rng.randint(-2, 2)))
        store.record_occupancy(rid, s["count"])


def start_background(store, interval=5.0, seed=None, skip=()):
    """Run the simulation in a daemon thread. Returns the stop Event."""
    rng = random.Random(seed)
    state = {}
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            tick(store, rng, state, skip)
            stop.wait(interval)

    threading.Thread(target=loop, name="simulator", daemon=True).start()
    return stop
